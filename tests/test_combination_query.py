import json
from pathlib import Path

import pytest

from satellite_catalog.application import query_combination, run_fixture
from satellite_catalog.selection import ResourceLimits
from satellite_catalog.storage import ObservationStore

FIXTURES = Path(__file__).parents[1] / "fixtures"
TARGET = [0, 0, 10, 10]
WINDOW = ("2026-10-01", "2026-10-31")


@pytest.fixture()
def catalog_db(tmp_path):
    db = str(tmp_path / "catalog.sqlite3")
    run_fixture(str(FIXTURES / "catalog_query.json"), db)
    return db


def _query(database, **overrides):
    params = dict(
        target_bbox=TARGET,
        acquired_after=WINDOW[0],
        acquired_before=WINDOW[1],
        max_cloud_cover=10.0,
        budget=3,
    )
    params.update(overrides)
    return query_combination(database, **params)


def _excluded_map(result):
    return {f"{item['product_id']}": item["reason"] for item in result["excluded"]}


def test_storage_sql_filters_time_window_and_cloud_cap(catalog_db):
    store = ObservationStore(catalog_db)
    try:
        rows = store.query_candidates(
            acquired_after="2026-10-01T00:00:00.000Z",
            acquired_before="2026-10-31T23:59:59.999Z",
            max_cloud_cover=10.0,
        )
        ids = {row["product_id"] for row in rows}
        # P-D 云量 95 超限、P-E 采集时间在 9 月，均在 SQL 阶段被过滤。
        assert "P-D" not in ids
        assert "P-E" not in ids
        # 重复来源的 P-A 两条记录都通过 SQL 筛选，去重留待几何/选择阶段。
        assert sum(1 for row in rows if row["product_id"] == "P-A") == 2
    finally:
        store.close()


def test_duplicate_sources_of_same_version_occupy_one_slot(catalog_db):
    result = _query(catalog_db, budget=1)
    selected_ids = [item["product_id"] for item in result["selected"]]
    assert selected_ids == ["P-A"]
    chosen = result["selected"][0]
    # 两个来源合并为一个候选，但两个来源都被保留可追溯。
    assert tuple(chosen["sources"]) == ("catalog-api", "catalog-web")
    assert chosen["version"] == "v1"
    assert result["candidates_considered"] == 4  # A/B/C/J 四个可靠候选


def test_overlap_is_counted_once_and_each_contribution_is_recomputable(catalog_db):
    result = _query(catalog_db, budget=2)
    selected_ids = [item["product_id"] for item in result["selected"]]
    # P-A 覆盖 60，P-B 新增 40（x 4..10 条带），合计 100；重叠的 4..6 条带不重复计数。
    assert selected_ids == ["P-A", "P-B"]
    contributions = {item["product_id"]: item for item in result["selected"]}
    assert contributions["P-A"]["contribution_area"] == pytest.approx(60.0)
    assert contributions["P-B"]["contribution_area"] == pytest.approx(40.0)
    assert result["covered_area"] == pytest.approx(100.0)
    assert result["uncovered_area"] == pytest.approx(0.0)
    assert result["uncovered"] == []
    # 逐幅贡献区域之和必须等于总覆盖面积（可复算性）。
    assert sum(item["contribution_area"] for item in result["selected"]) == pytest.approx(
        result["covered_area"]
    )


def test_edge_contact_only_is_not_eligible_coverage(catalog_db):
    result = _query(catalog_db)
    excluded = _excluded_map(result)
    # P-M 的 bbox 与目标只在经线 x=0 上接触，交集零面积，不可作为覆盖。
    assert excluded["P-M"] == "no_positive_overlap"


def test_missing_extent_record_is_unreliable_and_excluded(catalog_db):
    result = _query(catalog_db)
    assert _excluded_map(result)["P-F"] == "missing_or_invalid_extent"


def test_same_version_metadata_conflict_excludes_the_whole_group(catalog_db):
    result = _query(catalog_db)
    excluded = [item for item in result["excluded"] if item["product_id"] == "P-H"]
    assert {item["reason"] for item in excluded} == {"metadata_conflict"}
    assert all("bbox" in item["detail"] or "cloud_cover" in item["detail"] for item in excluded)
    assert all(item["product_id"] for item in result["selected"])
    assert "P-H" not in {item["product_id"] for item in result["selected"]}


