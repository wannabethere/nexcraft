#!/usr/bin/env python3
"""Ingest CURATED retrieval signals into a tenant's Qdrant collections.

The ontology-pipeline + ReindexWorker build the *spine* (hier_t* assets/fields/…)
from a Postgres source automatically. But three high-value, hand-authored signals
have no production ingest path of their own:

  * SQL examples  → SQL_PAIRS_<tenant>   (question + gold SQL + join instructions)
  * Metrics       → CARDS_<tenant>       (semantic-layer card, kind="metric")
  * Instructions  → CARDS_<tenant>       (semantic-layer card, kind="instruction")

(The ReindexWorker has no sql_pair task kind, and its doc-per-row card handler is
a v1 stub — so these would otherwise only land via the offline preview replay.)

This CLI upserts them straight into the tenant collections using the SAME
canonical embedder + collection schema as the ReindexWorker
(`ontology_store.vector`), so the points are production-faithful.

Inputs (any combination):
  --sql-pairs FILE.json   list of {id, question, sql, instructions,
                          references_asset_rks[], concepts[], key_areas[],
                          source_provenance}                (see sql_pairs_csod.json)
  --cards FILE.json       list of {id, kind, title?, aliases[]?, body,
                          markings[]?, refs[]?, origin?, layer?, deprecated?}
  --cards-dir DIR         a semantic_layer tree of <kind>s/<id>.card.md files
                          (YAML frontmatter + markdown body). metrics/ → metric,
                          instructions/ → instruction, etc.

Target Qdrant + embeddings come from the standard env vars (same as the worker):
    QDRANT_URL  (e.g. http://10.0.0.5:6333)  — OR —  QDRANT_HOST + QDRANT_PORT
    QDRANT_API_KEY (optional)
    OPENAI_API_KEY (or EMBEDDING_API_KEY);  EMBEDDING_MODEL=text-embedding-3-small

Run (real):
    cd nexcraft
    set -a; source packages/nexcraft-jobs/.env; set +a       # QDRANT_* + OPENAI_API_KEY
    .venv/bin/python packages/ontology-store/scripts/ingest_curated.py \
        --tenant acme --env prod \
        --sql-pairs packages/ontology-store/scripts/sql_pairs_csod.json \
        --cards-dir semantic_layer

Run (dry — no network, no embeddings; shows what WOULD upsert):
    python3 packages/ontology-store/scripts/ingest_curated.py --tenant acme \
        --sql-pairs scripts/sql_pairs_csod.json --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("ingest_curated")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

# semantic_layer dir name → card kind (mirrors ontology_pipeline.cards.loader)
_DIRNAME_TO_KIND = {
    "object_types": "object_type", "interfaces": "interface",
    "causal_nodes": "causal_node", "derived_states": "derived_state",
    "actions": "action", "metrics": "metric", "events": "event",
    "instructions": "instruction", "key_areas": "key_area",
}


# ── card narrative/payload (kept inline so --dry-run needs no heavy imports) ──
def _card_text(body: str, aliases: list[str] | None) -> str:
    body = (body or "").strip()
    if aliases:
        return f"{body}\n\nAliases: {', '.join(aliases)}"
    return body


def _card_payload(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "layer": card.get("layer", "semantic"),
        "kind": card["kind"],
        "card_id": card["id"],
        "title": card.get("title"),
        "aliases": card.get("aliases") or [],
        "markings": card.get("markings") or [],
        "refs": card.get("refs") or [],
        "origin": card.get("origin", "tenant"),
        "deprecated": bool(card.get("deprecated", False)),
    }


def _sql_pair_payload(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "question": str(p.get("question") or ""),
        "sql": p.get("sql") or "",
        "instructions": str(p.get("instructions") or ""),
        "references_asset_rks": p.get("references_asset_rks") or [],
        "concepts": p.get("concepts") or [],
        "key_areas": p.get("key_areas") or [],
        "source_provenance": p.get("source_provenance") or "curated",
        "valid_for_lifecycle": p.get("valid_for_lifecycle") or "production",
    }


# ── loaders ──────────────────────────────────────────────────────────────────
def _load_json_list(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list, got {type(data).__name__}")
    return data


def _load_cards_dir(root: Path) -> list[dict[str, Any]]:
    """Parse a semantic_layer tree of <kind>s/<id>.card.md files into card dicts."""
    try:
        import yaml  # local import; only needed for --cards-dir (present in nexcraft/.venv)
    except ImportError as exc:
        raise SystemExit(
            f"--cards-dir needs PyYAML to parse .card.md frontmatter ({exc}). "
            "Run with nexcraft/.venv/bin/python, or use --cards FILE.json instead."
        ) from exc

    cards: list[dict[str, Any]] = []
    for kind_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        kind = _DIRNAME_TO_KIND.get(kind_dir.name)
        if kind is None:
            logger.debug("skip unknown card dir %s", kind_dir.name)
            continue
        for md in sorted(kind_dir.glob("*.card.md")):
            text = md.read_text(encoding="utf-8")
            m = _FRONTMATTER_RE.match(text)
            if m:
                fm = yaml.safe_load(m.group(1)) or {}
                body = m.group(2).strip()
            else:
                fm, body = {}, text.strip()
            card = {
                "id": fm.get("id") or md.stem.replace(".card", ""),
                "kind": fm.get("kind") or kind,
                "title": fm.get("title"),
                "aliases": fm.get("aliases") or [],
                "markings": fm.get("markings") or [],
                "refs": fm.get("refs") or [],
                "origin": fm.get("origin", "tenant"),
                "layer": fm.get("layer", "semantic"),
                "deprecated": bool(fm.get("deprecated", False)),
                "body": body,
            }
            cards.append(card)
    return cards


# ── main ─────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ingest curated SQL examples / metric / instruction cards into Qdrant.")
    ap.add_argument("--tenant", required=True, help="Tenant id (scopes cards_<tenant> / sql_pairs_<tenant>).")
    ap.add_argument("--env", default="prod", help="Env slug (unused for tenant collections; kept for indexer parity).")
    ap.add_argument("--sql-pairs", type=Path, help="JSON list of SQL example pairs.")
    ap.add_argument("--cards", type=Path, help="JSON list of card dicts (kind=metric/instruction/...).")
    ap.add_argument("--cards-dir", type=Path, help="semantic_layer dir of <kind>s/*.card.md files.")
    ap.add_argument("--limit", type=int, default=0, help="Cap items per source (0 = all).")
    ap.add_argument("--dry-run", action="store_true", help="Parse + report only; no embeddings, no network.")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")

    if not (args.sql_pairs or args.cards or args.cards_dir):
        ap.error("provide at least one of --sql-pairs / --cards / --cards-dir")

    # Gather inputs (fail fast on bad files before touching the network).
    sql_pairs = _load_json_list(args.sql_pairs) if args.sql_pairs else []
    cards: list[dict[str, Any]] = []
    if args.cards:
        cards += _load_json_list(args.cards)
    if args.cards_dir:
        cards += _load_cards_dir(args.cards_dir)
    if args.limit:
        sql_pairs, cards = sql_pairs[: args.limit], cards[: args.limit]

    logger.info("tenant=%s  sql_pairs=%d  cards=%d  (dry_run=%s)",
                args.tenant, len(sql_pairs), len(cards), args.dry_run)

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
        client = QdrantClientFactory.get()       # reads QDRANT_URL / QDRANT_HOST+PORT / QDRANT_API_KEY
        embedder = OpenAIEmbedder()              # reads OPENAI_API_KEY / EMBEDDING_API_KEY / EMBEDDING_MODEL
        indexer = HierarchyVectorIndexer(qdrant_client=client, embedder=embedder, env=args.env)
        indexer.ensure_tenant_collections(args.tenant)

    counts = {"sql_pairs": 0, "cards": 0, "skipped": 0}

    # ── SQL examples → SQL_PAIRS_<tenant> ───────────────────────────────────
    for p in sql_pairs:
        pid = str(p.get("id") or p.get("sql_pair_id") or "").strip()
        question = str(p.get("question") or "").strip()
        if not pid or not question:
            logger.warning("skip sql pair (missing id/question): %r", {k: p.get(k) for k in ("id", "question")})
            counts["skipped"] += 1
            continue
        payload = _sql_pair_payload(p)
        # Embed question + instructions together (SQL_PAIRS narrative_fields).
        text = question if not payload["instructions"] else f"{question}\n\nInstructions: {payload['instructions']}"
        if indexer:
            indexer.upsert_sql_pair(args.tenant, sql_pair_id=pid, question=text, payload=payload)
        else:
            logger.info("[dry] sql_pair %-40s refs=%d", pid, len(payload["references_asset_rks"]))
        counts["sql_pairs"] += 1

    # ── Metric / instruction / any card → CARDS_<tenant> ────────────────────
    for c in cards:
        cid = str(c.get("id") or "").strip()
        kind = str(c.get("kind") or "").strip()
        body = str(c.get("body") or "").strip()
        if not cid or not kind or not body:
            logger.warning("skip card (missing id/kind/body): %r", {k: c.get(k) for k in ("id", "kind")})
            counts["skipped"] += 1
            continue
        point_id = f"{args.tenant}::semantic::{kind}::{cid}"
        text = _card_text(body, c.get("aliases"))
        payload = _card_payload({**c, "id": cid, "kind": kind})
        if indexer:
            indexer.upsert_card(args.tenant, point_id=point_id, body=text, payload=payload)
        else:
            logger.info("[dry] card %-12s %-40s aliases=%d", kind, cid, len(c.get("aliases") or []))
        counts["cards"] += 1

    logger.info("done: sql_pairs=%d cards=%d skipped=%d", counts["sql_pairs"], counts["cards"], counts["skipped"])
    if not args.dry_run:
        logger.info("collections: sql_pairs_%s, cards_%s", args.tenant, args.tenant)
    return 0


if __name__ == "__main__":
    sys.exit(main())
