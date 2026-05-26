"""Pipeline configuration models.

Loaded from a YAML file by `PipelineConfig.load(path)`. Environment-variable
substitution via `${VAR}` syntax is supported in string fields.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    """Recursively expand ${ENV_VAR} references in strings of a config tree."""
    if isinstance(value, str):
        def _sub(match: re.Match[str]) -> str:
            var = match.group(1)
            resolved = os.environ.get(var)
            if resolved is None:
                raise ValueError(f"Environment variable {var!r} referenced but not set")
            return resolved
        return _ENV_VAR_PATTERN.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class PostgresConnection(BaseModel):
    """Postgres connection details."""
    host: str
    port: int = 5432
    database: str
    user: str
    password: str
    sslmode: str = "prefer"

    def dsn(self) -> str:
        return (
            f"host={self.host} port={self.port} dbname={self.database} "
            f"user={self.user} password={self.password} sslmode={self.sslmode}"
        )


class LocalFilesSource(BaseModel):
    """Filesystem-only source — preview / dry-run mode.

    Points at a directory containing:
      - `schema_sql`: a pg_dump-style .sql file with CREATE TABLE + COMMENTs.
      - `data_dir`:   optional. When set, the foundry profiling pre-pass
                       reads `<data_dir>/<table>.csv` instead of querying
                       a live database.
      - `manifest`:   optional JSON file supplementing PK/FK info per table.

    No network. No psycopg. Use in conjunction with `output.kind=preview`
    so all artifacts land under `output/preview/` for manual inspection.
    """
    schema_sql: Path = Field(
        description="Path to a pg_dump-style .sql file (CREATE TABLE + COMMENTs).",
    )
    data_dir: Path | None = Field(
        default=None,
        description=(
            "Optional directory containing one CSV per table "
            "(`<table>.csv`). Required when `compute_column_stats=true`."
        ),
    )
    manifest: Path | None = Field(
        default=None,
        description=(
            "Optional JSON file with per-table PK/FK hints. "
            "Format: {tables: {table_name: {pk: 'col' | [cols], fk: [cols]}}}."
        ),
    )
    catalog_name: str | None = Field(
        default=None,
        description=(
            "Synthetic catalog name (= database name in the asset rk). "
            "Defaults to the schema_sql file's stem."
        ),
    )


class SourceConfig(BaseModel):
    """Identifies one logical source instance to introspect."""
    source_id: str
    org_id: str
    kind: Literal["postgres", "local_files"] = "postgres"
    # When kind="postgres" this is required. When kind="local_files" it's
    # optional and ignored (a placeholder satisfies the type).
    connection: PostgresConnection | None = None
    local: LocalFilesSource | None = None
    schemas: list[str] = Field(default_factory=lambda: ["public"])

    @model_validator(mode="after")
    def _validate_kind(self) -> "SourceConfig":
        if self.kind == "postgres":
            if self.connection is None:
                raise ValueError(
                    "source.kind='postgres' requires source.connection"
                )
            return self
        if self.kind == "local_files":
            if self.local is None:
                raise ValueError(
                    "source.kind='local_files' requires source.local "
                    "(schema_sql / data_dir / manifest)."
                )
            return self
        raise ValueError(
            f"Unsupported source kind {self.kind!r}. Supported: "
            "'postgres', 'local_files'. Snowflake / Salesforce / ServiceNow "
            "are roadmap."
        )


class TableFilter(BaseModel):
    """Optional inclusion/exclusion of tables.

    If both `include` and `include_patterns` are empty, ALL tables in the
    configured schemas are processed (the user's stated default).
    """
    include: list[str] = Field(default_factory=list, description="Exact table names to include.")
    exclude: list[str] = Field(default_factory=list, description="Exact table names to exclude.")
    include_patterns: list[str] = Field(default_factory=list,
                                        description="Glob patterns to include (e.g. 'csod_*').")
    exclude_patterns: list[str] = Field(default_factory=list,
                                        description="Glob patterns to exclude.")

    def is_configured(self) -> bool:
        """True if any filter rule is set; False means 'process all'."""
        return any([self.include, self.exclude, self.include_patterns, self.exclude_patterns])


class SemanticLayerConfig(BaseModel):
    """Where to find the existing card index + key_areas vocab for annotation."""
    cards_dir: Path | None = Field(
        default=None,
        description="Directory containing semantic_layer/<kind>s/*.card.md files. If absent, annotation is skipped.",
    )
    key_areas_vocab_path: Path | None = Field(
        default=None,
        description="Path to key_areas_vocab.yaml. If absent, key_areas annotation is skipped.",
    )


class OutputConfig(BaseModel):
    """Where to write pipeline artifacts.

    kind:
      - 'filesystem'       — write MDL + annotation JSON files under base_dir.
      - 'hierarchy_store'  — write into the ontology-store Postgres tables.
                             Requires ONTOLOGY_STORE_URL env var.
      - 'tee'              — write to BOTH filesystem and hierarchy_store.
      - 'preview'          — local-only sink. Writes everything FilesystemSink
                             writes PLUS dumps of every Postgres row + Qdrant
                             event that would have been produced. Use with
                             `source.kind=local_files` for a fully offline run.
                             Output tree:
                               <base_dir>/mdl/.../
                               <base_dir>/postgres/<table>/...    (PG-bound rows)
                               <base_dir>/qdrant/<collection>/... (Qdrant events)
                               <base_dir>/reindex_queue.jsonl     (would-be tasks)
    """
    kind: Literal["filesystem", "hierarchy_store", "tee", "preview"] = "filesystem"
    base_dir: Path = Field(default=Path("./out"),
                           description="Used by filesystem / tee / preview. For "
                           "hierarchy_store, a small marker tree under cwd is "
                           "used for run-state only.")


class LLMConfig(BaseModel):
    """LLM provider configuration. DeepSeek V3 via OpenAI-compatible API (foundry provider)."""
    provider: Literal["openai"] = "openai"
    model: str = "deepseek-chat"
    api_key_env: str = "DEEPSEEK_API_KEY"
    base_url_env: str = "DEEPSEEK_BASE_URL"
    base_url_default: str = "https://api.deepseek.com"


class PipelineBehavior(BaseModel):
    """Pipeline behavior switches."""
    fill_descriptions: bool = Field(
        default=True,
        description="If True, LLM fills missing table/column descriptions (basic).",
    )
    rich_description: bool = Field(
        default=False,
        description=(
            "If True, run the RichDescriptionEnricher to generate business_purpose, "
            "use_cases, update_frequency, key_relationships, etc. Strict superset of "
            "fill_descriptions — when True, basic fill_descriptions is skipped."
        ),
    )
    enrich_column_semantics: bool = Field(
        default=False,
        description="If True, run ColumnSemanticsEnricher (semantic_unit + business_meaning + is_business_key per column).",
    )
    compute_column_stats: bool = Field(
        default=True,
        description=(
            "If True, runs `ontology_pipeline.profile.TableProfiler` at "
            "introspect time for every table. Builds a foundry "
            "TabularContextBundle (per-column profile + top-k + row sample), "
            "persists aggregates immediately, threads the bundle through "
            "EnrichmentContext as grounding, and after the data_protection "
            "stage runs writes value-bearing samples for PII-cleared columns. "
            "Default on — deterministic and cheap (~1 SELECT-LIMIT per table)."
        ),
    )
    column_stats_sample_limit: int = Field(
        default=1000, ge=10, le=100_000,
        description=(
            "Row cap on the introspect-time sample used for column profiling. "
            "Bigger sample → tighter aggregates at linear cost."
        ),
    )
    enrich_data_protection: bool = Field(
        default=False,
        description=(
            "If True, run DataProtectionEnricher (is_pii / pii_categories / sensitivity_class per column "
            "+ asset-level RLS/CLS hints in side_output)."
        ),
    )
    infer_relationships: bool = Field(
        default=False,
        description=(
            "If True, run RelationshipInferenceEnricher for tables that lack any declared FK. "
            "Outputs land in side_output and high-confidence ones are written to MDL columns' "
            "properties.references."
        ),
    )
    enrich_causal_dependencies: bool = Field(
        default=False,
        description=(
            "If True, run CausalDependencyEnricher. Proposes causal_node participation roles "
            "(subject/outcome/mediator/moderator) for the asset against the tenant vocab, plus "
            "causal candidate edges (subject→predicate→object) with column-level evidence. "
            "LLM-driven (statistical causal discovery in ontology_foundry.causal runs on data, "
            "not metadata, and is a separate downstream pass)."
        ),
    )
    propose_new_causal_nodes: bool = Field(
        default=False,
        description=(
            "If True (and enrich_causal_dependencies), the LLM may draft NEW causal_node cards "
            "when existing vocab doesn't fit. Drafts land in side_output for human review — "
            "never auto-applied."
        ),
    )
    induce_relation_schema: bool = Field(
        default=False,
        description=(
            "If True, run `ontology_foundry.relations.induce_schema` as a "
            "post-pass over every inferred relationship the run produced. "
            "Canonicalizes predicate surfaces, aggregates (subject_type, "
            "object_type), and persists the resulting `RelationType` rows in "
            "ontology-store's `relation_type` table. Each contributing "
            "`lineage_edge` is linked via `predicate_id`. "
            "Requires the LLM (foundry uses it for predicate canonicalization)."
        ),
    )
    relation_induction_min_support: int = Field(
        default=2, ge=1, le=100,
        description=(
            "Minimum number of edges a canonicalized predicate must accumulate "
            "before it lands as a `RelationType` row. Default 2 — keep rare/spurious "
            "predicates out of the TBox without filtering too aggressively in "
            "small-corpus runs."
        ),
    )
    enrich_cross_asset_causal: bool = Field(
        default=False,
        description=(
            "If True, run the cross-asset causal stage AFTER all per-asset enrichment. "
            "Clusters assets by shared concepts / key_areas and proposes causal hypotheses "
            "linking two assets (or an asset to a causal_node card). Candidates land in "
            "causal_candidate via the existing sink path."
        ),
    )
    cross_asset_cluster_max_size: int = Field(
        default=5, ge=2, le=20,
        description="Per-cluster asset cap for the cross-asset LLM call (token-budget guard).",
    )
    annotate: bool = Field(
        default=True,
        description="If True, LLM proposes concepts/key_areas/causal_relations.",
    )
    concepts_source: Literal["ner_then_llm", "llm_only", "ner_only"] = Field(
        default="ner_then_llm",
        description=(
            "How concepts/key_areas/causal_relations annotations are produced. "
            "'ner_then_llm' (default): foundry's SeedFirstEntityLinker runs a "
            "deterministic pre-pass against the tenant card lexicon; its "
            "candidates ground the LLM call which confirms/extends. "
            "'ner_only': skip the LLM; emit only what the linker matched (cheapest, "
            "lowest recall). 'llm_only': legacy path — LLM with no NER grounding."
        ),
    )
    re_enrich_unchanged: bool = Field(
        default=False,
        description="If True, re-run annotations even when content hash matches a prior run.",
    )
    parallelism: int = Field(default=4, ge=1, le=32,
                             description="(Reserved; v1 pipeline is sequential.)")


class PipelineConfig(BaseModel):
    """Top-level pipeline config; load from YAML via .load()."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    source: SourceConfig
    tables: TableFilter = Field(default_factory=TableFilter)
    semantic_layer: SemanticLayerConfig = Field(default_factory=SemanticLayerConfig)
    output: OutputConfig
    llm: LLMConfig = Field(default_factory=LLMConfig)
    pipeline: PipelineBehavior = Field(default_factory=PipelineBehavior)

    @model_validator(mode="before")
    @classmethod
    def _coerce_null_nested_sections(cls, data: Any) -> Any:
        """YAML keys with only comments parse as null; treat as omitted (use defaults)."""
        if not isinstance(data, dict):
            return data
        for key in ("tables", "semantic_layer", "llm", "pipeline"):
            if data.get(key) is None:
                data[key] = {}
        return data

    @classmethod
    def load(cls, path: str | Path) -> "PipelineConfig":
        """Load and validate a config from a YAML file. Expands ${ENV_VAR} refs."""
        path = Path(path)
        with path.open("r") as fh:
            raw = yaml.safe_load(fh)
        if raw is None:
            raise ValueError(f"Config file at {path} is empty")
        expanded = _expand_env(raw)
        return cls.model_validate(expanded)
