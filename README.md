# 卫星遥感产品目录采集、入库与组合查询系统

该项目按单一职责拆分为：

- `sources.py`：读取 fixture / HTTP 原始记录；
- `normalize.py`：字段映射、标准化、质量校验和稳定去重键；
- `storage.py`：SQLite schema、幂等写入、拒绝记录、统计与**真实 SQL 候选筛选**；
- `geometry.py`：经纬度矩形规范化、跨日期变更线拆分、交集/相减/合并与面积；
- `selection.py`：同版本去重、元数据冲突判定、穷举/贪心组合选择与资源上限；
- `query.py`：组合查询编排（目录筛选 → 几何规范化 → 组合选择三阶段解耦）；
- `pipeline.py`：只编排依赖，不承担文件或 SQL 细节；
- `application.py`：组合依赖并管理资源生命周期；
- `cli.py`：`ingest` / `query` 子命令和结果输出。

## 数据工程链路

`fixture -> source adapter -> normalization -> deduplication -> quality checks -> SQLite sink`。

默认只使用本地 fixture，不连接真实站点，也不包含账号、连接串或 token。

## 外部依赖类型与版本

- Python 3.11+
- pytest 8.3.x（开发/测试）

## 运行

```bash
PYTHONPATH=src python3 -m satellite_catalog.cli ingest \
  --fixture fixtures/sample.json --database /tmp/satellite-catalog.sqlite3
PYTHONPATH=src python3 -m pytest tests
```

## 组合查询（budgeted combination query）

输入目标矩形、采集时间范围、云量上限和产品数量预算，返回所选
`product_id/version`、每幅影像的实际贡献区域、总覆盖与仍未覆盖部分。

```bash
PYTHONPATH=src python3 -m satellite_catalog.cli query \
  --database /tmp/satellite-catalog.sqlite3 \
  --target-bbox "0,0,10,10" \
  --acquired-after 2026-10-01 --acquired-before 2026-10-31 \
  --max-cloud-cover 10 --budget 2
# 可选资源上限：--max-candidates 200 --max-combinations 200000
```

### 几何约定（已在 `tests/test_geometry.py` 逐条验证）

- **bbox 顺序**：统一为 `[west, south, east, north]`
  （最小经度, 最小纬度, 最大经度, 最大纬度）；接受 4 元素序列或
  逗号分隔字符串。经度 `[-180, 180]`、纬度 `[-90, 90]`，零宽/零高、
  顺序颠倒或越界一律拒绝。
- **跨日期变更线**：当 `east < west` 时表示跨 180° 经线，规范化为
  `[west, 180]` 与 `[-180, east]` 两块矩形；目标与影像 bbox 采用同一
  规则，所有运算都在坐标平面内完成。
- **边界接触**：只有交集具有**严格正面积**才算有效覆盖；仅在点或线上
  接触（零面积交集）记为 `no_positive_overlap`，不占用预算。
- **面积约定**：面积 = 经度跨度 × 纬度跨度（平方度），是坐标平面面积的
  演示约定，**显式不代表地表面积**，不做球面/投影修正。

### 三阶段如何接入真实存储

1. **目录筛选**：`ObservationStore.query_candidates` 用 SQLite JSON1
   在已入库表上执行真实 SQL，按采集时间闭区间与云量上限缩小候选；
2. **几何规范化**：`geometry.parse_bbox` 解析每条记录的 bbox，
   `selection.build_candidates` 按 `(product_id, version)` 合并重复来源
   （同一产品版本只占一个名额，来源列表保留可追溯），同版本元数据矛盾
   （bbox/云量/采集时间不一致）或任何来源缺少可解析范围的整组记录，
   记为 `metadata_conflict` / `missing_or_invalid_extent` 排除，不另建
   产品版本生命周期；
3. **组合选择**：预算内优先最大化覆盖面积，其次最小化产品数，再按
   云量、采集时间、产品 ID 的确定性键序打破平局。

### 最优性与提前停止（调用方必须能感知）

- 候选规模可穷举时按子集大小枚举，`method="exhaustive"` 且
  `optimal=true`；完整覆盖目标后立即结束。
- 候选数超过 `max_candidates`（截断，`candidate_limit_reached`）或枚举
  组合数超过 `max_combinations`（`combination_limit_reached`）时，回退到
  边际增量贪心并做冗余剔除，此时 `method="greedy"`、`optimal=false`、
  `stopped_early=true`，**不宣称最优**。
- 返回中的 `stop_reason` 还包括 `exhaustive_completed`、`budget_is_zero`、
  `no_eligible_candidate`；`excluded` 给出每条被排除记录的来源与原因。
- 每个所选产品的 `contribution` 是扣除与先前产品重叠后的实际贡献矩形，
  各幅 `contribution_area` 之和等于 `covered_area`，可逐幅复算。

## HTTP source mode


The default fixture mode remains deterministic and credential-free. For a real JSON endpoint, use:

```bash
PYTHONPATH=src python3 -m satellite_catalog.cli \
  --url https://example.invalid/api/records \
  --database /tmp/satellite-catalog.sqlite3 \
  --raw-dir /tmp/satellite-catalog-raw
```

Replace `satellite_catalog` and `satellite-catalog` with this repository's package/project name. The endpoint must return a JSON list, or an object containing `records`, `data`, `items`, or `results`. Use `--records-key` for a different nested path. Optional authentication is read from `DATA_SOURCE_BEARER_TOKEN` or `DATA_SOURCE_API_KEY`; do not commit their values. `--raw-dir` stores immutable content-addressed copies of successful HTTP responses. HTTP and JSON failures stop the run instead of being recorded as successful ingestion.

External dependency types and versions: Python 3.11+, pytest 8.3.x for tests. The HTTP adapter uses Python's standard library and adds no runtime package.