def test_cloud_and_time_failures_never_reach_selection(catalog_db):
    result = _query(catalog_db)
    touched = {item["product_id"] for item in result["selected"]} | set(_excluded_map(result))
    # P-D/P-I 云量不合格、P-E 采集时间不在窗口内：在 SQL 阶段被过滤，
    # 既不会被选中，也不会出现在几何/选择阶段的排除清单里。
    assert "P-D" not in touched
    assert "P-E" not in touched
    assert "P-I" not in touched


def test_budget_shortfall_reports_uncovered_remainder(catalog_db):
    result = _query(catalog_db, budget=1)
    assert len(result["selected"]) == 1
    assert result["covered_area"] == pytest.approx(60.0)
    assert result["uncovered_area"] == pytest.approx(40.0)
    assert result["covered_fraction"] == pytest.approx(0.6)
    # 未覆盖部分是 x 6..10 的完整条带。
    assert result["uncovered"] == [[6.0, 0.0, 10.0, 10.0]]


def test_zero_budget_returns_empty_selection_with_full_target_uncovered(catalog_db):
    result = _query(catalog_db, budget=0)
    assert result["selected"] == []
    assert result["method"] == "exhaustive"
    assert result["optimal"] is True
    assert result["stop_reason"] == "budget_is_zero"
    assert result["uncovered_area"] == pytest.approx(100.0)


def test_exhaustive_search_is_marked_optimal_and_fully_covers_target(catalog_db):
    result = _query(catalog_db, budget=3)
    assert result["method"] == "exhaustive"
    assert result["optimal"] is True
    assert result["stopped_early"] is False
    assert result["stop_reason"] == "exhaustive_completed"
    assert result["covered_area"] == pytest.approx(100.0)
    # 覆盖目标只需 2 个产品，不应浪费第三个名额。
    assert len(result["selected"]) == 2


def test_candidate_limit_truncation_is_exposed_as_approximate(catalog_db):
    result = _query(
        catalog_db,
        budget=3,
        max_candidates=2,
        max_combinations=10**9,
    )
    assert result["method"] == "greedy"
    assert result["optimal"] is False
    assert result["stopped_early"] is True
    assert result["stop_reason"] == "candidate_limit_reached"
    assert result["candidates_used"] == 2
    assert result["candidates_considered"] == 4
    assert result["limits"]["max_candidates"] == 2


def test_combination_limit_falls_back_to_greedy_and_flags_approximation(catalog_db):
    result = _query(
        catalog_db,
        budget=3,
        max_candidates=10**9,
        max_combinations=6,  # 允许进入穷举，但枚举到第 7 个组合即触顶
    )
    assert result["method"] == "greedy"
    assert result["optimal"] is False
    assert result["stopped_early"] is True
    assert result["stop_reason"] == "combination_limit_reached"


def test_large_search_space_is_bounded_by_combination_limit(catalog_db):
    result = _query(
        catalog_db,
        budget=3,
        max_candidates=10**9,
        max_combinations=5,
    )
    # 候选数本身不大，未截断；穷举枚举数受 max_combinations 硬约束。
    assert result["candidates_used"] == 4
    assert result["method"] == "greedy"
    assert result["optimal"] is False
    assert result["stopped_early"] is True
    assert result["stop_reason"] == "combination_limit_reached"


def test_result_payload_is_json_serializable(catalog_db):
    result = _query(catalog_db)
    encoded = json.dumps(result, ensure_ascii=False)
    decoded = json.loads(encoded)
    assert decoded["target_bbox"] == [0.0, 0.0, 10.0, 10.0]
    assert decoded["time_window"]["acquired_after"].endswith("Z")


