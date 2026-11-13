import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class ControlledServer:
    """Offline HTTP server whose response sequence and request log are test-controlled."""

    def __init__(self, responses, on_request=None):
        self.responses = list(responses)
        self.requests = []
        self.on_request = on_request
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                with outer._lock:
                    info = {
                        "path": self.path,
                        "if_none_match": self.headers.get("If-None-Match"),
                        "if_modified_since": self.headers.get("If-Modified-Since"),
                    }
                    outer.requests.append(info)
                    if outer.on_request is not None:
                        outer.on_request(info)
                    spec = outer.responses.pop(0) if outer.responses else outer.responses_default()
                status = spec.get("status", 200)
                headers = dict(spec.get("headers", {}))
                body = spec.get("body", b"")
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                if status != 304:
                    self.send_header("Content-Length", str(len(body)))
                elif "Content-Length" in headers:
                    self.send_header("Content-Length", headers.pop("Content-Length"))
                self.end_headers()
                if status != 304:
                    self.wfile.write(body)

            def log_message(self, *_args):
                return

        self._handler = Handler
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_port}"

    def responses_default(self):
        return {"status": 500, "body": b"unexpected extra request"}


def records_body(marker="1", count=1):
    return json.dumps(
        {
            "records": [
                {
                    "source": "http",
                    "observed_at": f"2026-11-0{marker}T00:00:00Z",
                    "payload": {
                        "product_id": f"P-{marker}",
                        "platform": "SAT-A",
                        "acquired_at": "2026-10-31T12:00:00Z",
                        "version": "v1",
                    },
                }
            ]
        },
        sort_keys=True,
    ).encode()


FULL_200 = {"status": 200, "headers": {"ETag": '"v1"'}, "body": records_body("1")}
NOT_MODIFIED = {"status": 304, "headers": {"ETag": '"v1"'}}
CHANGED_200 = {
    "status": 200,
    "headers": {"ETag": '"v2"'},
    "body": records_body("2"),
}
NO_VALIDATOR_200 = {"status": 200, "body": records_body("1")}


def test_first_response_is_cached_and_second_sends_conditional_headers(tmp_path):
    from satellite_catalog.sources import HttpJsonSource

    with ControlledServer([FULL_200, NOT_MODIFIED]) as server:
        source = HttpJsonSource(
            server.url + "/records?page=1&size=2", raw_dir=str(tmp_path / "raw")
        )
        first_records, first = source.read_with_info()
        second_records, second = source.read_with_info()

    assert first.status == 200
    assert first.reused_snapshot is False
    assert first.downloaded_bytes > 0
    assert len(first_records) == 1

    assert second.status == 304
    assert second.reused_snapshot is True
    assert second.downloaded_bytes == 0
    # The 304 supplies no body, but the caller still gets the verified version.
    assert second_records == first_records
    assert second.record_count == 1
    assert second.snapshot_sha256 == first.snapshot_sha256

    assert server.requests[0]["if_none_match"] is None
    assert server.requests[0]["path"] == "/records?page=1&size=2"
    assert server.requests[1]["if_none_match"] == '"v1"'
    assert server.requests[1]["if_modified_since"] is None


def test_last_modified_revalidation_without_etag(tmp_path):
    from satellite_catalog.sources import HttpJsonSource

    last_modified = "Wed, 12 Nov 2025 10:00:00 GMT"
    full = {
        "status": 200,
        "headers": {"Last-Modified": last_modified},
        "body": records_body("1"),
    }
    not_modified = {"status": 304, "headers": {"Last-Modified": last_modified}}
    with ControlledServer([full, not_modified]) as server:
        source = HttpJsonSource(server.url + "/records", raw_dir=str(tmp_path / "raw"))
        first_records, _ = source.read_with_info()
        second_records, second = source.read_with_info()

    assert server.requests[1]["if_modified_since"] == last_modified
    assert server.requests[1]["if_none_match"] is None
    assert second.status == 304
    assert second_records == first_records


def test_changed_content_refetches_and_publishes_new_snapshot(tmp_path):
    from satellite_catalog.sources import HttpJsonSource

    with ControlledServer([FULL_200, CHANGED_200, NOT_MODIFIED]) as server:
        source = HttpJsonSource(server.url + "/records", raw_dir=str(tmp_path / "raw"))
        source.read_with_info()
        changed_records, changed = source.read_with_info()
        reused_records, reused = source.read_with_info()

    assert changed.status == 200
    assert changed.reused_snapshot is False
    assert changed.downloaded_bytes > 0
    assert changed_records[0]["payload"]["product_id"] == "P-2"
    assert server.requests[1]["if_none_match"] == '"v1"'
    assert reused.status == 304
    assert reused_records == changed_records
    assert reused.snapshot_sha256 == changed.snapshot_sha256
    assert server.requests[2]["if_none_match"] == '"v2"'


