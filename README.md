# 运费入仓预演（Freight Warehouse Rehearsal）

一张运单关联 **过磅单** 与 **价目** 两张来源表后，可能漏计（某侧命中 0 条）
也可能冒出多条计费明细（某侧命中多条，两次内连接产生笛卡尔积）。本服务用
FastAPI + SQLite 对正式入仓前的匹配结果做“预演”，只产出每票命中数、
每月内连接总行数和异常清单，**全程不展开计费行**。

## 匹配规则

| 来源 | 记录号 | 匹配键 | 操作 |
| --- | --- | --- | --- |
| 过磅单 `weigh_records` | `record_no` 唯一 | 车次 `train_no` | 补录 / 替换 / 撤回 |
| 价目 `price_records` | `record_no` 唯一 | 始发站 `origin` + 发车日落入 `[valid_from, valid_to]`（**含首尾两日**） | 补录 / 替换 / 撤回 |
| 运单 `waybills` | `waybill_no` 唯一 | 车次、始发站、发车日 | `/docs` 一次批量导入 |

- 每票运单的两次内连接生成行数 = `过磅命中数 × 价目命中数`。
- 任一侧命中数 ≠ 1 即异常（0 = 漏配，>1 = 重复），异常页给出两侧各自命中数。
- 月合计按发车月（`YYYY-MM`）统计全部运单的内连接总行数。

## 快速启动（docker compose）

```bash
docker compose up --build
# 打开交互式文档，可直接导入运单、补录/替换/撤回来源：
#   http://localhost:8000/docs
# 数据库持久化在命名卷 freight-data（/data/freight.db，WAL 模式）
```

本地直跑（无 Docker 时）：

```bash
pip install -r requirements.txt
FREIGHT_DB_PATH=./data/freight.db uvicorn app.main:app --reload
```

## 接口一览（均在 /docs 可见可调）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/waybills/import` | 批量导入运单；批次内/库内撞号整批 409 回滚 |
| PUT | `/weigh/records/{record_no}` | 过磅单补录或替换（upsert） |
| DELETE | `/weigh/records/{record_no}` | 撤回过磅单，不存在返回 404 |
| PUT | `/price/records/{record_no}` | 价目补录或替换（含窗口倒置校验） |
| DELETE | `/price/records/{record_no}` | 撤还价目 |
| GET | `/rehearsal/months/{mon}/total` | 月内连接总行数（读预聚合表） |
| GET | `/rehearsal/anomalies` | 异常运单键集分页（`cursor`/`limit`，可按月过滤） |
| GET | `/rehearsal/months/{mon}/overview` | **同一只读快照**返回月合计 + 异常页 + 版本号 |
| GET | `/rehearsal/waybills/{no}` | 单票预演明细 |
| POST | `/admin/rebuild` | 全量重建（核对用，正常写入只走增量） |
| GET | `/health` | 数据版本号与库路径 |

写入响应统一返回 `version / affected_waybills / affected_months`，可直接核对
增量范围。

## 关键设计如何满足需求

1. **增量重算**：写入前记录旧匹配条件（过磅的旧/新车次、价目的旧/新窗口），
   只定位这些条件涉及的运单；月份只取这些运单的发车月（价目改窗口时为新旧
   窗口并集）。响应里回传实际重算范围，可验证没有全量重算。
2. **来源与结果一起生效，失败可查原结果**：来源写入与 `match_stats` /
   `monthly_totals` 刷新、版本号 +1 在同一个 `BEGIN IMMEDIATE` 事务内提交；
   任何错误整体回滚，旧版本结果保持可读（有测试覆盖撞号、撤回不存在记录号）。
3. **同次查询同一已提交状态**：overview 在一个显式只读快照事务里执行两条查询，
   并返回该快照的 `version`；WAL 模式下读不阻塞写、写不阻塞读（有并发线程实测）。
4. **合计不逐单扫描**：`monthly_totals` 是预聚合表，月合计是主键索引点查；
   刷新月份时用一条 `GROUP BY` 集合语句，不按单循环。
5. **异常页只读异常运单**：`idx_match_anomaly(is_anomaly, departure_mon,
   waybill_no)` 覆盖索引，`EXPLAIN QUERY PLAN` 实测为 `SEARCH ... COVERING
   INDEX`，不碰三张来源表。
6. **查询不读来源明细、不展开计费行**：库中没有计费明细表，`match_stats`
   每票仅一行（命中数 + 乘积行数）；所有读接口只访问结果表。

## 测试

```bash
python3 -m pytest -q          # 14 个端到端用例，真实 SQLite + TestClient
```

覆盖：1:1 正常匹配、漏配与重复并存、有效期首尾边界、过磅改车次/价目改窗口的
增量范围、原子回滚、撤回失败保旧结果、键集分页、跨月合计、全量重建一致性、
版本号单调、查询计划只走结果表索引、输入校验。

## 目录

```
app/            FastAPI 应用（db 连接/模式、models 校验、service 增量重算、main 路由）
tests/          pytest 端到端用例
Dockerfile / docker-compose.yml / requirements.txt
```

## 实现状态

### 已实现

- 批量运单导入（唯一单号、车次、始发站、发车日，撞号整批拒绝）。
- 过磅单/价目按记录号补录、替换、撤回；价目有效期含首尾两日的日期窗口匹配。
- 每票两侧命中数、内连接行数、异常标记；按月合计；异常运单键集分页与按月过滤。
- 仅重算新旧匹配条件涉及运单与月份的增量机制；`/admin/rebuild` 全量核对入口。
- 来源与结果同事务原子提交、版本号、只读快照一致性、WAL 并发。
- 结果表 + 覆盖索引支撑的只读查询路径，无计费明细表。
- docker compose 一键启动、`/docs` 全接口可调、健康检查与持久化卷。

### 已实测

- 14 个 pytest 用例全部通过；真实 uvicorn 进程手工走通漏配/重复并存场景，
  手工复核行数（2×2、1×2、0×1、1×1 → 月合计 7）。
- `EXPLAIN QUERY PLAN` 确认月合计点查、异常页覆盖索引、单票主键索引。
- 并发线程实测：读快照期间另一连接提交新版本，读侧两次结果与版本号保持不变。
- 失败路径实测：撞号导入 409 全回滚、撤回不存在记录 404 且版本/旧结果不变。

### 未完成 / 边界说明

- 本环境未安装 Docker 守护进程，compose 文件仅做了结构静态校验，未实际
  `docker compose up` 构建运行（Dockerfile 基于 python:3.11-slim，步骤与本地
  验证过的安装方式一致）。
- 未做认证/鉴权与多租户隔离，定位为内网预演工具。
- 未实现运单本身的修改/删除接口（需求只要求一次导入）；需要时可复用同一套
  增量重算路径。
- 分页为运单号键集分页（稳定、可扩展），未提供 offset 跳页。
- 金额未入模：价目表带 `price_per_t` 但预演只统计行数与命中数，不生成金额
  明细，符合“不展开计费行”的约束。
