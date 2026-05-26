from __future__ import annotations

from collections import defaultdict

from ontology_foundry.causal.models import CausalEdgeFinding


def edge_consensus(
    runs: list[list[CausalEdgeFinding]],
    *,
    min_distinct_algorithms: int = 2,
) -> tuple[list[CausalEdgeFinding], list[tuple[str, str, int]]]:
    """
    Intersect multiple algorithm outputs (ingestion §5.3 consensus narrative).
    Counts distinct `algorithm` labels per directed edge; keeps edges whose
    algorithm count meets ``min_distinct_algorithms``.
    """
    votes: dict[tuple[str, str], set[str]] = defaultdict(set)
    for run in runs:
        for e in run:
            votes[(e.source, e.target)].add(e.algorithm)

    tallies: list[tuple[str, str, int]] = []
    agreed: list[CausalEdgeFinding] = []
    for (src, tgt), algs in votes.items():
        tallies.append((src, tgt, len(algs)))
        if len(algs) >= min_distinct_algorithms:
            agreed.append(
                CausalEdgeFinding(
                    source=src,
                    target=tgt,
                    algorithm="consensus",
                    diagnostics={"algorithms": sorted(algs)},
                )
            )
    return agreed, tallies
