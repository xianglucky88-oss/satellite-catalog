"""版本沿革能力的测试：声明处置、替代图语义、快照查询与溯源。"""
import pytest

from satellite_catalog.lineage import DeclarationError, compute_availability, parse_declaration
from satellite_catalog.lineage_store import LineageStore
from satellite_catalog.models import Observation  # noqa: F401  (文档化导入，保持与旧测试一致)
from satellite_catalog.normalize import normalize
from satellite_catalog.storage import ObservationStore

PRODUCT = "P-100"
STREAM = "publisher-lineage"


def observe(database: str, product_id: str, version: str, observed_at: str = "2026-11-01T00:00:00Z") -> None:
    store = ObservationStore(database)
    try:
        store.upsert(normalize({
            "source": "catalog-api",
            "observed_at": observed_at,
            "payload": {
                "product_id": product_id,
                "platform": "SAT-A",
                "acquired_at": "2026-10-31T12:00:00Z",
                "cloud_cover": 1.0,
                "bbox": "1,2,3,4",
                "version": version,
            },
        }))
    finally:
        store.close()


def decl(revision, supersedes=None, withdrawn=None, *, stream=STREAM, product_id=PRODUCT):
    return {
        "stream": stream,
        "product_id": product_id,
        "revision": revision,
        "issued_at": "2026-11-02T00:00:00Z",
        "supersedes": supersedes or [],
        "withdrawn": withdrawn or [],
    }


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "lineage.sqlite3")


@pytest.fixture
def store(db):
    instance = LineageStore(db)
    yield instance
    instance.close()


def versions(section):
    return [entry["version"] for entry in section]


# ---------------------------------------------------------------- 基本语义


def test_versions_stay_parallel_without_declarations(db, store):
    observe(db, PRODUCT, "v1")
    observe(db, PRODUCT, "v2")
    result = store.available(PRODUCT)
    assert versions(result["available"]) == ["v1", "v2"]
    assert result["pending"] == []
    assert result["unavailable"] == []
    assert result["view"]["declarations"] == {}


def test_supersession_makes_predecessor_unavailable_with_provenance(db, store):
    observe(db, PRODUCT, "v1")
    observe(db, PRODUCT, "v2")
    outcome = store.apply(decl(1, supersedes=[{"successor": "v2", "predecessor": "v1"}]))
    assert outcome["status"] == "applied"
    assert outcome["diff"]["available_removed"] == ["v1"]

    result = store.available(PRODUCT)
    assert versions(result["available"]) == ["v2"]
    assert result["view"]["declarations"] == {STREAM: 1}
    unavailable = {entry["version"]: entry for entry in result["unavailable"]}
    assert unavailable["v1"]["reasons"] == [
        {"kind": "superseded", "by": "v2", "stream": STREAM, "revision": 1}
    ]
    # 可用版本带目录观测证据
    evidence = result["available"][0]["evidence"]["observations"]
    assert evidence == [{"source": "catalog-api", "observed_at": "2026-11-01T00:00:00Z"}]


def test_branching_one_successor_supersedes_multiple_predecessors(db, store):
    for version in ("v1", "v2", "v3"):
        observe(db, PRODUCT, version)
    store.apply(decl(1, supersedes=[
        {"successor": "v3", "predecessor": "v2"},
        {"successor": "v3", "predecessor": "v1"},
    ]))
    result = store.available(PRODUCT)
    assert versions(result["available"]) == ["v3"]
    # v1 的历史路径：被 v3 直接替代
    lineage = store.lineage(PRODUCT, "v1")
    assert [path["versions"] for path in lineage["paths_to_terminals"]] == [["v1", "v3"]]
    assert lineage["superseded_by"][0]["stream"] == STREAM


def test_multiple_unwithdrawn_terminals_are_all_returned(db, store):
    for version in ("v1", "v3", "v4"):
        observe(db, PRODUCT, version)
    # 分叉：v1 被两个后继替代，两个末端并存，不强挑唯一最新版
    store.apply(decl(1, supersedes=[
        {"successor": "v3", "predecessor": "v1"},
        {"successor": "v4", "predecessor": "v1"},
    ]))
    result = store.available(PRODUCT)
    assert versions(result["available"]) == ["v3", "v4"]
    lineage = store.lineage(PRODUCT, "v1")
    assert sorted(path["versions"][-1] for path in lineage["paths_to_terminals"]) == ["v3", "v4"]


def test_pending_successor_keeps_predecessor_until_observed(db, store):
    observe(db, PRODUCT, "v2")
    outcome = store.apply(decl(1, supersedes=[{"successor": "v9", "predecessor": "v2"}]))
    assert outcome["status"] == "applied"
    # v9 尚未采到：关联保留为未满足，v2 仍可用
    result = store.available(PRODUCT)
    assert versions(result["available"]) == ["v2"]
    assert result["available"][0]["evidence"]["pending_successors"] == [
        {"version": "v9", "stream": STREAM, "revision": 1}
    ]
    assert versions(result["pending"]) == ["v9"]
    # v9 到达后，关联生效，v2 退出可用集合
    observe(db, PRODUCT, "v9", observed_at="2026-11-03T00:00:00Z")
    result = store.available(PRODUCT)
    assert versions(result["available"]) == ["v9"]
    assert versions(result["pending"]) == []
    assert versions(result["unavailable"]) == ["v2"]


