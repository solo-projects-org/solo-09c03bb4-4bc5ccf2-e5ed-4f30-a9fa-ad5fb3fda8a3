"""按结算日的关账核验。

核验语义：
- 重号检查、订单数与金额合计、变更标记三类查询在同一个
  「REPEATABLE READ, READ ONLY」事务里执行，共享同一数据库快照；
  扫描期间其他连接可正常补记/变更（不会被阻塞），新数据也不会进入本次快照。
- settlement_orders 上的触发器会把任何连接的 INSERT/UPDATE/DELETE 涉及的
  结算日追加到 settlement_order_mutations（只增不减）。核验结果记录快照内
  可见的当日流水行数作为「变更标记」；展示时再与库中当前标记比较：
    * 标记不一致 -> 核验后该日又有补记/变更，旧结果不得再显示为可信；
    * 只有重新核验成功（新快照、新标记一致）才恢复可信状态。
- 核验出错时：本次尝试记录为 error，此前最近一次成功结果及其有效性
  （由实时标记比较得出）原样保留，错误原因单独展示。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import date

import asyncpg

STATEMENT_TIMEOUT_MS = 15_000  # 核验查询的服务端限时（兜底，正常为毫秒级）
MAX_DUP_DETAILS = 100          # 重号明细最多保存/展示条数（组数、行数另有总数）

# 重号检查：取前 MAX_DUP_DETAILS 组明细，同时用窗口函数算出全部重号组数与涉及行数
DUP_SQL = """
WITH d AS (
    SELECT order_no, COUNT(*) AS cnt
    FROM settlement_orders
    WHERE settle_date = $1 AND status = 'SETTLED'
    GROUP BY order_no
    HAVING COUNT(*) > 1
)
SELECT order_no, cnt,
       COUNT(*) OVER ()                AS total_groups,
       COALESCE(SUM(cnt) OVER (), 0)::bigint AS total_rows
FROM d
ORDER BY cnt DESC, order_no
LIMIT $2
"""

TOTAL_SQL = """
SELECT COUNT(*) AS order_cnt,
       COALESCE(SUM(amount), 0)::text AS total_amount
FROM settlement_orders
WHERE settle_date = $1 AND status = 'SETTLED'
"""

MARKER_SQL = "SELECT COUNT(*) FROM settlement_order_mutations WHERE settle_date = $1"


@dataclass
class Verification:
    """一次核验尝试（成功或失败）。"""

    settle_date: date
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "running"                # running|ok|error
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    snapshot_marker: int | None = None     # 快照内可见的当日变更流水行数
    duplicates: list[dict] | None = None   # 重号明细（最多 MAX_DUP_DETAILS 条）
    duplicate_groups: int = 0              # 重号订单号总组数
    duplicate_rows: int = 0                # 重号涉及的订单行数
    order_cnt: int | None = None           # 无重号时的可信订单数
    total_amount: str | None = None        # 无重号时的可信金额合计
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "settle_date": self.settle_date.isoformat(),
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": (
                (self.finished_at - self.started_at) * 1000
                if self.finished_at is not None else None
            ),
            "snapshot_marker": self.snapshot_marker,
            "duplicates": self.duplicates,
            "duplicate_groups": self.duplicate_groups,
            "duplicate_rows": self.duplicate_rows,
            "duplicates_truncated": self.duplicate_groups > len(self.duplicates or []),
            "order_cnt": self.order_cnt,
            "total_amount": self.total_amount,
            "error": self.error,
        }


class VerificationStore:
    """内存核验仓库：按结算日保留最近若干次尝试，重启即清空。"""

    def __init__(self, per_date: int = 20, max_dates: int = 200):
        self._by_date: dict[str, list[Verification]] = {}
        self._per_date = per_date
        self._max_dates = max_dates

    def add(self, v: Verification) -> None:
        key = v.settle_date.isoformat()
        attempts = self._by_date.setdefault(key, [])
        attempts.append(v)
        del attempts[:-self._per_date]
        if len(self._by_date) > self._max_dates:
            # 淘汰最久未核验的日期（不淘汰刚写入的）
            oldest = min(self._by_date, key=lambda k: self._by_date[k][-1].started_at)
            if oldest != key:
                del self._by_date[oldest]

    def _attempts(self, key: str) -> list[Verification]:
        return self._by_date.get(key, [])

    def last_success(self, key: str) -> Verification | None:
        for v in reversed(self._attempts(key)):
            if v.status == "ok":
                return v
        return None

    def last_error(self, key: str) -> Verification | None:
        """最近一次尝试若是失败则返回它（用于展示错误原因）。

        尝试按完成顺序追加，因此列表末尾即最新尝试；最新一次若是成功，
        此前的失败即被取代，不再展示。
        """
        attempts = self._attempts(key)
        if attempts and attempts[-1].status == "error":
            return attempts[-1]
        return None


async def run_verification(dsn: str, settle_date: date) -> Verification:
    """在同一 REPEATABLE READ, READ ONLY 快照内完成核验。

    出错不抛异常，返回 status=error 的 Verification，由调用方记录，
    以便保留此前成功结果并展示错误原因。
    """
    v = Verification(settle_date=settle_date)
    conn: asyncpg.Connection | None = None
    try:
        conn = await asyncpg.connect(dsn)
        # 同一可重复读只读事务：三条查询共享同一快照，期间其他连接照常写入
        await conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        await conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
        rows = await conn.fetch(DUP_SQL, settle_date, MAX_DUP_DETAILS)
        total = await conn.fetchrow(TOTAL_SQL, settle_date)
        marker = await conn.fetchval(MARKER_SQL, settle_date)
        await conn.execute("ROLLBACK")  # 只读事务，回滚即结束且无副作用
        v.status = "ok"
        v.duplicates = [{"order_no": r["order_no"], "cnt": r["cnt"]} for r in rows]
        v.duplicate_groups = rows[0]["total_groups"] if rows else 0
        v.duplicate_rows = rows[0]["total_rows"] if rows else 0
        v.order_cnt = total["order_cnt"]
        v.total_amount = total["total_amount"]
        v.snapshot_marker = marker
    except Exception as exc:  # 连接异常、超时、SQL 报错等
        v.status = "error"
        v.error = f"{type(exc).__name__}: {exc}"
    finally:
        v.finished_at = time.time()
        if conn is not None:
            try:
                if conn.is_in_transaction():
                    await conn.execute("ROLLBACK")
            except Exception:
                pass
            try:
                await conn.close()
            except Exception:
                pass
    return v


async def build_date_state(pool: asyncpg.Pool, store: VerificationStore,
                           settle_date: date) -> dict:
    """组装某结算日的展示状态：最近一次成功结果 + 实时有效性 + 最近错误。

    有效性在每次读取时用库中当前变更标记实时判定，因此任何连接补记后，
    旧结果立即不再可信。
    """
    key = settle_date.isoformat()
    success = store.last_success(key)
    error = store.last_error(key)
    async with pool.acquire() as conn:
        marker = await conn.fetchval(MARKER_SQL, settle_date)
    state = {
        "settle_date": key,
        "current_marker": marker,
        "result": None,
        "trusted": False,
        "stale": False,
        "last_error": None,
    }
    if success is not None:
        stale = success.snapshot_marker != marker
        state["result"] = success.to_dict()
        state["stale"] = stale
        # 可信 = 核验成功 + 无重号 + 核验后无任何补记/变更
        state["trusted"] = (not stale) and success.duplicate_groups == 0
    if error is not None:
        state["last_error"] = {
            "message": error.error,
            "at": error.finished_at or error.started_at,
        }
    return state
