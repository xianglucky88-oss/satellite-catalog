"""版本沿革的 SQLite 存储：声明修订、替代边、撤回记录的持久化与快照查询。

与 ObservationStore 共用一个数据库文件，但表相互独立。写入（apply）在单个
事务内完成校验与替换，非法修订整体拒绝，不会部分改写当前可用结论；查询在
单个读事务内完成，保证一次查询看到同一份已提交关系视图。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .lineage import (
    Declaration,
    DeclarationError,
    Edge,
    TaggedEdge,
    Withdrawal,
    compute_availability,
    compute_lineage,
    declaration_key,
    find_cycle,
    parse_declaration,
    raw_payload_hash,
)

EVIDENCE_LIMIT = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS declaration_revisions (
    stream TEXT NOT NULL,
    revision INTEGER NOT NULL,
    product_id TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (stream, revision)
);
CREATE TABLE IF NOT EXISTS declaration_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream TEXT NOT NULL,
    revision INTEGER NOT NULL,
    existing_hash TEXT NOT NULL,
    received_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS supersession_edges (
    stream TEXT NOT NULL,
    product_id TEXT NOT NULL,
    successor TEXT NOT NULL,
    predecessor TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY (stream, successor, predecessor)
);
CREATE TABLE IF NOT EXISTS version_withdrawals (
    stream TEXT NOT NULL,
    product_id TEXT NOT NULL,
    version TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY (stream, version)
);
"""


