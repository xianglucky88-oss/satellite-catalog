import pytest

from satellite_catalog.geometry import (
    BBox,
    Rect,
    area_of,
    intersect,
    intersect_rects,
    merge_rects,
    parse_bbox,
    subtract_rects,
)


def test_bbox_order_is_west_south_east_north_and_accepts_string_or_list():
    from_string = parse_bbox("1, 2, 3, 4")
    from_list = parse_bbox([1, 2, 3, 4])
    assert from_string.to_list() == [1.0, 2.0, 3.0, 4.0]
    assert from_list.to_list() == [1.0, 2.0, 3.0, 4.0]
    assert from_string.area == 4.0  # (3-1) * (4-2)，坐标平面面积，单位平方度


def test_area_is_plain_coordinate_plane_area_not_surface_area():
    bbox = parse_bbox([0, 0, 10, 10])
    assert bbox.area == 100.0
    # 同样的经纬度跨度在不同纬度上面积完全相同：没有任何球面/投影修正。
    high_latitude = parse_bbox([0, 60, 10, 70])
    assert high_latitude.area == bbox.area


def test_antimeridian_crossing_bbox_splits_into_two_pieces():
    bbox = parse_bbox([175, -5, -175, 5])  # east < west => 跨日期变更线
    assert bbox.crosses_antimeridian is True
    pieces = bbox.split()
    assert pieces == [Rect(175, -5, 180, 5), Rect(-180, -5, -175, 5)]
    assert bbox.area == 5 * 10 + 5 * 10 == 100.0
    normal = parse_bbox([10, 0, 20, 10])
    assert normal.crosses_antimeridian is False
    assert len(normal.split()) == 1


def test_antimeridian_target_intersection_is_computed_on_both_sides():
    target = parse_bbox([170, -10, -170, 10]).split()
    image = parse_bbox([175, -5, -175, 5]).split()
    overlap = intersect_rects(target, image)
    assert area_of(overlap) == 100.0  # 两侧各 5x10


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "1,2,3",
        "1,2,3,x",
        [1, 2, 3],
        [-181, 0, 10, 10],
        [0, 91, 10, 95],
        [0, 10, 10, 5],   # north < south
        [5, 0, 5, 10],    # 零经度跨度
        [0, 5, 10, 5],    # 零纬度跨度
    ],
)
def test_invalid_bboxes_are_rejected(raw):
    with pytest.raises(ValueError):
        parse_bbox(raw)


def test_edge_contact_only_does_not_count_as_coverage():
    target = Rect(0, 0, 10, 10)
    touching = Rect(-5, 0, 0, 10)          # 仅在经线 x=0 上接触
    assert intersect(target, touching) is None
    line_touch = Rect(0, 10, 10, 15)       # 仅在纬线 y=10 上接触
    assert intersect(target, line_touch) is None
    corner = Rect(-5, -5, 0, 0)            # 仅在角点接触
    assert intersect(target, corner) is None
    overlapping = Rect(-5, 2, 5, 8)
    assert intersect(target, overlapping) == Rect(0, 2, 5, 8)


def test_subtract_rects_leaves_exactly_the_uncovered_pieces():
    target = [Rect(0, 0, 10, 10)]
    cutters = [Rect(0, 0, 6, 10), Rect(4, 0, 10, 10)]
    uncovered = merge_rects(subtract_rects(target, cutters))
    # 两个切割矩形的并集恰好覆盖整个目标。
    assert uncovered == []
    partial = merge_rects(subtract_rects(target, [Rect(0, 0, 4, 5)]))
    assert area_of(partial) == 100.0 - 20.0


def test_merge_rects_joins_adjacent_pieces_deterministically():
    merged = merge_rects([Rect(0, 0, 5, 10), Rect(5, 0, 10, 10)])
    assert merged == [Rect(0, 0, 10, 10)]
    stacked = merge_rects([Rect(0, 0, 10, 4), Rect(0, 4, 10, 10)])
    assert stacked == [Rect(0, 0, 10, 10)]