def test_no_validators_keeps_legacy_unconditional_behaviour(tmp_path):
    from satellite_catalog.sources import HttpJsonSource

    with ControlledServer([NO_VALIDATOR_200, NO_VALIDATOR_200]) as server:
        source = HttpJsonSource(server.url + "/records", raw_dir=str(tmp_path / "raw"))
        first_records, first = source.read_with_info()
        second_records, second = source.read_with_info()

    assert first.status == 200 and second.status == 200
    assert server.requests[1]["if_none_match"] is None
    assert server.requests[1]["if_modified_since"] is None
    assert first_records == second_records
    # Full responses are still stored as immutable snapshots, as before.
    assert len(list((tmp_path / "raw").glob("*.json"))) >= 1


def test_url_parameters_form_distinct_cache_identities(tmp_path):
    from satellite_catalog.sources import HttpJsonSource

    with ControlledServer([FULL_200, CHANGED_200]) as server:
        source_a = HttpJsonSource(server.url + "/records?page=1", raw_dir=str(tmp_path / "raw"))
        source_b = HttpJsonSource(server.url + "/records?page=2", raw_dir=str(tmp_path / "raw"))
        source_a.read_with_info()
        source_b.read_with_info()

    # Different query means a different identity, so no validator is reused.
    assert server.requests[1]["if_none_match"] is None


def test_records_container_forms_distinct_cache_identities(tmp_path):
    from satellite_catalog.sources import HttpJsonSource

    with ControlledServer([FULL_200, NO_VALIDATOR_200]) as server:
        default_key = HttpJsonSource(server.url + "/records", raw_dir=str(tmp_path / "raw"))
        explicit_key = HttpJsonSource(
            server.url + "/records",
            raw_dir=str(tmp_path / "raw"),
            records_key="records",
        )
        default_key.read_with_info()
        explicit_key.read_with_info()

    assert server.requests[1]["if_none_match"] is None


def test_access_scope_is_not_crossed(tmp_path, monkeypatch):
    from satellite_catalog.sources import HttpJsonSource

    raw = str(tmp_path / "raw")
    with ControlledServer([FULL_200, NO_VALIDATOR_200]) as server:
        monkeypatch.delenv("DATA_SOURCE_BEARER_TOKEN", raising=False)
        monkeypatch.delenv("DATA_SOURCE_API_KEY", raising=False)
        anonymous = HttpJsonSource(server.url + "/records", raw_dir=raw)
        anonymous.read_with_info()

        monkeypatch.setenv("DATA_SOURCE_BEARER_TOKEN", "tenant-b-token")
        tenant_b = HttpJsonSource(server.url + "/records", raw_dir=raw)
        tenant_b.read_with_info()

    assert server.requests[1]["if_none_match"] is None


def test_explicit_access_scope_overrides_credential_derivation(tmp_path, monkeypatch):
    from satellite_catalog.sources import HttpJsonSource

    raw = str(tmp_path / "raw")
    with ControlledServer([FULL_200, NOT_MODIFIED]) as server:
        monkeypatch.setenv("DATA_SOURCE_BEARER_TOKEN", "tenant-b-token")
        first = HttpJsonSource(server.url + "/records", raw_dir=raw, access_scope="tenant-b")
        first.read_with_info()

        monkeypatch.setenv("DATA_SOURCE_BEARER_TOKEN", "rotated-token")
        second = HttpJsonSource(server.url + "/records", raw_dir=raw, access_scope="tenant-b")
        second.read_with_info()

    # Same explicit scope remains reusable even after credential rotation.
    assert server.requests[1]["if_none_match"] == '"v1"'


def test_missing_snapshot_pointer_is_dropped_before_sending_validators(tmp_path):
    from satellite_catalog.sources import HttpJsonSource

    with ControlledServer([FULL_200, CHANGED_200]) as server:
        source = HttpJsonSource(server.url + "/records", raw_dir=str(tmp_path / "raw"))
        source.read_with_info()
        entries = list((tmp_path / "raw" / "_cache_entries").glob("*.json"))
        assert entries
        for entry in entries:
            entry.unlink()

        records, info = source.read_with_info()

    # A pointer with no complete snapshot must not invite an unsatisfiable
    # 304: the request goes out unconditionally and gets the full version.
    assert server.requests[1]["if_none_match"] is None
    assert info.status == 200
    assert info.attempts == 1
    assert len(records) == 1
    assert records[0]["payload"]["product_id"] == "P-2"
    # Its validated response republished a usable entry.
    assert list((tmp_path / "raw" / "_cache_entries").glob("*.json"))


