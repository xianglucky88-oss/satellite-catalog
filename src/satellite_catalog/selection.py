"""预算约束下的影像组合选择。

选择目标（按优先级）：
1. 在产品数量预算内，使所选产品对目标矩形的覆盖面积最大化；
2. 覆盖面积相同的组合，使用产品数更少者；
3. 再按确定性键序打破平局，保证结果可复现。

策略：
* 候选数较小时使用穷举（按子集大小枚举），得到的结果可声明为最优；
* 候选数较大时回退到“边际覆盖增量贪心 + 冗余产品剔除”，结果显式
  标记为近似，不宣称最优；
* 候选数或枚举组合数超过调用方给定的资源上限时提前停止，同样在
  返回结构中暴露提前停止的事实与原因。

几何上只消费 ``geometry`` 模块规范化后的矩形，本模块不解析原始 bbox。
"""
from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from typing import Any

from .geometry import (
    BBox,
    Rect,
    area_of,
    intersect_rects,
    merge_rects,
    parse_bbox,
    subtract_rects,
)


@dataclass(frozen=True)
class ResourceLimits:
    """调用方可控的资源上限；超限即提前停止并在结果中标记。"""

    max_candidates: int = 200
    max_combinations: int = 200_000


@dataclass(frozen=True)
class ExcludedRecord:
    product_id: str
    version: str
    source: str
    reason: str
    detail: str


@dataclass(frozen=True)
class Candidate:
    product_id: str
    version: str
    sources: tuple[str, ...]
    acquired_at: str
    cloud_cover: float
    bbox: BBox
    footprint: tuple[Rect, ...]
    overlap: tuple[Rect, ...]
    overlap_area: float
    # 确定性排序键：先按云量，再按采集时间，最后按产品/版本。
    order_key: tuple[Any, ...]


@dataclass(frozen=True)
class SelectedProduct:
    product_id: str
    version: str
    sources: tuple[str, ...]
    acquired_at: str
    cloud_cover: float
    bbox: list[float]
    # 该产品实际计入组合的覆盖区域（与已选产品重叠的部分不重复计入）。
    contribution: list[list[float]]
    contribution_area: float


@dataclass(frozen=True)
class SelectionResult:
    target_bbox: list[float]
    target_area: float
    covered_area: float
    uncovered_area: float
    covered_fraction: float
    selected: list[SelectedProduct]
    uncovered: list[list[float]]
    excluded: list[ExcludedRecord]
    candidates_considered: int
    candidates_used: int
    budget: int
    method: str  # "exhaustive" | "greedy"
    optimal: bool
    stopped_early: bool
    stop_reason: str
    limits: dict[str, int] = field(default_factory=dict)


def _bbox_fingerprint(bbox: BBox) -> tuple:
    # 几何指纹：规范化后的 footprint 矩形列表（含跨日期变更线拆分）。
    return tuple(
        (round(r.west, 10), round(r.south, 10), round(r.east, 10), round(r.north, 10))
        for r in merge_rects(bbox.split())
    )