def test_query_is_deterministic_across_runs(catalog_db):
    first = json.dumps(_query(catalog_db, budget=2), sort_keys=True)
    second = json.dumps(_query(catalog_db, budget=2), sort_keys=True)
    assert first == second
    # 贪心路径同样必须可复现。
    greedy_a = json.dumps(_query(catalog_db, budget=2, max_candidates=2), sort_keys=True)
    greedy_b = json.dumps(_query(catalog_db, budget=2, max_candidates=2), sort_keys=True)
    assert greedy_a == greedy_b


def test_antimeridian_target_and_footprints(tmp_path):
    db = str(tmp_path / "antimeridian.sqlite3")
    run_fixture(str(FIXTURES / "catalog_antimeridian.json"), db)
    # 目标跨 180° 经线：[170,-10,-170,10]，拆成两侧各 10x20，总面积 400。
    result = query_combination(
        db,
        target_bbox=[170, -10, -170, 10],
        acquired_after="2026-10-01",
        acquired_before="2026-10-31",
        max_cloud_cover=10.0,
        budget=2,
    )
    selected = {item["product_id"]: item for item in result["selected"]}
    # P-K 覆盖东侧 170..180（100），P-L 覆盖西侧 -180..-170（100）。
    assert set(selected) == {"P-K", "P-L"}
    assert selected["P-K"]["contribution_area"] == pytest.approx(100.0)
    assert selected["P-L"]["contribution_area"] == pytest.approx(100.0)
    assert result["covered_area"] == pytest.approx(200.0)
    assert result["uncovered_area"] == pytest.approx(200.0)
    # P-K/P-L 只覆盖纬度 -5..5，未覆盖部分是分列日期变更线两侧的 4 个角块。
    assert result["uncovered"] == [
        [-180.0, -10.0, -170.0, -5.0],
        [-180.0, 5.0, -170.0, 10.0],
        [170.0, -10.0, 180.0, -5.0],
        [170.0, 5.0, 180.0, 10.0],
    ]

    # 预算 1 且允许云量放宽到 12 时，跨线产品 P-G 单独覆盖整条 175..-175 带（100）。
    wide = query_combination(
        db,
        target_bbox=[170, -10, -170, 10],
        acquired_after="2026-10-01",
        acquired_before="2026-10-31",
        max_cloud_cover=12.0,
        budget=1,
    )
    assert [item["product_id"] for item in wide["selected"]] == ["P-G"]
    g = wide["selected"][0]
    assert g["contribution_area"] == pytest.approx(100.0)
    # 贡献区域明确拆成跨线两侧的两块矩形，可逐块复算。
    assert len(g["contribution"]) == 2


def test_invalid_arguments_are_rejected(catalog_db):
    with pytest.raises(ValueError):
        _query(catalog_db, budget=-1)
    with pytest.raises(ValueError):
        _query(catalog_db, max_cloud_cover=-1.0)


def test_time_window_is_inclusive_and_dates_are_normalized_to_utc(catalog_db):
    result = _query(catalog_db)
    assert result["time_window"] == {
        "acquired_after": "2026-10-01T00:00:00.000Z",
        "acquired_before": "2026-10-31T23:59:59.999Z",
    }
    # 窗口边界上的 P-A（10-05）等记录仍在候选中，证明闭区间而非开区间。
    assert "P-A" in {item["product_id"] for item in result["selected"]}


def test_records_without_numeric_cloud_cover_never_leave_storage(tmp_path):
    from satellite_catalog.normalize import normalize
    from satellite_catalog.storage import ObservationStore

    db = str(tmp_path / "no_cloud.sqlite3")
    store = ObservationStore(db)
    try:
        payload = {
            "product_id": "P-NC",
            "platform": "SAT",
            "acquired_at": "2026-10-15T12:00:00Z",
            "bbox": "0,0,5,5",
            "version": "v1",
        }  # 无 cloud_cover 字段
        store.upsert(normalize(
            {"source": "s", "observed_at": "2026-11-01T00:00:00Z", "payload": payload}
        ))
        rows = store.query_candidates(
            acquired_after="2026-10-01T00:00:00.000Z",
            acquired_before="2026-10-31T23:59:59.999Z",
            max_cloud_cover=10.0,
        )
        assert rows == []
    finally:
        store.close()
