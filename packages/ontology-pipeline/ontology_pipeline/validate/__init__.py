"""Validation stages — post-enrichment passes that *check* what the LLM proposed.

The LLM enrichment stages emit hypotheses (inferred FKs, causal candidates,
data-protection hints). The validate package runs statistical / structural
checks against actual source data to decide whether to promote a hypothesis,
reject it, or flag it for human review.

v1 ships:

  - `CausalValidator` — pulls sample data for each pending `causal_candidate`
    row and runs `ontology_foundry.causal` discovery + refutation against it.
    Same-asset candidates are testable directly; cross-asset / causal-node
    objects are recorded as `inconclusive` for v1 with a reason.

These run as separate processes (not inline with enrichment), so a failing
data pull, a slow algorithm, or a missing optional dependency never blocks
the main pipeline.
"""
from ontology_pipeline.validate.causal_validation import (
    CausalSampler,
    CausalTestSuite,
    CausalValidator,
    DefaultCausalTestSuite,
    ValidationOutcome,
)

__all__ = [
    "CausalValidator",
    "CausalSampler",
    "CausalTestSuite",
    "DefaultCausalTestSuite",
    "ValidationOutcome",
]