def test_withdrawn_successor_does_not_resurrect_predecessor(db, store):
    observe(db, PRODUCT, "v1")
    observe(db, PRODUCT, "v2")
    store.apply(decl(1, supersedes=[{"successor": "v2", "predecessor": "v1"}]))
    store.apply(decl(2, supersedes=[{"successor": "v2", "predecessor": "v1"}], withdrawn=["v2"]))
    result = store.available(PRODUCT)
    assert result["available"] == []
    reasons = {entry["version"]: entry["reasons"] for entry in result["unavailable"]}
    assert reasons["v2"] == [{"kind": "withdrawn", "stream": STREAM, "revision": 2}]
    # v1 仍被替代，不因后继撤回而重新可用
    assert reasons["v1"] == [{"kind": "superseded", "by": "v2", "stream": STREAM, "revision": 2}]
    # 后续修订解除撤回后 v2 恢复可用
    store.apply(decl(3, supersedes=[{"successor": "v2", "predecessor": "v1"}], withdrawn=[]))
    assert versions(store.available(PRODUCT)["available"]) == ["v2"]


# ---------------------------------------------------------------- 非法处置


def test_cross_product_edge_rejected_and_state_unchanged(db, store):
    observe(db, PRODUCT, "v1")
    observe(db, PRODUCT, "v2")
    bad = decl(1, supersedes=[{
        "successor": "v2",
        "predecessor": {"product_id": "P-999", "version": "v1"},
    }])
    outcome = store.apply(bad)
    assert outcome["status"] == "invalid"
    assert any("cross-product" in reason for reason in outcome["reasons"])
    # 已提交视图未被部分改写
    assert versions(store.available(PRODUCT)["available"]) == ["v1", "v2"]
    assert store.available(PRODUCT)["view"]["declarations"] == {}


def test_cycle_rejected_atomically(db, store):
    for version in ("v2", "v3", "v4"):
        observe(db, PRODUCT, version)
    store.apply(decl(1, supersedes=[
        {"successor": "v3", "predecessor": "v2"},
        {"successor": "v4", "predecessor": "v3"},
    ]))
    before = store.available(PRODUCT)
    outcome = store.apply(decl(2, supersedes=[
        {"successor": "v3", "predecessor": "v2"},
        {"successor": "v4", "predecessor": "v3"},
        {"successor": "v2", "predecessor": "v4"},
    ]))
    assert outcome["status"] == "invalid"
    assert any("cycle" in reason for reason in outcome["reasons"])
    # 非法图不能部分改写当前可用结论
    assert store.available(PRODUCT) == before
    # 旧边仍然有效
    assert versions(store.available(PRODUCT)["available"]) == ["v4"]


def test_contradictory_revision_rejected(db, store):
    observe(db, PRODUCT, "v1")
    observe(db, PRODUCT, "v2")
    mutual = decl(1, supersedes=[
        {"successor": "v2", "predecessor": "v1"},
        {"successor": "v1", "predecessor": "v2"},
    ])
    outcome = store.apply(mutual)
    assert outcome["status"] == "invalid"
    assert any("contradiction" in reason for reason in outcome["reasons"])

    self_edge = decl(2, supersedes=[{"successor": "v2", "predecessor": "v2"}])
    outcome = store.apply(self_edge)
    assert outcome["status"] == "invalid"
    assert any("supersedes itself" in reason for reason in outcome["reasons"])
    assert versions(store.available(PRODUCT)["available"]) == ["v1", "v2"]


def test_stale_revision_recorded_without_effect(db, store):
    observe(db, PRODUCT, "v1")
    observe(db, PRODUCT, "v2")
    store.apply(decl(10, supersedes=[{"successor": "v2", "predecessor": "v1"}]))
    # 晚到的旧修订（编号未见过但低于最新）：记录为 stale，不改变结论
    outcome = store.apply(decl(5, supersedes=[]))
    assert outcome["status"] == "stale"
    assert outcome["latest_revision"] == 10
    assert versions(store.available(PRODUCT)["available"]) == ["v2"]


def test_duplicate_revision_not_reapplied(db, store):
    observe(db, PRODUCT, "v1")
    observe(db, PRODUCT, "v2")
    first = store.apply(decl(1, supersedes=[{"successor": "v2", "predecessor": "v1"}]))
    assert first["status"] == "applied"
    second = store.apply(decl(1, supersedes=[{"successor": "v2", "predecessor": "v1"}]))
    assert second["status"] == "duplicate"
    assert second["recorded_status"] == "applied"
    count = store.conn.execute("SELECT COUNT(*) FROM supersession_edges").fetchone()[0]
    assert count == 1


