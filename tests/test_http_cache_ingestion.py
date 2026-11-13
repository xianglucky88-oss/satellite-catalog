import sqlite3

from satellite_catalog.application import run_url
from test_http_cache import (
    CHANGED_200,
    ControlledServer,
    FULL_200,
    NOT_MODIFIED,
    NO_VALIDATOR_200,
)


def test_second_collection_persists_identical_verified_version(tmp_path):
    raw_dir = str(tmp_path / "raw")
    database = str(tmp_path / "catalog.sqlite3")
    with ControlledServer([FULL_200, NOT_MODIFIED]) as server:
        first = run_url(server.url + "/records", database, raw_dir=raw_dir)
        second = run_url(server.url + "/records", database, raw_dir=raw_dir)

    # A 304 in round two is a verified unchanged version, not zero products.
    assert first["processed"] == 1
    assert second["processed"] == 1
    assert second["observations"] == first["observations"]
    assert second["changed"] == 0
    assert second["rejected"] == first["rejected"]
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT record_key, payload_json FROM observations ORDER BY record_key"
        ).fetchall()
    finally:
        connection.close()
    assert rows, "a 304 must not yield an empty catalogue"
    assert all("P-1" in payload for _key, payload in rows)


def test_changed_response_is_ingested_normally(tmp_path):
    raw_dir = str(tmp_path / "raw")
    database = str(tmp_path / "catalog.sqlite3")
    with ControlledServer([FULL_200, CHANGED_200]) as server:
        first = run_url(server.url + "/records", database, raw_dir=raw_dir)
        second = run_url(server.url + "/records", database, raw_dir=raw_dir)

    # Cache only affects the download path; product values still flow through
    # normalization, deduplication and the sink unchanged.
    assert first["processed"] == 1
    assert second["processed"] == 1
    # Both payload versions are distinct observations (different observed_at),
    # which proves the changed record still flowed through the normal path.
    assert second["observations"] == 2
    # Same product_id+version key but different observed_at/source payload.
    connection = sqlite3.connect(database)
    try:
        payloads = connection.execute(
            "SELECT payload_json FROM observations ORDER BY observed_at"
        ).fetchall()
    finally:
        connection.close()
    assert any("P-2" in row[0] for row in payloads)


def test_missing_validators_keeps_legacy_full_download(tmp_path):
    raw_dir = str(tmp_path / "raw")
    database = str(tmp_path / "catalog.sqlite3")
    with ControlledServer([NO_VALIDATOR_200, NO_VALIDATOR_200]) as server:
        first = run_url(server.url + "/records", database, raw_dir=raw_dir)
        second = run_url(server.url + "/records", database, raw_dir=raw_dir)

    assert first["processed"] == second["processed"] == 1
    assert second["changed"] == 0


def test_unusable_snapshot_does_not_invent_empty_catalogue(tmp_path):
    raw_dir = str(tmp_path / "raw")
    database = str(tmp_path / "catalog.sqlite3")
    with ControlledServer([FULL_200, NOT_MODIFIED, CHANGED_200]) as server:
        run_url(server.url + "/records", database, raw_dir=raw_dir)
        for snapshot in (tmp_path / "raw").glob("*.json"):
            snapshot.write_bytes(b"")
        result = run_url(server.url + "/records", database, raw_dir=raw_dir)

    assert result["processed"] == 1
    assert result["observations"] >= 1


def test_cache_improvement_is_measured_in_downloaded_bytes(tmp_path):
    from satellite_catalog.sources import HttpJsonSource

    raw_dir = str(tmp_path / "raw")
    with ControlledServer([FULL_200, NOT_MODIFIED]) as server:
        source = HttpJsonSource(server.url + "/records", raw_dir=raw_dir)
        _records, first = source.read_with_info()
        _records_again, second = source.read_with_info()

    assert first.downloaded_bytes == len(FULL_200["body"])
    assert second.status == 304
    assert second.downloaded_bytes == 0
    assert second.reused_snapshot is True
    assert second.snapshot_sha256 == first.snapshot_sha256
