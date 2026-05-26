from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa


@dataclass
class ComputeResult:
    """Output of Recipe.compute(). Carries the headline table plus optional auxiliaries.

    Matches the design contract in jobs/01-recipes.md. The primary table is the
    main result; auxiliaries hold related per-group/per-segment tables; metadata
    holds JSON-serializable summary stats.
    """

    primary: pa.Table
    auxiliaries: dict[str, pa.Table] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResultRef:
    uri: str
    job_id: str


@dataclass(frozen=True)
class ComputeResultHandle:
    """Cross-activity handle for a staged ComputeResult.

    Temporal can't serialize pa.Table over the wire, so the compute activity
    writes the primary/auxiliaries to Parquet under the staging root and passes
    URIs to the persist activity, which hydrates them back into a ComputeResult.
    """

    primary_uri: str
    auxiliary_uris: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
