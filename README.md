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
PYTHONPATH=src python3 -m satellite_catalog.cli --fixture fixtures/sample.json --database /tmp/satellite-catalog.sqlite3
PYTHONPATH=src python3 -m pytest tests
```

## HTTP source mode


The default fixture mode remains deterministic and credential-free. For a real JSON endpoint, use:

```bash
PYTHONPATH=src python3 -m satellite_catalog.cli \
  --url https://example.invalid/api/records \
  --database /tmp/satellite-catalog.sqlite3 \
  --raw-dir /tmp/satellite-catalog-raw
```

Replace `satellite_catalog` and `satellite-catalog` with this repository's package/project name. The endpoint must return a JSON list, or an object containing `records`, `data`, `items`, or `results`. Use `--records-key` for a different nested path. Optional authentication is read from `DATA_SOURCE_BEARER_TOKEN` or `DATA_SOURCE_API_KEY`; do not commit their values. `--raw-dir` stores immutable content-addressed copies of successful HTTP responses. HTTP and JSON failures stop the run instead of being recorded as successful ingestion.

### Conditional requests and the raw-dir cache

When `--raw-dir` is set, each response that passes JSON and record-container validation is persisted with the `ETag` / `Last-Modified` validators observed on that exact response. Later runs reuse them as `If-None-Match` / `If-Modified-Since`:

- A `304 Not Modified` is resolved from the complete raw snapshot captured **before** the conditional request was sent, so the caller still receives the same verified records — never an empty catalogue and never a success marker for a snapshot that does not exist.
- A damaged, missing or identity-mismatched snapshot triggers one unconditional refetch; if that is impossible the run fails loudly instead of ingesting `[]`.
- Sources that send no validators keep the original unconditional behaviour; full responses are still stored as immutable raw copies.
- Cache identity is the exact request URL (query parameters included) plus `--records-key` and an access scope. The scope is explicit via `--access-scope`, or derived from the configured credentials; caches are never reused across scopes, and an anonymous and an authenticated view do not share one.
- Snapshot and metadata are written atomically and published as a unit: a crash between them leaves a complete snapshot with no pointer, never a pointer to a partial file. Concurrent collectors may capture older metadata, but a `304` about that validator only reuses the matching snapshot and does not overwrite newer cache metadata.
- The cache only affects downloading and source reads. Product versions, geometry filtering and combination logic are unchanged.

Existing `--raw-dir` contents from earlier runs are left untouched; because those raw dumps carry no validator metadata, they are not trusted as conditional-cache entries until a full response has been fetched and validated again. Validator-bound entries live in `<raw-dir>/_cache_entries/` and reference the same content-addressed `<sha256>.json` snapshots.

External dependency types and versions: Python 3.11+, pytest 8.3.x for tests. The HTTP adapter uses Python's standard library and adds no runtime package.
