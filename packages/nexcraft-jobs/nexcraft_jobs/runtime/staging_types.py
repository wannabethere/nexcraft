"""Handles passed between Temporal activities (bulk data stays on disk/object store)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExtractedDataset:
    storage_uri: str
    schema_json: str
    row_count_estimate: int


@dataclass(frozen=True)
class ExtractResults:
    datasets: dict[str, ExtractedDataset]
    bytes_total: int
    duration_ms: int
