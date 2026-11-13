"""Offline satellite catalog ingestion and budgeted combination query."""

from .geometry import BBox, Rect, parse_bbox
from .selection import (
    Candidate,
    ExcludedRecord,
    ResourceLimits,
    SelectedProduct,
    SelectionResult,
)

__all__ = [
    "BBox",
    "Rect",
    "parse_bbox",
    "Candidate",
    "ExcludedRecord",
    "ResourceLimits",
    "SelectedProduct",
    "SelectionResult",
]
