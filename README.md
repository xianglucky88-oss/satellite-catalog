# 卫星遥感产品目录采集与元数据入库系统

该项目按单一职责拆分为：

- `sources.py`：读取 fixture 原始记录；
- `normalize.py`：字段映射、标准化、质量校验和稳定去重键；
- `storage.py`：SQLite schema、幂等写入、拒绝记录和统计；
- `pipeline.py`：只编排依赖，不承担文件或 SQL 细节；
- `application.py`：组合依赖并管理资源生命周期；
- `cli.py`：命令行参数和结果输出。

## 数据工程链路

`fixture -> source adapter -> normalization -> deduplication -> quality checks -> SQLite sink`。

默认只使用本地 fixture，不连接真实站点，也不包含账号、连接串或 token。

## 外部依赖类型与版本

- Python 3.11+
- pytest 8.3.x（开发/测试）

## 运行

```bash
PYTHONPATH=src python3 -m satellite_catalog.cli ingest --fixture fixtures/sample.json --database /tmp/satellite-catalog.sqlite3
PYTHONPATH=src python3 -m pytest tests
```

## 产品版本沿革（lineage）

版本字符串是发布者给出的不透明标识：系统不按 v9、v10 的字面或目录到达顺序
推断新旧。版本之间的新旧关系只来自本地显式声明，版本是否存在只来自目录观测；
缺少声明时各版本保持并列。

声明按“流”（`stream`，一个发布者沿革来源，绑定单一 `product_id`）组织，每份
声明携带单调递增的 `revision`、完整的 `supersedes` 边集（`successor` 替代
`predecessor`，允许一个版本替代多个旧版）和 `withdrawn` 列表。新修订整体替换
该流已提交的边与撤回集合。

- 存储：`declaration_revisions`（含 applied/invalid/stale 状态与拒绝原因）、
  `supersession_edges`、`version_withdrawals`、`declaration_conflicts`，与观测
  数据共用一个 SQLite 文件。
- 处置：相同修订重复接收不重复生效（duplicate）；同流同号不同内容记录冲突
  （conflict）；编号低于已见最新的晚到旧修订记录为 stale；跨产品连边、同一
  修订内部矛盾（自替代、双向替代）、成环的修订整体判 invalid——非法图不会
  部分改写当前可用结论。
- 未满足的关联：后继版本尚未采到时，替代边保留为 pending，前驱保持可用；
  后继到达后该边才生效。撤回后继不会让已被替代的前驱重新可用；多个未撤回
  末端并存时全部返回，不强挑唯一最新版。
- 一致性：声明更新与目录新增可交错发生；每次查询在单个读事务内完成，使用
  同一份已提交关系视图。所有结果都带 `view`（所用声明修订与各版本观测证据），
  应用修订返回可用末端的前后 diff，可解释纠正一条替代关系后哪些末端发生变化。

```bash
# 接收一条声明修订
PYTHONPATH=src python3 -m satellite_catalog.cli declare \
  --declaration fixtures/lineage/decl_rev1.json --database /tmp/satellite-catalog.sqlite3
# 查询可用末端版本 / 历史替代路径 / 无法确定原因
PYTHONPATH=src python3 -m satellite_catalog.cli available --product-id P-100 --database /tmp/satellite-catalog.sqlite3
PYTHONPATH=src python3 -m satellite_catalog.cli lineage  --product-id P-100 --version v1 --database /tmp/satellite-catalog.sqlite3
PYTHONPATH=src python3 -m satellite_catalog.cli explain  --product-id P-100 --version v1 --database /tmp/satellite-catalog.sqlite3
# 端到端演示（要求全新数据库路径）：从两个无关系版本开始，逐步补上替代、
# 分支、缺失目标与撤回声明，最后送入一条环和一条过期修订
PYTHONPATH=src python3 -m satellite_catalog.cli demo --database /tmp/lineage-demo.sqlite3
```

## HTTP source mode


The default fixture mode remains deterministic and credential-free. For a real JSON endpoint, use:

```bash
PYTHONPATH=src python3 -m satellite_catalog.cli ingest \
  --url https://example.invalid/api/records \
  --database /tmp/satellite-catalog.sqlite3 \
  --raw-dir /tmp/satellite-catalog-raw
```

Replace `satellite_catalog` and `satellite-catalog` with this repository's package/project name. The endpoint must return a JSON list, or an object containing `records`, `data`, `items`, or `results`. Use `--records-key` for a different nested path. Optional authentication is read from `DATA_SOURCE_BEARER_TOKEN` or `DATA_SOURCE_API_KEY`; do not commit their values. `--raw-dir` stores immutable content-addressed copies of successful HTTP responses. HTTP and JSON failures stop the run instead of being recorded as successful ingestion.

External dependency types and versions: Python 3.11+, pytest 8.3.x for tests. The HTTP adapter uses Python's standard library and adds no runtime package.
