from collections.abc import Iterable
from typing import Any

from .normalize import normalize
from .pipeline import IngestionPipeline
from .query import run_combination_query
from .selection import ResourceLimits
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


def query_combination(
    database: str,
    *,
    target_bbox: Any,
    acquired_after: str,
    acquired_before: str,
    max_cloud_cover: float,
    budget: int,
    max_candidates: int | None = None,
    max_combinations: int | None = None,
) -> dict[str, Any]:
    """应用组合根：在已入库的 SQLite 目录上打开只读会话并执行组合查询。"""
    kwargs: dict[str, int] = {}
    if max_candidates is not None:
        kwargs["max_candidates"] = max_candidates
    if max_combinations is not None:
        kwargs["max_combinations"] = max_combinations
    limits = ResourceLimits(**kwargs)
    store = ObservationStore(database)
    try:
        return run_combination_query(
            store,
            target_bbox=target_bbox,
            acquired_after=acquired_after,
            acquired_before=acquired_before,
            max_cloud_cover=max_cloud_cover,
            budget=budget,
            limits=limits,
        )
    finally:
        store.close()
