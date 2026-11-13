from __future__ import annotations

import hashlib
import http.client
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cache import CacheEntry, CacheIdentity, ResponseCache, SnapshotError, sha256_hex


class FixtureSource:
    """只负责从本地 fixture 读取原始记录，不做业务解析或持久化。"""

    def read(self, path: str) -> list[Any]:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError("fixture must contain a JSON list")
        return value


@dataclass(frozen=True)
class FetchInfo:
    """Transport facts for one ``read_with_info`` call.

    ``conditional_headers`` are the headers actually sent on the final
    attempt, ``downloaded_bytes`` counts body bytes received (a 304 body is
    empty), and ``reused_snapshot`` says the records came from a verified
    cached snapshot rather than the network.
    """

    status: int
    attempts: int
    conditional_headers: dict[str, str] = field(default_factory=dict)
    downloaded_bytes: int = 0
    reused_snapshot: bool = False
    snapshot_sha256: str = ""
    record_count: int = 0


class HttpJsonSource:
    """Reads JSON records over HTTP, revalidating cached responses when able.

    With ``raw_dir`` configured, every response that survives JSON and
    record-container validation is published to the cache together with the
    ETag/Last-Modified validators observed on *that* response. Subsequent
    requests send conditional headers only when the identity has a complete
    snapshot for the exact same URL, records container and access scope.

    A 304 is answered from the snapshot captured before that conditional
    request was sent. If that snapshot is missing, corrupt, or belongs to a
    different request, the response is fetched unconditionally once more (or
    the read fails explicitly); it is never reported as an empty catalogue.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 30.0,
        raw_dir: str | None = None,
        records_key: str | None = None,
        access_scope: str | None = None,
        bearer_token_env: str = "DATA_SOURCE_BEARER_TOKEN",
        api_key_env: str = "DATA_SOURCE_API_KEY",
    ) -> None:
        if not url.startswith(("http://", "https://")):
            raise ValueError("url must use http:// or https://")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if access_scope is not None and not access_scope.strip():
            raise ValueError("access_scope must be non-empty when provided")
        self.url = url
        self.timeout = timeout
        self.raw_dir = Path(raw_dir) if raw_dir else None
        # An empty container selection selects the same default path as no
        # key at all, so they must also share one cache identity.
        self.records_key = records_key if records_key and records_key.strip() else None
        self.access_scope = access_scope.strip() if access_scope is not None else None
        self.bearer_token_env = bearer_token_env
        self.api_key_env = api_key_env
        self.last_fetch: FetchInfo | None = None

    @property
    def cache(self) -> ResponseCache | None:
        return ResponseCache(self.raw_dir) if self.raw_dir is not None else None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "data-ingestion-adapter/0.1",
        }
        token = os.getenv(self.bearer_token_env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        api_key = os.getenv(self.api_key_env)
        if api_key:
            headers["X-API-Key"] = api_key
        return headers

    def _resolved_access_scope(self) -> str:
        """Explicit scopes win; otherwise credentials get a non-reversible tag.

        Caches must not be shared between an anonymous and an authenticated
        view of the same URL, nor between different credentials, unless the
        caller deliberately places them in one explicit scope.
        """
        if self.access_scope is not None:
            return self.access_scope
        token = os.getenv(self.bearer_token_env) or ""
        api_key = os.getenv(self.api_key_env) or ""
        if not token and not api_key:
            return "anonymous"
        material = hashlib.sha256(
            token.encode("utf-8") + b"\0" + api_key.encode("utf-8")
        ).hexdigest()
        return "credential:" + material[:16]

    def identity(self) -> CacheIdentity:
        return CacheIdentity(
            url=self.url,
            records_key=self.records_key,
            access_scope=self._resolved_access_scope(),
        )

    @staticmethod
    def _select_records(value: Any, records_key: str | None) -> list[Any]:
        selected = value
        if records_key:
            for part in records_key.split("."):
                if not isinstance(selected, dict) or part not in selected:
                    raise ValueError(f"records key not found: {records_key}")
                selected = selected[part]
        elif isinstance(selected, dict):
            for key in ("records", "data", "items", "results"):
                candidate = selected.get(key)
                if isinstance(candidate, list):
                    selected = candidate
                    break
        if not isinstance(selected, list):
            raise ValueError("source response must contain a JSON list of records")
        return selected

    def _parse_records(self, body: bytes) -> list[Any]:
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("source response is not valid UTF-8 JSON") from exc
        return self._select_records(value, self.records_key)

    @staticmethod
    def _header(headers: Any, name: str) -> str | None:
        value = headers.get(name) if headers is not None else None
        return value if isinstance(value, str) and value else None

    def read(self, _path: str | None = None) -> list[Any]:
        return self.read_with_info(_path)[0]

    def read_with_info(self, _path: str | None = None) -> tuple[list[Any], FetchInfo]:
        identity = self.identity()
        cache = self.cache
        # Capture the entry *before* sending the request. A concurrent writer
        # may publish a newer version while this request is in flight; a 304
        # must still be answered from the version this request validated.
        captured = cache.load(identity) if cache is not None else None
        if captured is not None and captured.has_validators():
            if not cache.snapshot_is_reusable(captured):
                # Validator metadata without a complete snapshot would invite
                # a 304 that cannot be answered. Forget it and fetch fresh.
                cache.invalidate(identity, captured)
                captured = None
        conditional = captured.conditional_headers() if captured is not None else {}
        if not conditional:
            captured = None
        records, info = self._fetch(
            identity,
            cache,
            captured,
            conditional,
            attempt=1,
            downloaded_so_far=0,
        )
        self.last_fetch = info
        return records, info

    def _fetch(
        self,
        identity: CacheIdentity,
        cache: ResponseCache | None,
        captured: CacheEntry | None,
        conditional: dict[str, str],
        *,
        attempt: int,
        downloaded_so_far: int,
    ) -> tuple[list[Any], FetchInfo]:
        headers = self._headers()
        headers.update(conditional)
        request = urllib.request.Request(self.url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                body = response.read()
                response_headers = response.headers
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                status = 304
                body = b""
                response_headers = exc.headers
            else:
                raise RuntimeError(f"source returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"source request failed: {exc.reason}") from exc
        except http.client.IncompleteRead as exc:
            # A partial download is a failed request, never a usable body.
            raise RuntimeError("source response was truncated") from exc

        downloaded = downloaded_so_far + len(body)

        if status != 304 and status != 200:
            # A 206 or similar is not a complete representation; caching it
            # would silently stand in for the full catalogue later.
            raise RuntimeError(f"source returned unexpected HTTP status {status}")

        if status == 304:
            if captured is None or not conditional:
                if attempt < 2:
                    # A 304 without a validator we sent cannot be tied to any
                    # snapshot. Ask once for the complete response instead of
                    # fabricating an empty result.
                    return self._fetch(
                        identity,
                        cache,
                        None,
                        {},
                        attempt=attempt + 1,
                        downloaded_so_far=downloaded,
                    )
                raise RuntimeError(
                    "source returned HTTP 304 without a reusable snapshot"
                )
            if cache is None:
                raise RuntimeError("cached snapshot is unavailable for HTTP 304")
            try:
                snapshot = cache.read_snapshot(captured)
                records = self._parse_records(snapshot)
                if len(records) != captured.record_count:
                    raise SnapshotError(
                        "cached snapshot record count does not match its entry"
                    )
            except (SnapshotError, ValueError) as exc:
                # Drop only this request's own entry; a newer pointer that a
                # concurrent collector published must stay untouched.
                cache.invalidate(identity, captured)
                if attempt < 2:
                    # The snapshot bound to this request is damaged or no
                    # longer validates. Re-fetch the complete response once;
                    # never publish a success marker for it and never return [].
                    return self._fetch(
                        identity,
                        cache,
                        None,
                        {},
                        attempt=attempt + 1,
                        downloaded_so_far=downloaded,
                    )
                raise RuntimeError(
                    f"cannot reuse cached snapshot after HTTP 304: {exc}"
                ) from exc
            return records, FetchInfo(
                status=304,
                attempts=attempt,
                conditional_headers=dict(conditional),
                downloaded_bytes=downloaded,
                reused_snapshot=True,
                snapshot_sha256=captured.snapshot_sha256,
                record_count=len(records),
            )

        # Any other successful response is a complete representation. Only
        # publish it after the existing JSON and container validation passes.
        records = self._parse_records(body)
        validators = {
            "etag": self._header(response_headers, "ETag"),
            "last_modified": self._header(response_headers, "Last-Modified"),
        }
        if cache is not None:
            cache.publish(
                identity,
                body,
                etag=validators["etag"],
                last_modified=validators["last_modified"],
                record_count=len(records),
            )
        return records, FetchInfo(
            status=status,
            attempts=attempt,
            conditional_headers=dict(conditional),
            downloaded_bytes=downloaded,
            reused_snapshot=False,
            snapshot_sha256=sha256_hex(body),
            record_count=len(records),
        )
