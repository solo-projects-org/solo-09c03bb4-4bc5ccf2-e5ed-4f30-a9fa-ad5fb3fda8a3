"""FastAPI 入口：网页 + 关账核验 API + 对照批次 API + 样表补记 API。"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from plans import structure_lines
from runner import BatchRunner
from sqlcheck import validate_select_sql
from store import Batch, BatchStore
from verify import VerificationStore, build_date_state, run_verification

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/settlement"
)

store = BatchStore(capacity=50)
runner = BatchRunner(DATABASE_URL, store)
vstore = VerificationStore()
_verify_locks: dict[str, asyncio.Lock] = {}
pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    last_exc: Exception | None = None
    for _ in range(60):  # 等数据库就绪（compose 里还有 healthcheck 兜底）
        try:
            pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
            break
        except Exception as exc:
            last_exc = exc
            await asyncio.sleep(1)
    if pool is None:
        raise RuntimeError(f"无法连接数据库: {last_exc}")
    yield
    await pool.close()


app = FastAPI(title="结算关账核验与 SQL 计划对照台", lifespan=lifespan)


class VerificationIn(BaseModel):
    settle_date: date


def _verify_lock_for(key: str) -> asyncio.Lock:
    if len(_verify_locks) > 200:  # 清理不再占用的锁，避免无界增长
        for k in [k for k, lk in _verify_locks.items() if not lk.locked()][:50]:
            _verify_locks.pop(k, None)
    return _verify_locks.setdefault(key, asyncio.Lock())


@app.post("/api/verifications")
async def create_verification(body: VerificationIn):
    """核验指定结算日：同一快照内查重号、订单数与金额合计、变更标记。"""
    if pool is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")
    key = body.settle_date.isoformat()
    lock = _verify_lock_for(key)
    if lock.locked():
        return JSONResponse(
            status_code=409,
            content={"detail": "该结算日正在核验中，请稍后再试", "busy": True},
        )
    async with lock:
        v = await run_verification(DATABASE_URL, body.settle_date)
        vstore.add(v)
    return await build_date_state(pool, vstore, body.settle_date)


@app.get("/api/verifications/{settle_date}")
async def get_verification(settle_date: date):
    """查询某结算日最近一次成功核验及其当前有效性（实时比对变更标记）。"""
    if pool is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")
    return await build_date_state(pool, vstore, settle_date)


class BatchIn(BaseModel):
    sql: str = Field(min_length=1, max_length=20000)


@app.post("/api/batches")
async def create_batch(body: BatchIn):
    try:
        sql = validate_select_sql(body.sql)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    batch = Batch(sql=sql)
    if not runner.try_start(batch):
        return JSONResponse(
            status_code=409,
            content={"detail": "已有对照批次在运行，当前繁忙，请稍后再试", "busy": True},
        )
    store.add(batch)
    return JSONResponse(status_code=201, content=batch.to_dict())


@app.get("/api/batches")
async def list_batches():
    return {
        "busy": runner.busy,
        "current_batch_id": runner.current_batch_id,
        "batches": [b.to_dict() for b in store.list()],
    }


@app.get("/api/batches/{batch_id}")
async def get_batch(batch_id: str):
    batch = store.get(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    data = batch.to_dict(include_plans=True)
    for ex, ex_dict in zip(batch.executions, data["executions"]):
        if ex.plan_structure is not None:
            ex_dict["structure_lines"] = structure_lines(ex.plan_structure)
    return data


@app.post("/api/batches/{batch_id}/cancel")
async def cancel_batch(batch_id: str):
    batch = store.get(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    found, pid = runner.request_cancel(batch_id)
    if not found:
        return {"ok": False, "detail": "批次未在运行，无法取消"}
    if pid is not None and pool is not None:
        try:
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT pg_cancel_backend($1)", pid)
        except Exception:
            pass  # 取消请求失败不阻断，worker 侧还有标记位和兜底超时
    return {"ok": True}


class SampleRowIn(BaseModel):
    settle_date: date | None = None                 # 缺省为数据库当前日期
    order_no: str | None = Field(default=None, max_length=64)  # 缺省随机生成


@app.post("/api/sample-row")
async def add_sample_row(body: SampleRowIn | None = None):
    """补记一行结算样表数据；对照/核验运行期间也可写入（它们用同一快照，看不到新行）。

    可指定 settle_date / order_no（便于演示重号冲突与结果失效）；补记会触发
    变更流水，使对应结算日的已核验结果失效。
    """
    if pool is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")
    order_no = (body.order_no or "").strip() if body else ""
    settle_date = body.settle_date if body else None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            WITH m AS (
                SELECT 1 + (random() * 4999)::int              AS merchant_id,
                       round((1 + random() * 499)::numeric, 2) AS amount
            )
            INSERT INTO settlement_orders (order_no, merchant_id, amount, fee, status, settle_date)
            SELECT CASE WHEN $1 = '' THEN 'SO-M-' || upper(substr(md5(random()::text), 1, 10))
                        ELSE $1 END,
                   merchant_id, amount, round(amount * 0.006, 2), 'SETTLED',
                   COALESCE($2, CURRENT_DATE)
            FROM m
            RETURNING order_id, order_no, merchant_id, amount, fee, status, settle_date
            """,
            order_no, settle_date,
        )
    return {"ok": True, "row": _jsonable(dict(row))}


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (datetime, date, Decimal)):
        return str(value)
    return value


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