def build_candidates(
    rows: list[dict[str, Any]], target_pieces: list[Rect]
) -> tuple[list[Candidate], list[ExcludedRecord]]:
    """把存储层返回的行组装为可靠候选（组合选择的第二阶段后半）。

    同一 (product_id, version) 可能来自多个来源（重复来源），
    去重后只占一个名额；若同版本元数据互相矛盾或任何来源缺少
    可解析范围，则整组记录都不能被当成可靠覆盖，整组排除并暴露原因。
    """
    excluded: list[ExcludedRecord] = []
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (str(row["product_id"]).strip().lower(), str(row["version"]).strip().lower())
        groups.setdefault(key, []).append(row)

    candidates: list[Candidate] = []
    for (product_id, version), group_rows in sorted(groups.items()):
        sources = tuple(sorted({str(row["source"]) for row in group_rows}))
        parsed: list[tuple[dict[str, Any], BBox]] = []
        bad: ExcludedRecord | None = None
        for row in group_rows:
            try:
                parsed_bbox = parse_bbox(_extract_bbox(row))
                parsed.append((row, parsed_bbox))
            except (TypeError, ValueError, KeyError) as exc:
                bad = ExcludedRecord(
                    product_id=str(row["product_id"]),
                    version=str(row["version"]),
                    source=str(row["source"]),
                    reason="missing_or_invalid_extent",
                    detail=str(exc),
                )
                break
        if bad is not None:
            for row in group_rows:
                excluded.append(ExcludedRecord(
                    product_id=str(row["product_id"]),
                    version=str(row["version"]),
                    source=str(row["source"]),
                    reason="missing_or_invalid_extent",
                    detail=bad.detail,
                ))
            continue

        # 同版本元数据一致性校验：范围、云量、采集时间必须完全一致。
        reference = parsed[0]
        ref_fingerprint = _bbox_fingerprint(reference[1])
        conflict_fields: set[str] = set()
        for row, parsed_bbox in parsed[1:]:
            if _bbox_fingerprint(parsed_bbox) != ref_fingerprint:
                conflict_fields.add("bbox")
            if float(row["cloud_cover"]) != float(reference[0]["cloud_cover"]):
                conflict_fields.add("cloud_cover")
            if str(row["acquired_at"]) != str(reference[0]["acquired_at"]):
                conflict_fields.add("acquired_at")
        if conflict_fields:
            for row in group_rows:
                excluded.append(ExcludedRecord(
                    product_id=str(row["product_id"]),
                    version=str(row["version"]),
                    source=str(row["source"]),
                    reason="metadata_conflict",
                    detail="inconsistent fields across sources: " + ",".join(sorted(conflict_fields)),
                ))
            continue

        ref_row, ref_bbox = reference
        footprint = tuple(ref_bbox.split())
        overlap_pieces = merge_rects(intersect_rects(target_pieces, footprint))
        overlap_area = area_of(overlap_pieces)
        if overlap_area <= 0.0:
            # 与目标仅触边或完全不相交：不占用预算，也不计入候选。
            for row in group_rows:
                excluded.append(ExcludedRecord(
                    product_id=str(row["product_id"]),
                    version=str(row["version"]),
                    source=str(row["source"]),
                    reason="no_positive_overlap",
                    detail="intersection with target has zero area (edge contact only or disjoint)",
                ))
            continue

        candidates.append(Candidate(
            product_id=str(ref_row["product_id"]),
            version=str(ref_row["version"]),
            sources=sources,
            acquired_at=str(ref_row["acquired_at"]),
            cloud_cover=float(ref_row["cloud_cover"]),
            bbox=ref_bbox,
            footprint=footprint,
            overlap=tuple(overlap_pieces),
            overlap_area=overlap_area,
            order_key=(
                float(ref_row["cloud_cover"]),
                str(ref_row["acquired_at"]),
                product_id,
                version,
            ),
        ))

    candidates.sort(key=lambda candidate: candidate.order_key)
    return candidates, excluded


def _extract_bbox(row: dict[str, Any]) -> Any:
    payload = json.loads(row["payload_json"])
    return payload.get("bbox")


def _covered_area(combo: list[Candidate], target_pieces: list[Rect]) -> float:
    covered_rects: list[Rect] = []
    for candidate in combo:
        covered_rects = merge_rects(covered_rects + list(candidate.overlap))
    return area_of(intersect_rects(target_pieces, covered_rects))


def _finalize(
    chosen: list[Candidate],
    target_pieces: list[Rect],
    target_area: float,
    method: str,
    optimal: bool,
    stopped_early: bool,
    stop_reason: str,
    excluded: list[ExcludedRecord],
    candidates_considered: int,
    candidates_used: int,
    budget: int,
    limits: ResourceLimits,
    target_bbox: list[float],
) -> SelectionResult:
    # 冗余剔除：移除后覆盖面积不下降的产品（贪心结果可能包含这类产品）。
    removable = True
    while removable and len(chosen) > 1:
        removable = False
        for index, candidate in enumerate(chosen):
            without = chosen[:index] + chosen[index + 1:]
            if _covered_area(without, target_pieces) >= _covered_area(chosen, target_pieces) - 1e-12:
                chosen = without
                removable = True
                break

    # 按固定顺序重算每幅影像实际贡献，使贡献区域可逐幅复算。
    ordered = sorted(chosen, key=lambda candidate: candidate.order_key)
    covered_so_far: list[Rect] = []
    selected: list[SelectedProduct] = []
    for candidate in ordered:
        fresh = subtract_rects(candidate.overlap, covered_so_far)
        fresh = merge_rects(fresh)
        contribution_area = area_of(fresh)
        if contribution_area <= 1e-12:
            continue
        covered_so_far = merge_rects(covered_so_far + fresh)
        selected.append(SelectedProduct(
            product_id=candidate.product_id,
            version=candidate.version,
            sources=candidate.sources,
            acquired_at=candidate.acquired_at,
            cloud_cover=candidate.cloud_cover,
            bbox=candidate.bbox.to_list(),
            contribution=[rect.to_list() for rect in fresh],
            contribution_area=contribution_area,
        ))

    uncovered = merge_rects(subtract_rects(target_pieces, covered_so_far))
    covered_area = area_of(covered_so_far)
    uncovered_area = area_of(uncovered)
    return SelectionResult(
        target_bbox=target_bbox,
        target_area=target_area,
        covered_area=covered_area,
        uncovered_area=uncovered_area,
        covered_fraction=(covered_area / target_area) if target_area > 0 else 0.0,
        selected=selected,
        uncovered=[rect.to_list() for rect in uncovered],
        excluded=excluded,
        candidates_considered=candidates_considered,
        candidates_used=candidates_used,
        budget=budget,
        method=method,
        optimal=optimal,
        stopped_early=stopped_early,
        stop_reason=stop_reason,
        limits={"max_candidates": limits.max_candidates,
                "max_combinations": limits.max_combinations},
    )


