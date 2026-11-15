# 结算关账核验与 SQL 计划对照台

面向夜班结算数据员与内网工程师的小工具，包含两块能力：

1. **关账核验（按结算日）**：核验指定结算日的订单能否用于下游汇总；
2. **SQL 计划对照**：迁库后夜间结算 SQL 变慢，在同一份数据上对比
   **默认计划** 与 **抑制嵌套循环（`SET LOCAL enable_nestloop=off`）** 的实际执行效果。

## 快速开始

```bash
docker compose up --build
```

打开 <http://localhost:8000> 。首次启动时数据库会自动建结算样表并灌入
100 万行订单（约需半分钟），`web` 会等 `db` 健康检查通过后再起。

重新初始化样表数据：

```bash
docker compose down -v && docker compose up --build
```

> 注意：变更流水表与触发器由 `db/init.sql` 在初始化时创建。从旧版本升级后，
> 需要 `docker compose down -v` 重建数据卷才能使用关账核验。

## 关账核验怎么工作

网页选择结算日后点击「开始核验」，服务端开一条**专用连接**，在同一个
**`REPEATABLE READ, READ ONLY`** 事务里依次执行：

1. 重号检查：`settle_date = 当天 AND status = 'SETTLED'` 按 `order_no` 分组，
   找出出现次数 > 1 的订单号（明细最多展示 100 组，另给出总组数与涉及行数）；
2. 汇总：同一条件下的订单数与 `SUM(amount)`；
3. 变更标记：`settlement_order_mutations` 中该日的流水行数。

三条查询共享**同一事务快照**，扫描期间其他连接可正常补记/变更（不会被阻塞），
新数据也不会进入本次快照。

**可信判定**（每次读取时实时计算，不是存下来的标志位）：

- `settlement_orders` 上的语句级触发器会把**任何数据库连接**的
  INSERT/UPDATE/DELETE 涉及的结算日追加到 `settlement_order_mutations`（只增不减）；
- 核验结果记录快照内可见的变更标记；展示时与库中当前标记比较：
  - 标记一致且无重号 → **可信**，展示可信订单数与金额合计；
  - 有重号 → 展示冲突订单号与出现次数，不可用于下游汇总；
  - 标记不一致 → 核验后该日又有补记/变更（包括扫描期间提交的新订单），
    旧结果**不得再显示为可信**，仅作参考并提示重新核验；
- 只有**重新核验成功**（新快照、新标记一致）才恢复可信状态；
- 核验出错时，本次尝试记为失败，**保留上一次成功结果及其实际有效性**，
  并展示错误原因。

页面上的「补记样表数据」可指定结算日与订单号（均可留空：结算日缺省当天、
订单号缺省随机），用于演示补记后结果失效、重复补记同一订单号触发重号冲突。

## SQL 计划对照怎么工作

提交一条只读 SELECT 后，后台会开一条**专用连接**，在**同一个
`REPEATABLE READ, READ ONLY` 事务**里执行 4 次 `EXPLAIN (ANALYZE, FORMAT JSON)`：

| 顺序 | 轮次 | 设置 |
|----|----|------------------------|
| #1 | 1  | 默认（enable_nestloop=on） |
| #2 | 1  | 关闭嵌套循环（=off）       |
| #3 | 2  | 关闭嵌套循环（=off）       |
| #4 | 2  | 默认（=on）               |

第二轮颠倒顺序，用来抵消缓存预热造成的偏差。四次执行共享**同一事务快照**，
对照期间其他连接照常写入（页面上的「补记样表数据」按钮可随时插一行），
新数据不会进入本批结果。

## 结论判定规则

- 两轮快慢结论**相反** → 标为「无法判断改善」；
- 两种设置得到**相同计划结构**（节点类型/扫描关系/索引/连接策略一致，
  忽略代价与行数等数值）→ 标为「无法判断改善」；
- 两轮一致 → 给出「关闭嵌套循环更快 / 默认计划更快」的结论；
- **超时、取消或报错** → 本批不给比较结论。

