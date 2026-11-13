"""经纬度矩形几何：规范化、交集、相减与合并。

本模块只做坐标平面上的矩形运算，统一约定如下（测试与文档均按此验证）：

* bbox 顺序固定为 ``[west, south, east, north]``，即
  （最小经度, 最小纬度, 最大经度, 最大纬度）；
  输入接受 4 元素序列，或逗号分隔字符串 ``"west,south,east,north"``。
* 经度取值范围 ``[-180, 180]``，纬度取值范围 ``[-90, 90]``。
* 跨日期变更线（180° 经线）表示：当 ``east < west`` 时视为跨线矩形，
  规范化为两块不跨线矩形 ``[west, 180]`` 与 ``[-180, east]``；
  所有后续运算都在 ``[-180, 180] x [-90, 90]`` 坐标平面内进行，
  不引入投影或球面几何。
* 有效覆盖判定：两个矩形只有在交集具有**严格正面积**时才算覆盖；
  仅在点或线上接触（交集面积为 0）不算有效覆盖。
* 面积 = 经度跨度 × 纬度跨度，单位为“平方度”。这是坐标平面面积的
  演示约定，显式不代表地表面积，也不做任何球面/投影修正。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# 判定“严格正面积/正宽度”时使用的容差：小于该值的跨度视为 0（仅触边）。
EPS = 1e-12


@dataclass(frozen=True)
class Rect:
    """坐标平面内一个不跨日期变更线的轴对齐矩形。"""

    west: float
    south: float
    east: float
    north: float

    @property
    def area(self) -> float:
        return max(0.0, (self.east - self.west) * (self.north - self.south))

    def to_list(self) -> list[float]:
        return [self.west, self.south, self.east, self.north]


@dataclass(frozen=True)
class BBox:
    """按统一顺序解析后的 bbox，可能跨越日期变更线。"""

    west: float
    south: float
    east: float
    north: float

    @property
    def crosses_antimeridian(self) -> bool:
        return self.east < self.west - EPS

    @property
    def area(self) -> float:
        return sum(piece.area for piece in self.split())

    def split(self) -> list[Rect]:
        """跨线矩形拆成两块不跨线矩形；不跨线时返回单元素列表。"""
        if self.crosses_antimeridian:
            return [
                Rect(self.west, self.south, 180.0, self.north),
                Rect(-180.0, self.south, self.east, self.north),
            ]
        return [Rect(self.west, self.south, self.east, self.north)]

    def to_list(self) -> list[float]:
        return [self.west, self.south, self.east, self.north]


def parse_bbox(raw: Any) -> BBox:
    """解析外部 bbox 表示并做范围/退化校验。

    接受：
      * 4 个数字组成的 list/tuple：``[west, south, east, north]``；
      * 逗号分隔字符串：``"west,south,east,north"``（允许空白）。

    缺失、非数字、元素个数不对、经纬度越界、南北极顺序颠倒或
    零宽/零高退化矩形都抛出 ``ValueError``，由上层记为不可靠记录。
    """
    if raw is None:
        raise ValueError("bbox is missing")
    if isinstance(raw, str):
        parts = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        parts = list(raw)
    else:
        raise ValueError("bbox must be a 4-element sequence or a comma-separated string")
    if len(parts) != 4:
        raise ValueError("bbox must contain exactly 4 values: west,south,east,north")
    try:
        west, south, east, north = (float(part) for part in parts)
    except (TypeError, ValueError) as exc:
        raise ValueError("bbox values must be numeric") from exc
    for name, value, low, high in (
        ("west", west, -180.0, 180.0),
        ("east", east, -180.0, 180.0),
        ("south", south, -90.0, 90.0),
        ("north", north, -90.0, 90.0),
    ):
        if not (low <= value <= high):
            raise ValueError(f"bbox {name}={value} out of range [{low}, {high}]")
    if north - south <= EPS:
        raise ValueError("bbox must have positive latitude span (south < north)")
    # east == west 是零宽退化矩形；east < west 合法，表示跨日期变更线。
    if abs(east - west) <= EPS:
        raise ValueError("bbox must have positive longitude span")
    return BBox(west, south, east, north)


def intersect(a: Rect, b: Rect) -> Rect | None:
    """正面积交集；仅触边（零面积）时返回 ``None``。"""
    west = max(a.west, b.west)
    east = min(a.east, b.east)
    south = max(a.south, b.south)
    north = min(a.north, b.north)
    if east - west <= EPS or north - south <= EPS:
        return None
    return Rect(west, south, east, north)


def intersect_rects(rects: Iterable[Rect], cutters: Iterable[Rect]) -> list[Rect]:
    """两组矩形之间的全部正面积交集小块。"""
    pieces: list[Rect] = []
    for rect in rects:
        for cutter in cutters:
            overlap = intersect(rect, cutter)
            if overlap is not None:
                pieces.append(overlap)
    return pieces


def subtract_rect(a: Rect, b: Rect) -> list[Rect]:
    """返回 ``a`` 中不在 ``b`` 内的部分（至多 4 块矩形）。"""
    cut = intersect(a, b)
    if cut is None:
        return [a]
    pieces: list[Rect] = []
    if cut.west - a.west > EPS:
        pieces.append(Rect(a.west, a.south, cut.west, a.north))
    if a.east - cut.east > EPS:
        pieces.append(Rect(cut.east, a.south, a.east, a.north))
    if cut.south - a.south > EPS:
        pieces.append(Rect(cut.west, a.south, cut.east, cut.south))
    if a.north - cut.north > EPS:
        pieces.append(Rect(cut.west, cut.north, cut.east, a.north))
    return pieces


def subtract_rects(rects: Iterable[Rect], cutters: Iterable[Rect]) -> list[Rect]:
    """从一组矩形中依次减去若干切割矩形。"""
    remaining = list(rects)
    for cutter in cutters:
        remaining = [piece for rect in remaining for piece in subtract_rect(rect, cutter)]
    return remaining


def _try_merge(a: Rect, b: Rect) -> Rect | None:
    # 经度跨度相同且纬度方向相接/重叠 -> 纵向合并。
    if (
        abs(a.west - b.west) <= EPS
        and abs(a.east - b.east) <= EPS
        and min(a.north, b.north) + EPS >= max(a.south, b.south)
    ):
        return Rect(a.west, min(a.south, b.south), a.east, max(a.north, b.north))
    # 纬度跨度相同且经度方向相接/重叠 -> 横向合并。
    if (
        abs(a.south - b.south) <= EPS
        and abs(a.north - b.north) <= EPS
        and min(a.east, b.east) + EPS >= max(a.west, b.west)
    ):
        return Rect(min(a.west, b.west), a.south, max(a.east, b.east), a.north)
    return None


def merge_rects(rects: Iterable[Rect]) -> list[Rect]:
    """把相接且可拼成更大矩形的小块合并，输出按坐标排序的规范矩形列表。"""
    merged = [rect for rect in rects if rect.area > EPS]
    changed = True
    while changed:
        changed = False
        for i in range(len(merged)):
            for j in range(i + 1, len(merged)):
                combined = _try_merge(merged[i], merged[j])
                if combined is not None:
                    merged = [rect for k, rect in enumerate(merged) if k not in (i, j)]
                    merged.append(combined)
                    changed = True
                    break
            if changed:
                break
    return sorted(merged, key=lambda rect: (rect.west, rect.south, rect.east, rect.north))


def area_of(rects: Iterable[Rect]) -> float:
    return sum(rect.area for rect in rects)
