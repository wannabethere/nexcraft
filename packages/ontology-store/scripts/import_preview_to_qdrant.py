#!/usr/bin/env python3
"""Import a nexcraft ontology-pipeline `preview` output tree into Qdrant.

The pipeline's `preview` sink only *stages* the artifacts that the production
`hierarchy_store` sink + ReindexWorker would write — JSON on disk, never pushed
to Qdrant. This script replays that staged tree straight into a Qdrant instance
using the SAME canonical embedder + collection schema the ReindexWorker uses
(`ontology_store.vector`), so the points are faithful to production.

What it loads (from `<preview-dir>`):

  mdl/<source>/<schema>/<table>.json
      → HIER_T4_ASSETS   (one point per model)   via indexer.upsert_asset
      → HIER_T5_FIELDS   (one point per column)  via indexer.upsert_field
  qdrant/causal_events/*.json      → CAUSAL_EVENTS_<tenant>      (faithful replay)
  qdrant/relation_events/*.json    → RELATION_EVENTS_<tenant>
  qdrant/protection_events/*.json  → PROTECTION_EVENTS_<tenant>
  qdrant/card_events/*.json        → CARD_EVENTS_<tenant>
  <sql-pairs-file> (optional)      → SQL_PAIRS_<tenant>          via upsert_sql_pair

The staged event files already carry production-built narrative + payload
({event_id, collection, narrative, payload}); we upsert them verbatim (payload
nested under metadata.*, matching HierarchyVectorIndexer._upsert_one).

Target Qdrant is taken from the standard env vars the QdrantClientFactory reads:
    QDRANT_URL   (e.g. http://52.6.13.191:6333)   — OR —
    QDRANT_HOST + QDRANT_PORT
    QDRANT_API_KEY (optional)
Embeddings need OPENAI_API_KEY (or EMBEDDING_API_KEY); EMBEDDING_MODEL defaults
to text-embedding-3-small (1536-dim, matching every collection spec).

Run (real):
    cd nexcraft
    .venv/bin/pip install qdrant-client          # one-time: the [vector] extra
    set -a; source <env-with-QDRANT_HOST+OPENAI_API_KEY>; set +a
    .venv/bin/python packages/ontology-store/scripts/import_preview_to_qdrant.py \
        --preview-dir output/preview --source-id csod-local \
        --env preview --tenant csod-local

Run (dry — no network, no embeddings; just shows what WOULD upsert):
    python3 packages/ontology-store/scripts/import_preview_to_qdrant.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("import_preview_to_qdrant")

# Staged event subdir → collections.py spec tier_id (resolved lazily so dry-run
# only needs the pure-python collections module, not qdrant_client).
_EVENT_DIRS = ("causal_events", "relation_events", "protection_events", "card_events")


# ── Narrative + payload composition (mirrors workers/narrative.py intent) ────


def _columns(model: dict[str, Any]) -> list[dict[str, Any]]:
    return model.get("columns") or []


def _col_prop(col: dict[str, Any], key: str, default: Any = None) -> Any:
    props = col.get("properties") or {}
    return props.get(key, default)


def build_asset_narrative(model: dict[str, Any]) -> str:
    """Embedding text for a T4 asset: name + description + column briefs."""
    parts: list[str] = [str(model.get("name") or "")]
    desc = model.get("description")
    if desc:
        parts.append(str(desc))
    cols = _columns(model)
    if cols:
        parts.append("Columns:")
        for c in cols:
            bm = _col_prop(c, "business_meaning") or _col_prop(c, "description") or ""
            parts.append(f"  - {c.get('name')} ({c.get('type')}) — {bm}".rstrip(" —"))
    return "\n".join(p for p in parts if p)


def build_asset_payload(
    model: dict[str, Any],
    *,
    org_id: str,
    source_id: str,
    catalog: str,
    schema: str,
    has_inferred: bool,
    causal_node_count: int,
) -> dict[str, Any]:
    schema_rk = f"postgres://{source_id}.{catalog}/{schema}"
    is_view = bool(model.get("is_view"))
    desc = str(model.get("description") or "")
    return {
        "asset_rk": model.get("rk"),
        "asset_kind": "view" if is_view else "table",
        "lifecycle_stage": "active",
        "effective_sensitivity_class": "",
        "domain_tags": [],
        "concepts": [],
        "key_areas": [],
        "causal_relations": [],
        "org_id": org_id,
        "source_id": source_id,
        "catalog_uid": catalog,
        "schema_rk": schema_rk,
        "primary_object_type": "",
        "relation_predicates": [],
        "has_inferred_relationships": has_inferred,
        "causal_node_count": causal_node_count,
        "rich_description_present": model.get("description_provenance") == "llm_rich_documentation"
        or len(desc) > 40,
    }


def build_field_narrative(col: dict[str, Any]) -> str:
    bm = _col_prop(col, "business_meaning") or _col_prop(col, "description") or ""
    su = _col_prop(col, "semantic_unit") or ""
    base = f"{col.get('name')} ({col.get('type')}) — {bm}".rstrip(" —")
    return base + (f"\nsemantic_unit: {su}" if su else "")


def build_field_payload(
    col: dict[str, Any],
    *,
    parent_rk: str,
    org_id: str,
    pii_cols: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    name = col.get("name")
    pii = pii_cols.get(name, {})
    is_pii = bool(_col_prop(col, "is_pii") or pii.get("is_pii"))
    is_bk = bool(_col_prop(col, "is_primary_key") or _col_prop(col, "is_business_key"))
    return {
        "field_rk": col.get("rk"),
        "field_kind": "column",
        "parent_rk": parent_rk,
        "org_id": org_id,
        "is_pii": is_pii,
        "pii_categories": pii.get("pii_categories") or _col_prop(col, "pii_categories") or [],
        "is_business_key": is_bk,
        "semantic_unit": _col_prop(col, "semantic_unit") or "",
    }


# ── Loaders for sidecar enrichment (best-effort, defensive) ──────────────────


def load_pii_cols(dp_dir: Path, source_id: str, schema: str, table: str) -> dict[str, dict[str, Any]]:
    """Map column_name → {is_pii, pii_categories} from a data_protection_hints file."""
    f = dp_dir / source_id / schema / f"{table}.json"
    if not f.is_file():
        return {}
    try:
        hints = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    # The enricher may attach a per-column lookup; tolerate several shapes.
    lookup = hints.get("column_lookup") if isinstance(hints, dict) else None
    if isinstance(lookup, dict):
        for col, meta in lookup.items():
            if isinstance(meta, dict):
                out[col] = {
                    "is_pii": bool(meta.get("is_pii")),
                    "pii_categories": meta.get("pii_categories") or [],
                }
    for col in hints.get("cls_columns") or []:
        if isinstance(col, str):
            out.setdefault(col, {})["is_pii"] = True
    return out


def count_causal_for_table(causal_dir: Path, source_id: str, schema: str, table: str) -> int:
    f = causal_dir / source_id / schema / f"{table}.json"
    if not f.is_file():
        return 0
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for k in ("candidates", "items"):
            if isinstance(data.get(k), list):
                return len(data[k])
    return 0


def has_inferred_rels(rel_dir: Path, source_id: str, schema: str, table: str) -> bool:
    return (rel_dir / source_id / schema / f"{table}.json").is_file()


# ── Main ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    repo_default = Path(__file__).resolve().parents[3] / "output" / "preview"
    ap.add_argument("--preview-dir", type=Path, default=repo_default)
    ap.add_argument("--source-id", default="csod-local", help="locates mdl/<source>/<schema>")
    ap.add_argument("--schema", default="public")
    ap.add_argument("--env", default="preview", help="env-scope for spine collections (hier_t4_assets_<env>)")
    ap.add_argument("--tenant", default="csod-local", help="tenant-scope for events/sql_pairs collections")
    ap.add_argument("--org-id", default="preview-org", help="payload org_id (default matches staged events)")
    ap.add_argument("--rewrite-org", action="store_true", default=True,
                    help="overwrite staged-event payload org_id with --org-id for consistency")
    ap.add_argument("--no-rewrite-org", dest="rewrite_org", action="store_false")
    ap.add_argument("--sql-pairs-file", type=Path, default=None,
                    help="optional JSON list of curated {id,question,sql,instructions,...} → SQL_PAIRS")
    ap.add_argument("--limit", type=int, default=None, help="cap items per category (testing)")
    ap.add_argument("--dry-run", action="store_true", help="parse + compose only; no network/embeddings")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    preview = args.preview_dir
    mdl_dir = preview / "mdl"
    if not mdl_dir.is_dir():
        logger.error("No mdl/ under %s — is --preview-dir correct?", preview)
        return 2

    from ontology_store.vector.collections import (  # pure-python; safe in dry-run
        CARD_EVENTS,
        CAUSAL_EVENTS,
        HIER_T4_ASSETS,
        HIER_T5_FIELDS,
        PROTECTION_EVENTS,
        RELATION_EVENTS,
        SQL_PAIRS,
        resolve_collection_name,
    )

    event_spec = {
        "causal_events": CAUSAL_EVENTS,
        "relation_events": RELATION_EVENTS,
        "protection_events": PROTECTION_EVENTS,
        "card_events": CARD_EVENTS,
    }

    # Target collection names (shown in dry-run so the operator can reconcile
    # context_preparer's QDRANT_COLLECTION_* env to these).
    logger.info("Target collections:")
    logger.info("  assets  : %s", resolve_collection_name(HIER_T4_ASSETS, env=args.env))
    logger.info("  fields  : %s", resolve_collection_name(HIER_T5_FIELDS, env=args.env))
    for name, spec in event_spec.items():
        logger.info("  %-8s: %s", name, resolve_collection_name(spec, tenant_id=args.tenant))
    logger.info("  sql_pairs: %s", resolve_collection_name(SQL_PAIRS, tenant_id=args.tenant))

    indexer = None
    if not args.dry_run:
        try:
            from ontology_store.vector import (
                HierarchyVectorIndexer,
                OpenAIEmbedder,
                QdrantClientFactory,
            )
        except ImportError as exc:
            logger.error("Vector deps missing (%s). Install: nexcraft/.venv/bin/pip install qdrant-client", exc)
            return 2
        client = QdrantClientFactory.get()  # reads QDRANT_URL / QDRANT_HOST+PORT / QDRANT_API_KEY
        embedder = OpenAIEmbedder()
        indexer = HierarchyVectorIndexer(qdrant_client=client, embedder=embedder, env=args.env)
        indexer.ensure_all_env_collections()
        indexer.ensure_tenant_collections(args.tenant)

    counts = {"assets": 0, "fields": 0, "sql_pairs": 0}
    counts.update({k: 0 for k in _EVENT_DIRS})

    # ── Spine: assets (T4) + fields (T5) from mdl/ ──────────────────────────
    mdl_glob = sorted((mdl_dir / args.source_id / args.schema).glob("*.json"))
    if args.limit:
        mdl_glob = mdl_glob[: args.limit]
    for f in mdl_glob:
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("skip %s: %s", f.name, exc)
            continue
        catalog = doc.get("catalog") or ""
        for model in doc.get("models") or []:
            asset_rk = model.get("rk")
            if not asset_rk:
                continue
            table = model.get("name") or f.stem
            has_inf = has_inferred_rels(
                preview / "inferred_relationships", args.source_id, args.schema, table
            )
            n_causal = count_causal_for_table(
                preview / "causal_candidates", args.source_id, args.schema, table
            )
            a_text = build_asset_narrative(model)
            a_payload = build_asset_payload(
                model, org_id=args.org_id, source_id=args.source_id,
                catalog=catalog, schema=args.schema,
                has_inferred=has_inf, causal_node_count=n_causal,
            )
            if indexer:
                indexer.upsert_asset(asset_rk, a_text, a_payload)
            counts["assets"] += 1

            pii_cols = load_pii_cols(
                preview / "data_protection_hints", args.source_id, args.schema, table
            )
            for col in _columns(model):
                field_rk = col.get("rk")
                if not field_rk:
                    continue
                if indexer:
                    indexer.upsert_field(
                        field_rk,
                        build_field_narrative(col),
                        build_field_payload(col, parent_rk=asset_rk, org_id=args.org_id, pii_cols=pii_cols),
                    )
                counts["fields"] += 1
        logger.debug("processed %s", f.name)

    # ── Events: faithful replay of staged qdrant/<collection>/*.json ────────
    for ev_dir_name in _EVENT_DIRS:
        ev_dir = preview / "qdrant" / ev_dir_name
        if not ev_dir.is_dir():
            continue
        spec = event_spec[ev_dir_name]
        store = indexer.store_for(spec, tenant_id=args.tenant) if indexer else None
        files = sorted(ev_dir.glob("*.json"))
        if args.limit:
            files = files[: args.limit]
        for f in files:
            try:
                ev = json.loads(f.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("skip event %s: %s", f.name, exc)
                continue
            event_id = ev.get("event_id") or f.stem
            narrative = ev.get("narrative") or ""
            payload = dict(ev.get("payload") or {})
            if args.rewrite_org:
                payload["org_id"] = args.org_id
            if store is not None:
                store.upsert_points([{"id": event_id, "text": narrative, "metadata": payload}])
            counts[ev_dir_name] += 1

    # ── Curated SQL pairs (the join-pattern few-shot fix) ───────────────────
    if args.sql_pairs_file and args.sql_pairs_file.is_file():
        pairs = json.loads(args.sql_pairs_file.read_text(encoding="utf-8"))
        if args.limit:
            pairs = pairs[: args.limit]
        for p in pairs:
            pid = str(p.get("id") or p.get("sql_pair_id") or "")
            question = str(p.get("question") or "")
            instructions = str(p.get("instructions") or "")
            if not pid or not question:
                logger.warning("skip sql pair (missing id/question): %r", p)
                continue
            text = question if not instructions else f"{question}\n\nInstructions: {instructions}"
            payload = {
                "question": question,
                "sql": p.get("sql") or "",
                "instructions": instructions,
                "references_asset_rks": p.get("references_asset_rks") or [],
                "concepts": p.get("concepts") or [],
                "key_areas": p.get("key_areas") or [],
                "source_provenance": p.get("source_provenance") or "curated",
                "valid_for_lifecycle": p.get("valid_for_lifecycle") or "active",
                "org_id": args.org_id,
            }
            if indexer:
                indexer.upsert_sql_pair(args.tenant, sql_pair_id=pid, question=text, payload=payload)
            counts["sql_pairs"] += 1

    mode = "DRY-RUN (nothing written)" if args.dry_run else "UPSERTED"
    logger.info("%s — %s", mode, json.dumps(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