def test_304_with_corrupt_snapshot_refetches_and_republishes(tmp_path):
    from satellite_catalog.sources import HttpJsonSource

    with ControlledServer([FULL_200, NOT_MODIFIED, CHANGED_200, NOT_MODIFIED]) as server:
        source = HttpJsonSource(server.url + "/records", raw_dir=str(tmp_path / "raw"))
        source.read_with_info()
        for snapshot in (tmp_path / "raw").glob("*.json"):
            snapshot.write_bytes(b"truncated{")

        records, info = source.read_with_info()
        assert info.status == 200 and len(records) == 1

        records_again, info_again = source.read_with_info()

    assert server.requests[2]["if_none_match"] is None
    assert info_again.status == 304
    assert records_again == records
    assert server.requests[3]["if_none_match"] == '"v2"'


def test_304_without_any_sent_validator_is_not_empty(tmp_path):
    from satellite_catalog.sources import HttpJsonSource

    # First request yields no validators; second request is unconditionally
    # answered 304, which cannot be tied to any snapshot.
    with ControlledServer([NO_VALIDATOR_200, NOT_MODIFIED, CHANGED_200]) as server:
        source = HttpJsonSource(server.url + "/records", raw_dir=str(tmp_path / "raw"))
        source.read_with_info()
        records, info = source.read_with_info()

    assert server.requests[1]["if_none_match"] is None
    assert server.requests[2]["if_none_match"] is None
    assert info.status == 200 and len(records) == 1
    assert records[0]["payload"]["product_id"] == "P-2"


def test_empty_json_list_is_a_valid_response_not_a_304_fallback(tmp_path):
    from satellite_catalog.sources import HttpJsonSource

    empty = {"status": 200, "headers": {"ETag": '"empty"'}, "body": b'{"records": []}'}
    with ControlledServer([empty]) as server:
        source = HttpJsonSource(server.url + "/records", raw_dir=str(tmp_path / "raw"))
        records, info = source.read_with_info()

    assert records == []
    assert info.status == 200
    assert info.record_count == 0


def test_304_with_never_sent_validator_fails_if_refetch_is_impossible(tmp_path):
    from satellite_catalog.sources import HttpJsonSource

    never_only = {"status": 304, "headers": {"ETag": '"never"'}}
    with ControlledServer([never_only, never_only]) as server:
        source = HttpJsonSource(server.url + "/records", raw_dir=str(tmp_path / "raw"))
        with pytest.raises(RuntimeError, match="304"):
            source.read_with_info()

    assert server.requests[1]["if_none_match"] is None


def test_concurrent_new_response_and_stale_304_keep_metadata_bound(tmp_path):
    from satellite_catalog.cache import ResponseCache
    from satellite_catalog.sources import HttpJsonSource

    raw_dir = tmp_path / "raw"
    source = HttpJsonSource(
        # Patched below once the ephemeral port is known.
        "http://127.0.0.1:1/records",
        raw_dir=str(raw_dir),
    )

    with ControlledServer([FULL_200, NOT_MODIFIED]) as server:
        source.url = server.url + "/records"
        source.read_with_info()  # snapshot/validators '"v1"'

        cache = ResponseCache(raw_dir)
        captured = cache.load(source.identity())
        assert captured.etag == '"v1"'

        def concurrent_collector_publishes_v2(request_info):
            # Only act on the stale collector's conditional request. While it
            # is in flight, another collector completes and publishes '"v2"'.
            if request_info["if_none_match"] == '"v1"' and not hasattr(
                concurrent_collector_publishes_v2, "done"
            ):
                concurrent_collector_publishes_v2.done = True
                cache.publish(
                    source.identity(),
                    records_body("2"),
                    etag='"v2"',
                    last_modified=None,
                    record_count=1,
                )

        server.on_request = concurrent_collector_publishes_v2
        records, info = source.read_with_info()

    # The 304 answers for the validator actually sent ('"v1"'): the caller gets
    # the complete v1 snapshot captured before the request, not v2 and not [].
    assert info.status == 304
    assert info.reused_snapshot is True
    assert info.snapshot_sha256 == captured.snapshot_sha256
    assert records[0]["payload"]["product_id"] == "P-1"
    # The concurrent collector's metadata is not overwritten by stale validators.
    current = ResponseCache(raw_dir).load(source.identity())
    assert current is not None
    assert current.etag == '"v2"'
    assert current.snapshot_sha256 != captured.snapshot_sha256
    # The next request revalidates against the newer version.
    assert cache.read_snapshot(current).find(b'"P-2"') != -1


