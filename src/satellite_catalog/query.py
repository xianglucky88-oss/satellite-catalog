"""组合查询编排：目录筛选、几何规范化、组合选择三阶段解耦。

调用方只需提供目标矩形、采集时间范围、云量上限与产品数量预算；
本模块负责：
  1. 把时间边界规范化成可与库中 ISO-8601 字符串比较的 UTC 表示，
     并调用存储层的真实 SQL 查询完成目录筛选；
  2. 用 geometry 模块解析/规范化每个 bbox，在 selection 模块中完成
     重复来源去重、同版本元数据冲突判定和仅触边剔除；
  3. 在预算与资源上限内执行组合选择，并把“近似/提前停止”的事实
     原样透传给调用方。
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from .geometry import BBox, parse_bbox
from .selection import (
    ResourceLimits,
    SelectionResult,
    build_candidates,
    select_combination,
)
from .storage import ObservationStore


def _to_utc_iso(value: str, *, end_of_day: bool) -> str:
    """把日期或日期时间字符串规范化为 UTC 的 ISO-8601 字符串。

    接受 ``YYYY-MM-DD``（分别解释为当天 00:00:00 / 23:59:59.999999）
    以及带时区的 ISO-8601 日期时间；无时区的日期时间按 UTC 处理。
    存储中的 ``acquired_at`` 采用同一规范形式，因此 SQL 字符串比较
    与时间顺序一致。
    """
    text = value.strip()
    if len(text) == 10:
        day = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if end_of_day:
            day = day.replace(hour=23, minute=59, second=59, microsecond=999999)
        return day.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    normalized = text.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def run_combination_query(
    store: ObservationStore,
    *,
    target_bbox: Any,
    acquired_after: str,
    acquired_before: str,
    max_cloud_cover: float,
    budget: int,
    limits: ResourceLimits | None = None,
) -> dict[str, Any]:
    """对已入库目录执行组合查询，返回可 JSON 序列化的结果字典。"""
    if max_cloud_cover < 0:
        raise ValueError("max_cloud_cover must be non-negative")
    if budget < 0:
        raise ValueError("budget must be non-negative")
    limits = limits or ResourceLimits()

    # 目标矩形同样遵循统一 bbox 约定，并在跨日期变更线时拆分。
    target: BBox = parse_bbox(target_bbox)
    target_pieces = target.split()
    target_area = sum(piece.area for piece in target_pieces)

    after = _to_utc_iso(acquired_after, end_of_day=False)
    before = _to_utc_iso(acquired_before, end_of_day=True)

    # 阶段一：真实存储查询（SQL 完成时间窗与云量筛选）。
    rows = store.query_candidates(
        acquired_after=after,
        acquired_before=before,
        max_cloud_cover=float(max_cloud_cover),
    )

    # 阶段二：几何规范化 + 重复来源去重 + 同版本冲突/缺范围判定。
    candidates, excluded = build_candidates(rows, target_pieces)

    # 阶段三：预算内组合选择（穷举或贪心，资源上限受控）。
    result: SelectionResult = select_combination(
        candidates=candidates,
        excluded=excluded,
        target_pieces=target_pieces,
        target_area=target_area,
        target_bbox=target.to_list(),
        budget=budget,
        limits=limits,
    )

    payload = asdict(result)
    payload["total_observations_in_store"] = store.total_observations()
    payload["time_window"] = {"acquired_after": after, "acquired_before": before}
    payload["max_cloud_cover"] = max_cloud_cover
    return payload
