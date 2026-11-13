"""产品版本沿革领域逻辑：声明解析、结构校验、替代图计算。

版本字符串是发布者给出的不透明标识：本模块不解析其含义，不按字典序或到达
顺序推断新旧。版本之间的新旧关系只来自显式声明（supersedes / withdrawn），
版本是否存在只来自目录观测。缺少声明时，各版本保持并列。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

MAX_PATHS = 64


class DeclarationError(ValueError):
    """声明在模式或结构层面非法（不依赖已提交图状态即可判定）。"""

    def __init__(self, reasons: list[str]):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True)
class Edge:
    """一条替代关系：successor 显式替代 predecessor（同一 product_id 内）。"""

    successor: str
    predecessor: str


@dataclass(frozen=True)
class TaggedEdge:
    """带有声明来源（stream + revision）的已提交替代边。"""

    successor: str
    predecessor: str
    stream: str
    revision: int

    def as_dict(self, observed: set[str]) -> dict[str, Any]:
        return {
            "successor": self.successor,
            "predecessor": self.predecessor,
            "stream": self.stream,
            "revision": self.revision,
            "status": "effective" if self.successor in observed else "pending",
        }


@dataclass(frozen=True)
class Withdrawal:
    """一条撤回声明：version 已被发布者撤回。"""

    version: str
    stream: str
    revision: int


@dataclass(frozen=True)
class Declaration:
    """一份解析后的声明修订。revision 是发布者给出的单调序号。"""

    stream: str
    product_id: str
    revision: int
    issued_at: str
    edges: tuple[Edge, ...]
    withdrawn: tuple[str, ...]
    payload_hash: str
    canonical_json: str


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _parse_endpoint(value: Any, role: str, reasons: list[str]) -> tuple[str | None, str]:
    """端点可以是版本字符串，或 {"product_id": ..., "version": ...} 对象。

    返回 (endpoint_product_or_none, version)。显式给出不同 product_id 的端点
    构成跨产品引用，由调用方判定为非法。
    """
    if isinstance(value, str):
        version = _clean_text(value)
        if not version:
            reasons.append(f"{role} must be a non-empty version string")
        return None, version
    if isinstance(value, dict):
        raw_product = value.get("product_id")
        version = _clean_text(value.get("version"))
        product = _clean_text(raw_product)
        if raw_product is not None and not product:
            reasons.append(f"{role}.product_id must be a non-empty string")
        if not version:
            reasons.append(f"{role}.version must be a non-empty string")
        return (product or None), version
    reasons.append(f"{role} must be a version string or an object with product_id and version")
    return None, ""


def parse_declaration(raw: Any) -> Declaration:
    """解析并做模式级 + 结构级校验，不依赖已提交图状态。

    结构级非法包括：跨产品引用、同一修订内部互相矛盾（自替代、双向替代）。
    环检测需要已提交图，由存储层负责。
    """
    reasons: list[str] = []
    if not isinstance(raw, dict):
        raise DeclarationError(["declaration must be a JSON object"])
    stream = _clean_text(raw.get("stream"))
    product_id = _clean_text(raw.get("product_id"))
    issued_at = _clean_text(raw.get("issued_at"))
    revision = raw.get("revision")
    if not stream:
        reasons.append("stream is required")
    if not product_id:
        reasons.append("product_id is required")
    if not issued_at:
        reasons.append("issued_at is required")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        reasons.append("revision must be a positive integer")
        revision = 0

    edges: list[Edge] = []
    seen_edges: set[tuple[str, str]] = set()
    raw_edges = raw.get("supersedes", [])
    if not isinstance(raw_edges, list):
        reasons.append("supersedes must be a list")
        raw_edges = []
    for index, item in enumerate(raw_edges):
        if not isinstance(item, dict):
            reasons.append(f"supersedes[{index}] must be an object")
            continue
        succ_product, successor = _parse_endpoint(item.get("successor"), f"supersedes[{index}].successor", reasons)
        pred_product, predecessor = _parse_endpoint(item.get("predecessor"), f"supersedes[{index}].predecessor", reasons)
        for endpoint_product, role in ((succ_product, "successor"), (pred_product, "predecessor")):
            if endpoint_product is not None and endpoint_product != product_id:
                reasons.append(
                    f"cross-product reference: supersedes[{index}].{role} "
                    f"belongs to product {endpoint_product!r}, not {product_id!r}"
                )
        if successor and predecessor and (successor, predecessor) not in seen_edges:
            seen_edges.add((successor, predecessor))
            edges.append(Edge(successor, predecessor))

    withdrawn: list[str] = []
    seen_withdrawn: set[str] = set()
    raw_withdrawn = raw.get("withdrawn", [])
    if not isinstance(raw_withdrawn, list):
        reasons.append("withdrawn must be a list")
        raw_withdrawn = []
    for index, item in enumerate(raw_withdrawn):
        w_product, version = _parse_endpoint(item, f"withdrawn[{index}]", reasons)
        if w_product is not None and w_product != product_id:
            reasons.append(
                f"cross-product reference: withdrawn[{index}] "
                f"belongs to product {w_product!r}, not {product_id!r}"
            )
        if version and version not in seen_withdrawn:
            seen_withdrawn.add(version)
            withdrawn.append(version)

    # 同一声明修订内部矛盾：自替代、双向替代。
    contradiction_pairs: set[tuple[str, str]] = set()
    edge_pairs = {(edge.successor, edge.predecessor) for edge in edges}
    for edge in edges:
        if edge.successor == edge.predecessor:
            reasons.append(f"contradiction: version {edge.successor!r} supersedes itself")
        elif (edge.predecessor, edge.successor) in edge_pairs:
            pair = tuple(sorted((edge.successor, edge.predecessor)))
            if pair not in contradiction_pairs:
                contradiction_pairs.add(pair)
                reasons.append(
                    f"contradiction: {pair[0]!r} and {pair[1]!r} supersede each other in the same revision"
                )

    reasons = list(dict.fromkeys(reasons))
    if reasons:
        raise DeclarationError(reasons)

    canonical = json.dumps(
        {
            "stream": stream,
            "product_id": product_id,
            "revision": revision,
            "issued_at": issued_at,
            "supersedes": [
                {"successor": edge.successor, "predecessor": edge.predecessor} for edge in edges
            ],
            "withdrawn": sorted(withdrawn),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return Declaration(stream, product_id, revision, issued_at, tuple(edges), tuple(withdrawn), payload_hash, canonical)


def declaration_key(raw: Any) -> tuple[str, int] | None:
    """尽力从原始声明中提取 (stream, revision) 键，用于非法声明的审计落库。"""
    if not isinstance(raw, dict):
        return None
    stream = raw.get("stream")
    revision = raw.get("revision")
    if isinstance(stream, str) and stream.strip():
        if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1:
            return " ".join(stream.split()), revision
    return None


def raw_payload_hash(raw: Any) -> str:
    canonical = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def find_cycle(edges: Iterable[Edge]) -> list[str] | None:
    """在替代图上找环；返回环上的版本序列（首尾相同）或 None。"""
    adjacency: dict[str, list[str]] = {}
    nodes: set[str] = set()
    for edge in edges:
        adjacency.setdefault(edge.successor, []).append(edge.predecessor)
        nodes.add(edge.successor)
        nodes.add(edge.predecessor)
    color: dict[str, int] = {node: 0 for node in nodes}  # 0=white 1=gray 2=black
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = 1
        stack.append(node)
        for nxt in adjacency.get(node, []):
            state = color.get(nxt, 0)
            if state == 1:
                return stack[stack.index(nxt):] + [nxt]
            if state == 0:
                cycle = visit(nxt)
                if cycle is not None:
                    return cycle
        stack.pop()
        color[node] = 2
        return None

    for node in sorted(nodes):
        if color[node] == 0:
            cycle = visit(node)
            if cycle is not None:
                return cycle
    return None


def compute_availability(
    observed: Iterable[str],
    edges: Iterable[TaggedEdge],
    withdrawn: Iterable[Withdrawal],
) -> dict[str, Any]:
    """计算某产品在同一份已提交视图下的可用末端集合。

    规则：
    - 后继版本尚未被目录采到的边是“未满足的关联”（pending），不剥夺前驱的
      可用性；后继到达后该边才生效（effective），前驱才退出可用集合。
    - 被撤回的版本不可用；撤回后继不会让已被替代的前驱重新可用。
    - 同一版本可能被多条边替代、也可能替代多个前驱（分支）；多个未撤回
      末端并存时全部保留，不强挑唯一最新版。
    """
    observed_set = set(observed)
    edge_list = list(edges)
    withdrawn_map: dict[str, Withdrawal] = {}
    for item in withdrawn:
        withdrawn_map.setdefault(item.version, item)

    effective = [edge for edge in edge_list if edge.successor in observed_set]
    pending = [edge for edge in edge_list if edge.successor not in observed_set]

    superseded_by: dict[str, list[TaggedEdge]] = {}
    for edge in effective:
        superseded_by.setdefault(edge.predecessor, []).append(edge)
    pending_successors: dict[str, list[TaggedEdge]] = {}
    for edge in pending:
        pending_successors.setdefault(edge.predecessor, []).append(edge)

    reasons: dict[str, list[dict[str, Any]]] = {}
    for version, incoming in superseded_by.items():
        for edge in incoming:
            reasons.setdefault(version, []).append(
                {
                    "kind": "superseded",
                    "by": edge.successor,
                    "stream": edge.stream,
                    "revision": edge.revision,
                }
            )
    for version, item in withdrawn_map.items():
        reasons.setdefault(version, []).append(
            {"kind": "withdrawn", "stream": item.stream, "revision": item.revision}
        )

    available = sorted(
        version
        for version in observed_set
        if version not in superseded_by and version not in withdrawn_map
    )
    unavailable = sorted(
        version
        for version in observed_set
        if version in superseded_by or version in withdrawn_map
    )
    referenced_successors = {edge.successor for edge in edge_list}
    pending_versions = sorted(
        version
        for version in referenced_successors
        if version not in observed_set and version not in withdrawn_map
    )
    return {
        "available": available,
        "unavailable": unavailable,
        "pending": pending_versions,
        "observed": sorted(observed_set),
        "withdrawn": sorted(withdrawn_map),
        "superseded_by": superseded_by,
        "pending_successors": pending_successors,
        "reasons": reasons,
    }


def compute_lineage(
    edges: Iterable[TaggedEdge],
    version: str,
    observed: Iterable[str],
) -> dict[str, Any]:
    """计算某版本的历史替代路径：向旧版方向的祖先、向新版方向的后继，

    以及从该版本出发到各末端/源头的完整路径。路径上的每条边都带声明来源，
    后继未采到的边标记为 pending。
    """
    edge_list = list(edges)
    observed_set = set(observed)
    by_successor: dict[str, list[TaggedEdge]] = {}
    by_predecessor: dict[str, list[TaggedEdge]] = {}
    for edge in edge_list:
        by_successor.setdefault(edge.successor, []).append(edge)
        by_predecessor.setdefault(edge.predecessor, []).append(edge)

    def closure(start: str, forward: bool) -> list[TaggedEdge]:
        adjacency = by_predecessor if forward else by_successor
        seen: set[tuple[str, str, str, int]] = set()
        visited = {start}
        stack = [start]
        collected: list[TaggedEdge] = []
        while stack:
            node = stack.pop()
            for edge in adjacency.get(node, []):
                key = (edge.successor, edge.predecessor, edge.stream, edge.revision)
                if key in seen:
                    continue
                seen.add(key)
                collected.append(edge)
                nxt = edge.successor if forward else edge.predecessor
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append(nxt)
        return collected

    def paths(start: str, forward: bool) -> tuple[list[list[TaggedEdge]], bool]:
        adjacency = by_predecessor if forward else by_successor
        found: list[list[TaggedEdge]] = []
        truncated = False

        def walk(node: str, trail: list[TaggedEdge], on_path: set[str]) -> None:
            nonlocal truncated
            if len(found) >= MAX_PATHS:
                truncated = True
                return
            next_edges = [edge for edge in adjacency.get(node, [])
                          if (edge.successor if forward else edge.predecessor) not in on_path]
            if not next_edges:
                if trail:
                    found.append(list(trail))
                return
            for edge in next_edges:
                nxt = edge.successor if forward else edge.predecessor
                on_path.add(nxt)
                trail.append(edge)
                walk(nxt, trail, on_path)
                trail.pop()
                on_path.discard(nxt)

        walk(start, [], {start})
        return found, truncated

    def render_path(trail: list[TaggedEdge], forward: bool) -> dict[str, Any]:
        versions = [trail[0].predecessor if forward else trail[0].successor]
        steps = []
        for edge in trail:
            steps.append(
                {
                    "from": edge.predecessor if forward else edge.successor,
                    "to": edge.successor if forward else edge.predecessor,
                    "stream": edge.stream,
                    "revision": edge.revision,
                    "status": "effective" if edge.successor in observed_set else "pending",
                }
            )
            versions.append(edge.successor if forward else edge.predecessor)
        return {"versions": versions, "steps": steps}

    forward_paths, forward_truncated = paths(version, forward=True)
    backward_paths, backward_truncated = paths(version, forward=False)
    return {
        "version": version,
        "observed": version in observed_set,
        "supersedes": [edge.as_dict(observed_set) for edge in closure(version, forward=False)],
        "superseded_by": [edge.as_dict(observed_set) for edge in closure(version, forward=True)],
        "paths_to_terminals": [render_path(trail, True) for trail in forward_paths],
        "paths_to_roots": [render_path(trail, False) for trail in backward_paths],
        "truncated": forward_truncated or backward_truncated,
    }
