from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Bump whenever the persisted metadata layout changes. Unknown schemas are
# treated as a cache miss instead of being guessed at.
CACHE_SCHEMA = 1
INDEX_DIRECTORY = "_cache_entries"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _atomic_write(target: Path, data: bytes) -> None:
    """Write a complete file and publish it with one atomic rename.

    A reader (or a crash) can therefore observe either the previous complete
    file or the new complete file, never a half-written one. The temporary
    file is removed on failure.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    temp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        _fsync_directory(target.parent)
    except BaseException:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _fsync_directory(directory: Path) -> None:
    # Directory fsync is not available on every platform; durability is best
    # effort there while the atomic rename itself is still what protects
    # readers from partial files.
    try:
        handle = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


class SnapshotError(RuntimeError):
    """A cached snapshot cannot be trusted for the identity being reused."""


@dataclass(frozen=True)
class CacheIdentity:
    """Everything that makes two responses safely interchangeable."""

    url: str
    records_key: str | None
    access_scope: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "records_key": self.records_key,
            "access_scope": self.access_scope,
        }

    def key(self) -> str:
        return sha256_hex(_json_bytes(self.as_dict()))


@dataclass(frozen=True)
class CacheEntry:
    """Identity, validators and the complete raw response as one unit."""

    identity: CacheIdentity
    etag: str | None
    last_modified: str | None
    snapshot: str
    snapshot_sha256: str
    snapshot_size: int
    record_count: int
    stored_at: str
    snapshot_path: Path

    def conditional_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.last_modified:
            headers["If-Modified-Since"] = self.last_modified
        return headers

    def has_validators(self) -> bool:
        return bool(self.etag or self.last_modified)


class ResponseCache:
    """Persists validated raw bodies and validator metadata under raw_dir.

    Layout:
        <raw_dir>/<sha256(body)>.json          immutable snapshot, shared only
                                               through an identity-bound entry
        <raw_dir>/_cache_entries/<identity>.json
                                               pointer + validators + digest

    The pointer is written only after the snapshot was validated and atomically
    renamed into place. The entry repeats the full identity so a copied or
    stale entry cannot be used for a different URL, container selection or
    access scope.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.index_directory = self.directory / INDEX_DIRECTORY

    @property
    def index_path(self) -> Path:
        return self.index_directory

    def entry_path(self, identity: CacheIdentity) -> Path:
        return self.index_directory / f"{identity.key()}.json"

    def load(self, identity: CacheIdentity) -> CacheEntry | None:
        """Return a trustworthy metadata entry, or None for a cache miss.

        Corrupt metadata, schema drift and identity mismatches are misses. They
        must never become a "successful" empty catalogue.
        """
        path = self.entry_path(identity)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("schema") != CACHE_SCHEMA:
            return None
        if payload.get("identity") != identity.as_dict():
            return None
        etag = payload.get("etag")
        last_modified = payload.get("last_modified")
        snapshot = payload.get("snapshot")
        digest = payload.get("snapshot_sha256")
        size = payload.get("snapshot_size")
        record_count = payload.get("record_count")
        stored_at = payload.get("stored_at")
        if not (etag is None or isinstance(etag, str)):
            return None
        if not (last_modified is None or isinstance(last_modified, str)):
            return None
        if not isinstance(snapshot, str) or not isinstance(digest, str):
            return None
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return None
        if not isinstance(record_count, int) or isinstance(record_count, bool) or record_count < 0:
            return None
        if not isinstance(stored_at, str):
            return None
        # The filename is part of the digest contract; an entry whose pointer
        # was rewritten to another object is not reusable.
        if snapshot != f"{digest}.json":
            return None
        if Path(snapshot).name != snapshot:
            return None
        return CacheEntry(
            identity=identity,
            etag=etag,
            last_modified=last_modified,
            snapshot=snapshot,
            snapshot_sha256=digest,
            snapshot_size=size,
            record_count=record_count,
            stored_at=stored_at,
            snapshot_path=self.directory / snapshot,
        )

    def snapshot_is_reusable(self, entry: CacheEntry) -> bool:
        """Whether the snapshot is complete and matches the entry digest."""
        if Path(entry.snapshot).name != entry.snapshot:
            return False
        try:
            body = entry.snapshot_path.read_bytes()
        except (FileNotFoundError, OSError):
            return False
        return (
            len(body) == entry.snapshot_size
            and sha256_hex(body) == entry.snapshot_sha256
        )

    def read_snapshot(self, entry: CacheEntry) -> bytes:
        """Read and verify the complete raw response referenced by an entry."""
        if Path(entry.snapshot).name != entry.snapshot:
            raise SnapshotError("snapshot reference escapes the raw directory")
        try:
            body = entry.snapshot_path.read_bytes()
        except FileNotFoundError as exc:
            raise SnapshotError("cached snapshot is missing") from exc
        except OSError as exc:
            raise SnapshotError("cached snapshot cannot be read") from exc
        if len(body) != entry.snapshot_size:
            raise SnapshotError("cached snapshot size does not match its entry")
        if sha256_hex(body) != entry.snapshot_sha256:
            raise SnapshotError("cached snapshot digest does not match its entry")
        return body

    def publish(
        self,
        identity: CacheIdentity,
        body: bytes,
        *,
        etag: str | None,
        last_modified: str | None,
        record_count: int,
    ) -> CacheEntry:
        """Publish a validated response for later conditional requests.

        Callers must have parsed and selected records from *body* first. The
        snapshot is renamed into place before the pointer is atomically
        replaced, so an interrupted write cannot leave an entry pointing at a
        partial object.
        """
        digest = sha256_hex(body)
        snapshot = f"{digest}.json"
        target = self.directory / snapshot
        if not self._snapshot_is_complete(target, digest, len(body)):
            _atomic_write(target, body)

        entry = CacheEntry(
            identity=identity,
            etag=etag,
            last_modified=last_modified,
            snapshot=snapshot,
            snapshot_sha256=digest,
            snapshot_size=len(body),
            record_count=record_count,
            stored_at=datetime.now(timezone.utc).isoformat(),
            snapshot_path=target,
        )
        payload = {
            "schema": CACHE_SCHEMA,
            "identity": identity.as_dict(),
            "etag": entry.etag,
            "last_modified": entry.last_modified,
            "snapshot": entry.snapshot,
            "snapshot_sha256": entry.snapshot_sha256,
            "snapshot_size": entry.snapshot_size,
            "record_count": entry.record_count,
            "stored_at": entry.stored_at,
        }
        _atomic_write(self.entry_path(identity), _json_bytes(payload))
        return entry

    def invalidate(self, identity: CacheIdentity, entry: CacheEntry) -> bool:
        """Drop an entry only if it is still the exact one that failed.

        A concurrent writer may have published a newer entry for the same
        identity; that newer pointer must survive. Returning False means
        either the entry was already replaced or it was never there.
        """
        try:
            raw = self.entry_path(identity).read_bytes()
        except (FileNotFoundError, OSError):
            return False
        try:
            current_payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(current_payload, dict):
            return False
        if (
            current_payload.get("snapshot") != entry.snapshot
            or current_payload.get("snapshot_sha256") != entry.snapshot_sha256
            or current_payload.get("stored_at") != entry.stored_at
        ):
            return False
        try:
            self.entry_path(identity).unlink()
        except FileNotFoundError:
            return False
        _fsync_directory(self.index_directory)
        return True

    @staticmethod
    def _snapshot_is_complete(path: Path, digest: str, size: int) -> bool:
        try:
            body = path.read_bytes()
        except (FileNotFoundError, OSError):
            return False
        return len(body) == size and sha256_hex(body) == digest