def test_conflicting_redelivery_recorded(db, store):
    observe(db, PRODUCT, "v1")
    observe(db, PRODUCT, "v2")
    store.apply(decl(1, supersedes=[{"successor": "v2", "predecessor": "v1"}]))
    outcome = store.apply(decl(1, supersedes=[]))  # 同 stream+revision，内容不同
    assert outcome["status"] == "conflict"
    conflicts = store.conn.execute("SELECT COUNT(*) FROM declaration_conflicts").fetchone()[0]
    assert conflicts == 1
    assert versions(store.available(PRODUCT)["available"]) == ["v2"]


def test_stream_bound_to_one_product(db, store):
    store.apply(decl(1, product_id="P-100"))
    outcome = store.apply(decl(2, product_id="P-200"))
    assert outcome["status"] == "invalid"
    assert any("bound" in reason for reason in outcome["reasons"])


def test_unkeyable_declaration_returns_error(store):
    outcome = store.apply({"product_id": PRODUCT})
    assert outcome["status"] == "error"
    assert outcome["reasons"]


# ---------------------------------------------------------------- 纠正与解释


def test_correction_diff_explains_terminal_changes(db, store):
    for version in ("v1", "v2", "v3"):
        observe(db, PRODUCT, version)
    store.apply(decl(1, supersedes=[
        {"successor": "v2", "predecessor": "v1"},
        {"successor": "v3", "predecessor": "v2"},
    ]))
    assert versions(store.available(PRODUCT)["available"]) == ["v3"]
    # 纠正：v3 并不替代 v2
    outcome = store.apply(decl(2, supersedes=[{"successor": "v2", "predecessor": "v1"}]))
    assert outcome["status"] == "applied"
    assert outcome["diff"]["available_before"] == ["v3"]
    assert outcome["diff"]["available_after"] == ["v2", "v3"]
    assert outcome["diff"]["available_added"] == ["v2"]


def test_explain_unknown_and_pending(db, store):
    observe(db, PRODUCT, "v2")
    store.apply(decl(1, supersedes=[{"successor": "v9", "predecessor": "v2"}]))
    unknown = store.explain(PRODUCT, "v7")
    assert unknown["status"] == "unknown"
    assert unknown["reasons"][0]["kind"] == "unknown"

    pending = store.explain(PRODUCT, "v9")
    assert pending["status"] == "pending"
    assert any(reason["kind"] == "not_observed" for reason in pending["reasons"])

    kept = store.explain(PRODUCT, "v2")
    assert kept["status"] == "available"
    assert kept["pending_successors"] == [{"version": "v9", "stream": STREAM, "revision": 1}]


def test_lineage_paths_cover_full_history(db, store):
    for version in ("v1", "v2", "v3"):
        observe(db, PRODUCT, version)
    store.apply(decl(1, supersedes=[
        {"successor": "v2", "predecessor": "v1"},
        {"successor": "v3", "predecessor": "v2"},
    ]))
    lineage = store.lineage(PRODUCT, "v1")
    assert [path["versions"] for path in lineage["paths_to_terminals"]] == [["v1", "v2", "v3"]]
    step = lineage["paths_to_terminals"][0]["steps"][0]
    assert step["stream"] == STREAM and step["revision"] == 1 and step["status"] == "effective"

    lineage_v3 = store.lineage(PRODUCT, "v3")
    assert [path["versions"] for path in lineage_v3["paths_to_roots"]] == [["v3", "v2", "v1"]]
    assert {edge["predecessor"] for edge in lineage_v3["supersedes"]} == {"v1", "v2"}


# ---------------------------------------------------------------- 解析与纯函数


def test_parse_rejects_bad_schema():
    with pytest.raises(DeclarationError):
        parse_declaration({"stream": STREAM, "product_id": PRODUCT, "revision": 0, "issued_at": "x"})
    with pytest.raises(DeclarationError):
        parse_declaration("not an object")


def test_parse_normalizes_and_dedupes():
    declaration = parse_declaration({
        "stream": STREAM,
        "product_id": PRODUCT,
        "revision": 3,
        "issued_at": "2026-11-02T00:00:00Z",
        "supersedes": [
            {"successor": " v2 ", "predecessor": "v1"},
            {"successor": "v2", "predecessor": "v1"},
        ],
        "withdrawn": ["v0", "v0"],
    })
    assert len(declaration.edges) == 1
    assert declaration.edges[0].successor == "v2"
    assert declaration.withdrawn == ("v0",)


def test_compute_availability_is_pure_and_deterministic():
    from satellite_catalog.lineage import TaggedEdge, Withdrawal

    edges = [TaggedEdge("v2", "v1", "s", 1), TaggedEdge("v9", "v2", "s", 2)]
    summary = compute_availability({"v1", "v2"}, edges, [Withdrawal("v0", "s", 1)])
    assert summary["available"] == ["v2"]
    assert summary["pending"] == ["v9"]
    assert summary["pending_successors"]["v2"][0].successor == "v9"
