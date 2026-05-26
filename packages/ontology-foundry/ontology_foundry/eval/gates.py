from __future__ import annotations

from ontology_foundry.eval.models import EvalIssue, GateVerdict


def gate_nonempty_body(body: str, *, min_chars: int = 1) -> tuple[GateVerdict, list[EvalIssue]]:
    """eval_strategy §3.1 — body non-empty."""
    issues: list[EvalIssue] = []
    if len(body.strip()) < min_chars:
        issues.append(EvalIssue(code="EMPTY_BODY", message="Card body is empty"))
        return GateVerdict.FAIL, issues
    return GateVerdict.PASS, issues


def gate_id_pattern(card_id: str, pattern: str) -> tuple[GateVerdict, list[EvalIssue]]:
    """eval_strategy §3.1 — ID naming convention (caller supplies regex string)."""
    import re

    issues: list[EvalIssue] = []
    if not re.fullmatch(pattern, card_id):
        issues.append(
            EvalIssue(
                code="BAD_CARD_ID",
                message=f"card_id does not match pattern: {pattern}",
            )
        )
        return GateVerdict.FAIL, issues
    return GateVerdict.PASS, issues


def gate_refs_resolve(card_id: str, refs: list[str], resolver: set[str]) -> tuple[GateVerdict, list[EvalIssue]]:
    """eval_strategy §3.2 — every ref resolves."""
    issues: list[EvalIssue] = []
    for r in refs:
        if r not in resolver:
            issues.append(EvalIssue(code="DANGLING_REF", message=f"Unresolved ref {r!r} from {card_id}"))
    if issues:
        return GateVerdict.FAIL, issues
    return GateVerdict.PASS, []
