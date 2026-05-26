"""Run-state tracking for pipeline idempotency.

Stores a content hash per table in `<output.base_dir>/run_state.json`. On each
run, the pipeline computes the hash of the live introspection result for a
table and compares it to the stored hash:

  - First run / new table       → process, write hash.
  - Same hash as last run       → skip (unless `re_enrich_unchanged=True`).
  - Different hash              → process, overwrite hash.

The hash inputs are deliberately the introspection result (table shape +
column shapes + native descriptions + PK/FK), so source-side changes invalidate
the hash, but pipeline-internal LLM/model changes do not (re-runs are cheap).
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ontology_pipeline.models import TableInfo

logger = logging.getLogger(__name__)

_STATE_FILE_NAME = "run_state.json"
_STATE_FORMAT_VERSION = 1


def content_hash(table: TableInfo) -> str:
    """Stable hash of a table's introspected shape.

    Includes: schema_name, name, is_view, view_definition, primary_key (sorted),
    and per-column (name, sql_type, nullable, description, is_primary_key,
    references_table, references_column). Excludes row_count_estimate (noisy).
    """
    payload = {
        "schema_name": table.schema_name,
        "name": table.name,
        "is_view": table.is_view,
        "view_definition": table.view_definition,
        "description": table.description,
        "primary_key": sorted(table.primary_key),
        "columns": [
            {
                "name": c.name,
                "sql_type": c.sql_type,
                "nullable": c.nullable,
                "description": c.description,
                "is_primary_key": c.is_primary_key,
                "references_table": c.references_table,
                "references_column": c.references_column,
            }
            for c in sorted(table.columns, key=lambda x: x.name)
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RunState:
    """Persistent run-state record stored under `output.base_dir/run_state.json`."""

    def __init__(self, base_dir: Path) -> None:
        self._path = Path(base_dir) / _STATE_FILE_NAME
        self._state: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"format_version": _STATE_FORMAT_VERSION, "sources": {}}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("run_state.json is not a JSON object")
            data.setdefault("format_version", _STATE_FORMAT_VERSION)
            data.setdefault("sources", {})
            return data
        except Exception as exc:
            logger.warning("Run-state file at %s unreadable (%s); starting fresh", self._path, exc)
            return {"format_version": _STATE_FORMAT_VERSION, "sources": {}}

    def lookup(self, *, source_id: str, schema: str, table: str) -> str | None:
        return (
            self._state["sources"]
            .get(source_id, {})
            .get(schema, {})
            .get(table, {})
            .get("content_hash")
        )

    def record(
        self,
        *,
        source_id: str,
        schema: str,
        table: str,
        content_hash_value: str,
        outcome: str,
    ) -> None:
        sources = self._state["sources"]
        per_source = sources.setdefault(source_id, {})
        per_schema = per_source.setdefault(schema, {})
        per_schema[table] = {
            "content_hash": content_hash_value,
            "last_outcome": outcome,
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
        }

    def flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Stamp top-level updated_at on flush.
        self._state["updated_at"] = datetime.now(timezone.utc).isoformat()
        # Atomic write via temp-file + rename.
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._state, indent=2, sort_keys=False), encoding="utf-8")
        tmp.replace(self._path)
