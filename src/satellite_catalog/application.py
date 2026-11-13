import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable

from .lineage_store import LineageStore
from .normalize import normalize
from .pipeline import IngestionPipeline
from .sources import FixtureSource, HttpJsonSource
from .storage import ObservationStore


def _run_records(records: Iterable[Any], database: str) -> dict[str, int]:
    """应用组合根：组装 source、pipeline 和 store，并管理资源生命周期。"""
    store = ObservationStore(database)
    try:
        return IngestionPipeline(records, normalize, store).run()
    finally:
        store.close()


def run_fixture(fixture: str, database: str) -> dict[str, int]:
    return _run_records(FixtureSource().read(fixture), database)


def run_url(
    url: str,
    database: str,
    *,
    timeout: float = 30.0,
    raw_dir: str | None = None,
    records_key: str | None = None,
) -> dict[str, int]:
    source = HttpJsonSource(url, timeout=timeout, raw_dir=raw_dir, records_key=records_key)
    return _run_records(source.read(), database)


# ---------------------------------------------------------------- 版本沿革


def apply_declaration(raw: Any, database: str) -> dict[str, Any]:
    """接收一条声明修订（已解析的 JSON 值）。"""
    store = LineageStore(database)
    try:
        return store.apply(raw)
    finally:
        store.close()


def apply_declaration_file(path: str, database: str) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return apply_declaration(raw, database)


def query_available(database: str, product_id: str) -> dict[str, Any]:
    store = LineageStore(database)
    try:
        return store.available(product_id)
    finally:
        store.close()


def query_lineage(database: str, product_id: str, version: str) -> dict[str, Any]:
    store = LineageStore(database)
    try:
        return store.lineage(product_id, version)
    finally:
        store.close()


def query_explain(database: str, product_id: str, version: str) -> dict[str, Any]:
    store = LineageStore(database)
    try:
        return store.explain(product_id, version)
    finally:
        store.close()


def run_demo(database: str, fixtures_dir: str | None = None) -> list[dict[str, Any]]:
    """演示版本沿革：从两个无关系版本开始，逐步补上替代、分支、缺失目标与
    撤回声明，最后送入一条环和一条过期修订。每一步都记录可用集合与历史
    路径，便于手工对照。要求使用全新数据库，保证步骤可重复。
    """
    if os.path.exists(database):
        raise ValueError(f"demo requires a fresh database path, already exists: {database}")
    base = Path(fixtures_dir) if fixtures_dir else Path(__file__).parents[2] / "fixtures" / "lineage"
    product = "P-100"
    steps: list[dict[str, Any]] = []

    def record(step: str, **data: Any) -> None:
        steps.append({"step": step, **data})

    def available() -> dict[str, Any]:
        return query_available(database, product)

    def lineage(version: str) -> dict[str, Any]:
        return query_lineage(database, product, version)

    def explain(version: str) -> dict[str, Any]:
        return query_explain(database, product, version)

    def declare(name: str) -> dict[str, Any]:
        return apply_declaration_file(str(base / name), database)

    record(
        "1. ingest two unrelated versions v1, v2 (no declarations yet)",
        ingestion=run_fixture(str(base / "catalog_1.json"), database),
        available=available(),
    )
    record(
        "2. declare rev1: v2 supersedes v1",
        declaration=declare("decl_rev1.json"),
        available=available(),
        lineage_v1=lineage("v1"),
    )
    record(
        "3. v3 arrives in the catalog (still no declaration about it)",
        ingestion=run_fixture(str(base / "catalog_2.json"), database),
        available=available(),
    )
    record(
        "4. declare rev2: v3 supersedes both v2 and v1 (branching)",
        declaration=declare("decl_rev2.json"),
        available=available(),
        lineage_v1=lineage("v1"),
    )
    record(
        "5. declare rev3: v4 supersedes v3, but v4 is not collected yet",
        declaration=declare("decl_rev3.json"),
        available=available(),
        explain_v3=explain("v3"),
    )
    record(
        "6. v4 arrives in the catalog; the pending edge now takes effect",
        ingestion=run_fixture(str(base / "catalog_3.json"), database),
        available=available(),
        explain_v3=explain("v3"),
    )
    record(
        "7. declare rev4: v4 is withdrawn; superseded v3 must not come back",
        declaration=declare("decl_rev4.json"),
        available=available(),
        explain_v3=explain("v3"),
        explain_v4=explain("v4"),
    )
    record(
        "8. declare rev5: correction drops v4->v3 and un-withdraws v4",
        declaration=declare("decl_rev5.json"),
        available=available(),
    )
    record(
        "9. declare rev6: introduces a cycle v3->v2->v4->v3 and is rejected",
        declaration=declare("decl_rev6_cycle.json"),
        available=available(),
    )
    record(
        "10. a late old revision (rev25, never recorded, below latest 60) is recorded as stale",
        declaration=declare("decl_rev25_late.json"),
        available=available(),
    )
    record(
        "11. re-deliver rev5 unchanged: duplicate, no repeated effect",
        declaration=declare("decl_rev5.json"),
        available=available(),
        lineage_v1=lineage("v1"),
    )
    return steps