class LineageStore:
    """只负责声明修订的接收处置、替代图存储和一致性快照查询。"""

    def __init__(self, database: str):
        self.conn = sqlite3.connect(database)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------ apply

    def apply(self, raw: Any) -> dict[str, Any]:
        """接收一条声明修订。处置结果：applied / duplicate / conflict / stale /
        invalid / error。相同修订重复接收不重复生效；旧修订晚到被记录为 stale；
        非法修订整体拒绝，已提交视图不变。
        """
        try:
            declaration = parse_declaration(raw)
        except DeclarationError as exc:
            key = declaration_key(raw)
            if key is None:
                return {"status": "error", "reasons": exc.reasons}
            stream, revision = key
            product_id = raw.get("product_id") if isinstance(raw, dict) else ""
            product_id = " ".join(str(product_id).split()) if isinstance(product_id, str) else ""
            return self._record_unappliable(
                stream, product_id or "", revision, raw, raw_payload_hash(raw), exc.reasons
            )
        return self._apply_declaration(declaration)

    def _record_unappliable(
        self,
        stream: str,
        product_id: str,
        revision: int,
        raw: Any,
        payload_hash: str,
        reasons: list[str],
    ) -> dict[str, Any]:
        """结构非法但可键控的声明：占用 (stream, revision) 槽位并落审计行。"""
        payload_json = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.conn.execute(
                "SELECT payload_hash, status FROM declaration_revisions WHERE stream = ? AND revision = ?",
                (stream, revision),
            ).fetchone()
            if existing is not None:
                result = self._handle_redelivery(stream, revision, existing, payload_hash, payload_json)
                self.conn.commit()
                return result
            latest = self._latest_revision_locked(stream)
            if latest is not None and revision < latest:
                self._insert_revision_locked(
                    stream, product_id, revision, "", payload_hash, "stale", reasons, payload_json
                )
                self.conn.commit()
                return {"status": "stale", "stream": stream, "revision": revision, "latest_revision": latest}
            self._insert_revision_locked(
                stream, product_id, revision, "", payload_hash, "invalid", reasons, payload_json
            )
            self.conn.commit()
            return {"status": "invalid", "stream": stream, "revision": revision, "reasons": reasons}
        except Exception:
            self.conn.rollback()
            raise

    def _apply_declaration(self, declaration: Declaration) -> dict[str, Any]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.conn.execute(
                "SELECT payload_hash, status FROM declaration_revisions WHERE stream = ? AND revision = ?",
                (declaration.stream, declaration.revision),
            ).fetchone()
            if existing is not None:
                result = self._handle_redelivery(
                    declaration.stream, declaration.revision, existing,
                    declaration.payload_hash, declaration.canonical_json,
                )
                self.conn.commit()
                return result

            latest = self._latest_revision_locked(declaration.stream)
            if latest is not None and declaration.revision < latest:
                self._insert_revision_locked(
                    declaration.stream, declaration.product_id, declaration.revision,
                    declaration.issued_at, declaration.payload_hash, "stale",
                    ["revision is older than the latest recorded revision"],
                    declaration.canonical_json,
                )
                self.conn.commit()
                return {
                    "status": "stale",
                    "stream": declaration.stream,
                    "revision": declaration.revision,
                    "latest_revision": latest,
                }

            bound = self.conn.execute(
                "SELECT DISTINCT product_id FROM declaration_revisions WHERE stream = ? AND status = 'applied'",
                (declaration.stream,),
            ).fetchall()
            bound_products = {row[0] for row in bound}
            if bound_products and declaration.product_id not in bound_products:
                reasons = [
                    f"stream {declaration.stream!r} is already bound to product "
                    f"{sorted(bound_products)[0]!r}; one stream serves one product"
                ]
                self._insert_revision_locked(
                    declaration.stream, declaration.product_id, declaration.revision,
                    declaration.issued_at, declaration.payload_hash, "invalid", reasons,
                    declaration.canonical_json,
                )
                self.conn.commit()
                return {"status": "invalid", "stream": declaration.stream,
                        "revision": declaration.revision, "reasons": reasons}

            # 环检测：本流的边将被整体替换，其余流的已提交边保持不变。
            other_edges = [
                Edge(successor=row[0], predecessor=row[1])
                for row in self.conn.execute(
                    "SELECT successor, predecessor FROM supersession_edges WHERE product_id = ? AND stream <> ?",
                    (declaration.product_id, declaration.stream),
                )
            ]
            cycle = find_cycle(other_edges + list(declaration.edges))
            if cycle is not None:
                reasons = ["cycle detected: " + " -> ".join(cycle)]
                self._insert_revision_locked(
                    declaration.stream, declaration.product_id, declaration.revision,
                    declaration.issued_at, declaration.payload_hash, "invalid", reasons,
                    declaration.canonical_json,
                )
                self.conn.commit()
                return {"status": "invalid", "stream": declaration.stream,
                        "revision": declaration.revision, "reasons": reasons}

            before = self._availability_locked(declaration.product_id)

            self.conn.execute(
                "DELETE FROM supersession_edges WHERE stream = ?", (declaration.stream,)
            )
            self.conn.executemany(
                "INSERT INTO supersession_edges(stream, product_id, successor, predecessor, revision)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (declaration.stream, declaration.product_id, edge.successor, edge.predecessor, declaration.revision)
                    for edge in declaration.edges
                ],
            )
            self.conn.execute(
                "DELETE FROM version_withdrawals WHERE stream = ?", (declaration.stream,)
            )
            self.conn.executemany(
                "INSERT INTO version_withdrawals(stream, product_id, version, revision) VALUES (?, ?, ?, ?)",
                [
                    (declaration.stream, declaration.product_id, version, declaration.revision)
                    for version in declaration.withdrawn
                ],
            )
            self._insert_revision_locked(
                declaration.stream, declaration.product_id, declaration.revision,
                declaration.issued_at, declaration.payload_hash, "applied", [],
                declaration.canonical_json,
            )

            after = self._availability_locked(declaration.product_id)
            self.conn.commit()
            return {
                "status": "applied",
                "stream": declaration.stream,
                "revision": declaration.revision,
                "product_id": declaration.product_id,
                "diff": _diff_availability(before, after),
            }
        except Exception:
            self.conn.rollback()
            raise

    def _handle_redelivery(
        self,
        stream: str,
        revision: int,
        existing: tuple[str, str],
        payload_hash: str,
        payload_json: str,
    ) -> dict[str, Any]:
        existing_hash, recorded_status = existing
        if existing_hash == payload_hash:
            # 重复接收相同修订：不重复生效。
            return {
                "status": "duplicate",
                "stream": stream,
                "revision": revision,
                "recorded_status": recorded_status,
            }
        self.conn.execute(
            "INSERT INTO declaration_conflicts(stream, revision, existing_hash, received_hash, payload_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (stream, revision, existing_hash, payload_hash, payload_json),
        )
        return {
            "status": "conflict",
            "stream": stream,
            "revision": revision,
            "reasons": ["same stream and revision arrived with different content; first delivery kept"],
        }

    def _latest_revision_locked(self, stream: str) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(revision) FROM declaration_revisions WHERE stream = ?", (stream,)
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def _insert_revision_locked(
        self,
        stream: str,
        product_id: str,
        revision: int,
        issued_at: str,
        payload_hash: str,
        status: str,
        detail: list[str],
        payload_json: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO declaration_revisions"
            "(stream, revision, product_id, issued_at, payload_hash, status, detail_json, payload_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (stream, revision, product_id, issued_at, payload_hash, status,
             json.dumps(detail, ensure_ascii=False), payload_json),
        )

    # ---------------------------------------------------------------- queries

    def available(self, product_id: str) -> dict[str, Any]:
        """可用末端版本查询：单事务快照，结果可指回声明与目录证据。"""
        self.conn.execute("BEGIN")
        try:
            result = self._availability_locked(product_id)
            self.conn.commit()
            return result
        except Exception:
            self.conn.rollback()
            raise

    def lineage(self, product_id: str, version: str) -> dict[str, Any]:
        """历史替代路径查询：单事务快照。"""
        self.conn.execute("BEGIN")
        try:
            observed = self._observed_versions_locked(product_id)
            edges = self._edges_locked(product_id)
            withdrawn = self._withdrawn_locked(product_id)
            availability = compute_availability(observed, edges, withdrawn)
            paths = compute_lineage(edges, version, observed)
            result = {
                "product_id": product_id,
                "view": self._view_locked(product_id),
                "status": _status_of(version, observed, edges, withdrawn, availability),
                "reasons": availability["reasons"].get(version, []),
                **paths,
            }
            self.conn.commit()
            return result
        except Exception:
            self.conn.rollback()
            raise

    def explain(self, product_id: str, version: str) -> dict[str, Any]:
        """无法确定/不可用原因查询：单事务快照。"""
        self.conn.execute("BEGIN")
        try:
            observed = self._observed_versions_locked(product_id)
            edges = self._edges_locked(product_id)
            withdrawn = self._withdrawn_locked(product_id)
            availability = compute_availability(observed, edges, withdrawn)
            reasons = list(availability["reasons"].get(version, []))
            pending_successors = [
                {"version": edge.successor, "stream": edge.stream, "revision": edge.revision}
                for edge in availability["pending_successors"].get(version, [])
            ]
            status = _status_of(version, observed, edges, withdrawn, availability)
            if status == "unknown":
                reasons.append({
                    "kind": "unknown",
                    "detail": "no catalog observation and no declaration references this version",
                })
            if status == "pending":
                reasons.append({
                    "kind": "not_observed",
                    "detail": "declared as a successor but not yet observed in the catalog",
                })
            result = {
                "product_id": product_id,
                "version": version,
                "view": self._view_locked(product_id),
                "status": status,
                "observed": version in observed,
                "observations": self._observation_evidence_locked(product_id, version),
                "reasons": reasons,
                "pending_successors": pending_successors,
            }
            self.conn.commit()
            return result
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------- internals

    def _availability_locked(self, product_id: str) -> dict[str, Any]:
        observed = self._observed_versions_locked(product_id)
        edges = self._edges_locked(product_id)
        withdrawn = self._withdrawn_locked(product_id)
        summary = compute_availability(observed, edges, withdrawn)
        return {
            "product_id": product_id,
            "view": self._view_locked(product_id),
            "available": [
                {
                    "version": version,
                    "evidence": {
                        "observations": self._observation_evidence_locked(product_id, version),
                        "pending_successors": [
                            {"version": edge.successor, "stream": edge.stream, "revision": edge.revision}
                            for edge in summary["pending_successors"].get(version, [])
                        ],
                    },
                }
                for version in summary["available"]
            ],
            "pending": [
                {
                    "version": version,
                    "reason": "declared as a successor but not yet observed in the catalog",
                    "declared_by": [
                        {"stream": edge.stream, "revision": edge.revision}
                        for edge in edges
                        if edge.successor == version
                    ],
                }
                for version in summary["pending"]
            ],
            "unavailable": [
                {
                    "version": version,
                    "reasons": summary["reasons"].get(version, []),
                    "evidence": {"observations": self._observation_evidence_locked(product_id, version)},
                }
                for version in summary["unavailable"]
            ],
        }

    def _view_locked(self, product_id: str) -> dict[str, Any]:
        declarations = {
            row[0]: row[1]
            for row in self.conn.execute(
                "SELECT stream, MAX(revision) FROM declaration_revisions"
                " WHERE status = 'applied' AND product_id = ? GROUP BY stream",
                (product_id,),
            )
        }
        observation_count = 0
        if self._has_observations_table_locked():
            observation_count = self.conn.execute(
                "SELECT COUNT(*) FROM observations"
                " WHERE json_extract(payload_json, '$.product_id') = ?",
                (product_id,),
            ).fetchone()[0]
        return {"declarations": declarations, "observation_count": observation_count}

    def _has_observations_table_locked(self) -> bool:
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'observations'"
        ).fetchone()
        return row is not None

    def _observed_versions_locked(self, product_id: str) -> set[str]:
        if not self._has_observations_table_locked():
            return set()
        rows = self.conn.execute(
            "SELECT DISTINCT json_extract(payload_json, '$.version') FROM observations"
            " WHERE json_extract(payload_json, '$.product_id') = ?",
            (product_id,),
        ).fetchall()
        return {str(row[0]) for row in rows if row[0] is not None}

    def _observation_evidence_locked(self, product_id: str, version: str) -> list[dict[str, Any]]:
        if not self._has_observations_table_locked():
            return []
        rows = self.conn.execute(
            "SELECT source, observed_at FROM observations"
            " WHERE json_extract(payload_json, '$.product_id') = ?"
            "   AND json_extract(payload_json, '$.version') = ?"
            " ORDER BY observed_at, source LIMIT ?",
            (product_id, version, EVIDENCE_LIMIT),
        ).fetchall()
        return [{"source": row[0], "observed_at": row[1]} for row in rows]

    def _edges_locked(self, product_id: str) -> list[TaggedEdge]:
        rows = self.conn.execute(
            "SELECT successor, predecessor, stream, revision FROM supersession_edges"
            " WHERE product_id = ? ORDER BY stream, successor, predecessor",
            (product_id,),
        ).fetchall()
        return [TaggedEdge(successor=row[0], predecessor=row[1], stream=row[2], revision=row[3]) for row in rows]

    def _withdrawn_locked(self, product_id: str) -> list[Withdrawal]:
        rows = self.conn.execute(
            "SELECT version, stream, revision FROM version_withdrawals WHERE product_id = ? ORDER BY stream, version",
            (product_id,),
        ).fetchall()
        return [Withdrawal(version=row[0], stream=row[1], revision=row[2]) for row in rows]

    def close(self) -> None:
        self.conn.close()