## 运行约束

- 同时只运行一批；运行中再提交会收到 **409 繁忙** 提示，网页也会显示名额占用；
- 每次执行由服务端 `statement_timeout = 10s` 限时，另有 15 秒客户端兜底
  （到点直接断开连接，确保 SQL 实际停止）；
- 「取消」通过 `pg_cancel_backend` 让正在执行的 SQL 真正停下来；
- 运行名额在 **本批 SQL 实际停止 → 事务结束 → 连接关闭** 之后才释放，
  期间其他请求（查询批次、补记数据）照常响应；
- 输入校验：仅单条只读 SELECT（sqlparse 静态检查 + READ ONLY 事务双保险）。

## 样表与示例 SQL

- `merchants`：5000 个商户，类别 A 只有 100 个；
- `settlement_orders`：100 万行结算订单，**类别 A 的 100 个商户沉淀了 70 万单**
  （数据倾斜）。数据库关闭了并行查询（`max_parallel_workers_per_gather=0`，
  模拟迁库后实例未开并行的场景），此时优化器估算「每商户约 200 单」，
  按类别 A 关联时容易误选嵌套循环——实际要捞 70 万行。

页面提供三个预设样例，分别对应三种结论分支：

1. **类别 A 结算汇总**（默认）：`left(m.category, 1) = 'A'` 的表达式过滤让优化器
   只能用默认选择率（把 100 个商户估成 25 个），默认计划误选嵌套循环，
   通常可得到「关闭嵌套循环更快」的结论；
2. **单商户结算明细**：高选择性过滤，索引嵌套循环占绝对优势，
   通常得到「默认计划更快」；
3. **每日结算汇总**：无关联查询，两种设置计划结构相同 → 「无法判断改善」。

默认预置的示例 SQL：

```sql
SELECT m.merchant_id,
       m.merchant_name,
       COUNT(*)      AS order_cnt,
       SUM(o.amount) AS total_amount,
       SUM(o.fee)    AS total_fee
FROM settlement_orders o
JOIN merchants m ON m.merchant_id = o.merchant_id
WHERE left(m.category, 1) = 'A'
GROUP BY m.merchant_id, m.merchant_name
ORDER BY total_amount DESC;
```

## API 一览

| 方法 | 路径 | 说明 |
|-----|------|------|
| POST | `/api/verifications` | 核验指定结算日（body: `{"settle_date": "YYYY-MM-DD"}`，409=该日正在核验） |
| GET  | `/api/verifications/{date}` | 该日最近一次成功核验 + 实时可信状态 + 最近错误 |
| POST | `/api/batches` | 提交对照批次（409=繁忙，400=SQL 校验失败） |
| GET  | `/api/batches` | 批次列表（不含完整计划 JSON） |
| GET  | `/api/batches/{id}` | 批次详情（含 4 次计划 JSON 与结构摘要） |
| POST | `/api/batches/{id}/cancel` | 取消运行中的批次 |
| POST | `/api/sample-row` | 向 `settlement_orders` 补记一行（body 可空；可指定 `settle_date`、`order_no`） |

## 目录结构

```
├── docker-compose.yml
├── db/init.sql            # 结算样表 + 倾斜数据 + 变更流水表与触发器
└── app/
    ├── Dockerfile
    ├── main.py            # FastAPI 入口（核验/对照/补记 API）
    ├── verify.py          # 关账核验（单事务快照、变更标记、可信判定、内存仓库）
    ├── runner.py          # 批次执行器（单事务 4 次执行、限时、取消、名额管理）
    ├── plans.py           # 计划结构归一化 + 结论判定
    ├── sqlcheck.py        # 只读 SELECT 校验
    ├── store.py           # 内存批次仓库
    └── static/            # 网页
```

> 说明：该工具面向受信任的内网工程师，输入的 SQL 会被直接执行（只读事务保护），
> 请勿暴露到公网。批次与核验数据保存在内存中，重启即清空（订单与变更流水在数据库中）。