def select_combination(
    candidates: list[Candidate],
    excluded: list[ExcludedRecord],
    target_pieces: list[Rect],
    target_area: float,
    target_bbox: list[float],
    budget: int,
    limits: ResourceLimits,
) -> SelectionResult:
    """在预算与资源上限内执行组合选择，并显式暴露近似/提前停止事实。"""
    candidates_considered = len(candidates)
    if budget <= 0:
        return _finalize(
            [], target_pieces, target_area, "exhaustive", True, False,
            "budget_is_zero", excluded, candidates_considered, 0, budget,
            limits, target_bbox,
        )
    if not candidates:
        return _finalize(
            [], target_pieces, target_area, "exhaustive", True, False,
            "no_eligible_candidate", excluded, candidates_considered, 0, budget,
            limits, target_bbox,
        )

    # 资源上限：候选过多时只保留排序后最靠前的部分，并标记提前停止。
    truncated = False
    used_candidates = candidates
    if len(used_candidates) > limits.max_candidates:
        used_candidates = used_candidates[: limits.max_candidates]
        truncated = True

    effective_budget = min(budget, len(used_candidates))

    if not truncated:
        # 穷举：按子集大小从小到大枚举；枚举数受 max_combinations 硬约束，
        # 触顶即放弃最优性声明，回退贪心并在结果中暴露提前停止。
        best_combo: list[Candidate] = []
        best_area = -1.0
        enumerated = 0
        stopped_early = False
        stop_reason = "exhaustive_completed"
        for size in range(1, effective_budget + 1):
            for combo in itertools.combinations(used_candidates, size):
                enumerated += 1
                if enumerated > limits.max_combinations:
                    stopped_early = True
                    stop_reason = "combination_limit_reached"
                    break
                area = _covered_area(list(combo), target_pieces)
                if area > best_area + 1e-12:
                    best_area = area
                    best_combo = list(combo)
            if stopped_early:
                break
            # 已经能完整覆盖目标：更大的子集不可能更优，且当前大小就是
            # 取得完整覆盖的最小子集（按 size 递增枚举），可提前结束。
            if best_area >= target_area - 1e-12:
                break
        method = "greedy" if stopped_early else "exhaustive"
        if stopped_early:
            best_combo = _greedy(used_candidates, target_pieces, effective_budget)
        return _finalize(
            best_combo, target_pieces, target_area, method,
            optimal=not stopped_early, stopped_early=stopped_early,
            stop_reason=stop_reason,
            excluded=excluded, candidates_considered=candidates_considered,
            candidates_used=len(used_candidates), budget=budget,
            limits=limits, target_bbox=target_bbox,
        )

    # 候选数超过上限被截断：穷举不再具有全局意义，直接贪心近似。
    chosen = _greedy(used_candidates, target_pieces, effective_budget)
    return _finalize(
        chosen, target_pieces, target_area, "greedy", optimal=False,
        stopped_early=True, stop_reason="candidate_limit_reached",
        excluded=excluded, candidates_considered=candidates_considered,
        candidates_used=len(used_candidates), budget=budget,
        limits=limits, target_bbox=target_bbox,
    )


def _greedy(
    candidates: list[Candidate], target_pieces: list[Rect], budget: int
) -> list[Candidate]:
    """每一步选择对当前未覆盖部分边际增量最大的产品。"""
    chosen: list[Candidate] = []
    covered: list[Rect] = []
    remaining_pool = list(candidates)
    while remaining_pool and len(chosen) < budget:
        best_index = -1
        best_gain = 0.0
        best_pieces: list[Rect] = []
        for index, candidate in enumerate(remaining_pool):
            fresh = merge_rects(subtract_rects(candidate.overlap, covered))
            gain = area_of(fresh)
            # 平局时使用候选的确定性 order_key，保证可复现。
            if gain > best_gain + 1e-12 or (
                abs(gain - best_gain) <= 1e-12
                and gain > 0.0
                and (best_index == -1 or candidate.order_key < remaining_pool[best_index].order_key)
            ):
                best_index = index
                best_gain = gain
                best_pieces = fresh
        if best_index == -1 or best_gain <= 1e-12:
            break
        chosen.append(remaining_pool.pop(best_index))
        covered = merge_rects(covered + best_pieces)
    return chosen