def _status_of(
    version: str,
    observed: set[str],
    edges: list[TaggedEdge],
    withdrawn: list[Withdrawal],
    availability: dict[str, Any],
) -> str:
    if version in availability["available"]:
        return "available"
    if version in availability["unavailable"]:
        return "unavailable"
    if version in availability["pending"]:
        return "pending"
    if version in availability["withdrawn"]:
        return "withdrawn"
    referenced = (
        any(version in (edge.successor, edge.predecessor) for edge in edges)
        or any(version == item.version for item in withdrawn)
    )
    if version not in observed and not referenced:
        return "unknown"
    return "referenced"


def _diff_availability(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    def versions(section: dict[str, Any], key: str) -> set[str]:
        return {entry["version"] for entry in section[key]}

    before_available = versions(before, "available")
    after_available = versions(after, "available")
    before_pending = versions(before, "pending")
    after_pending = versions(after, "pending")
    return {
        "available_before": sorted(before_available),
        "available_after": sorted(after_available),
        "available_added": sorted(after_available - before_available),
        "available_removed": sorted(before_available - after_available),
        "pending_before": sorted(before_pending),
        "pending_after": sorted(after_pending),
        "pending_added": sorted(after_pending - before_pending),
        "pending_removed": sorted(before_pending - after_pending),
    }
