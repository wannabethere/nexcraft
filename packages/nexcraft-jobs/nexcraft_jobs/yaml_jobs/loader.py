"""Load job YAML from disk."""

from __future__ import annotations

from pathlib import Path

import yaml

from nexcraft_jobs.yaml_jobs.env_expand import expand_env_tokens
from nexcraft_jobs.yaml_jobs.spec import TemporalJobSpec


def load_job_file(path: str | Path) -> TemporalJobSpec:
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    expanded = expand_env_tokens(raw)
    data = yaml.safe_load(expanded)
    if not isinstance(data, dict):
        raise ValueError(f"Job YAML root must be a mapping, got {type(data).__name__}")
    spec = TemporalJobSpec.model_validate(data)
    try:
        from nexcraft_jobs.runtime.workflow_type_registry import validate_workflow_input

        validate_workflow_input(spec.workflow_type, spec.input)
    except KeyError:
        pass  # recipe workflows and legacy types skip registry validation
    except Exception:
        raise
    return spec