def test_interrupted_publication_leaves_no_dangling_pointer(tmp_path, monkeypatch):
    from satellite_catalog import cache as cache_module
    from satellite_catalog.cache import CacheIdentity, ResponseCache
    from satellite_catalog.sources import HttpJsonSource

    raw_dir = tmp_path / "raw"
    identity = CacheIdentity("http://example.invalid/r", None, "anonymous")
    response_cache = ResponseCache(raw_dir)

    real_replace = cache_module.os.replace
    calls = {"count": 0}

    def replace_then_crash(source_path, target_path):
        calls["count"] += 1
        if calls["count"] == 1:
            return real_replace(source_path, target_path)  # snapshot lands
        raise OSError("simulated crash between snapshot and pointer")

    # Snapshot write succeeds, pointer rename is interrupted.
    monkeypatch.setattr(cache_module.os, "replace", replace_then_crash)
    with pytest.raises(OSError, match="simulated crash"):
        response_cache.publish(
            identity,
            records_body("1"),
            etag='"v1"',
            last_modified=None,
            record_count=1,
        )
    monkeypatch.undo()

    # The complete snapshot exists, but no pointer claims it: nothing that a
    # later 304 could resolve against.
    assert list(raw_dir.glob("*.json"))
    assert response_cache.load(identity) is None
    with ControlledServer([FULL_200, NOT_MODIFIED]) as server:
        source = HttpJsonSource(server.url + "/records", raw_dir=str(raw_dir))
        records, info = source.read_with_info()
        assert info.status == 200 and len(records) == 1
        records_again, info_again = source.read_with_info()
        assert info_again.status == 304
        assert records_again == records

    # Temporary files never masquerade as index entries.
    assert not list(raw_dir.glob(".*.tmp"))
    assert not list((raw_dir / "_cache_entries").glob("*.tmp"))


def test_pointer_identity_mismatch_is_treated_as_miss(tmp_path):
    from satellite_catalog.cache import CacheIdentity, ResponseCache
    from satellite_catalog.sources import HttpJsonSource

    raw_dir = tmp_path / "raw"
    with ControlledServer([FULL_200, NO_VALIDATOR_200]) as server:
        source = HttpJsonSource(server.url + "/records", raw_dir=str(raw_dir))
        source.read_with_info()

        cache = ResponseCache(raw_dir)
        identity = source.identity()
        entry_path = cache.entry_path(identity)
        payload = json.loads(entry_path.read_text())
        payload["identity"]["url"] = server.url + "/other"
        entry_path.write_text(json.dumps(payload))

        assert ResponseCache(raw_dir).load(CacheIdentity(server.url + "/other", None, "anonymous")) is None
        source.read_with_info()

    assert server.requests[1]["if_none_match"] is None


def test_existing_raw_dir_with_legacy_dumps_is_adopted_after_validation(tmp_path):
    from satellite_catalog.sources import HttpJsonSource

    raw = tmp_path / "raw"
    raw.mkdir()
    # Simulate a raw_dir written by the previous version: immutable bodies but
    # no validator-bound pointers.
    (raw / ("f" * 64 + ".json")).write_bytes(b'{"legacy": true}')

    with ControlledServer([FULL_200, NOT_MODIFIED]) as server:
        source = HttpJsonSource(server.url + "/records", raw_dir=str(raw))
        records, first = source.read_with_info()
        # Legacy dumps carry no validators, so the first request is
        # unconditional and its validated response is now cached.
        assert server.requests[0]["if_none_match"] is None
        records_again, second = source.read_with_info()

    assert second.status == 304
    assert records_again == records
    assert (raw / ("f" * 64 + ".json")).exists()


def test_snapshot_digest_mismatch_blocks_reuse(tmp_path):
    from satellite_catalog.cache import CacheEntry, CacheIdentity, ResponseCache, SnapshotError

    directory = tmp_path / "raw"
    cache = ResponseCache(directory)
    entry = CacheEntry(
        identity=CacheIdentity("http://example.invalid/r", None, "anonymous"),
        etag='"v1"',
        last_modified=None,
        snapshot="deadbeef.json",
        snapshot_sha256="deadbeef",
        snapshot_size=10,
        record_count=1,
        stored_at="now",
        snapshot_path=directory / "deadbeef.json",
    )
    assert cache.snapshot_is_reusable(entry) is False
    with pytest.raises(SnapshotError):
        cache.read_snapshot(entry)
