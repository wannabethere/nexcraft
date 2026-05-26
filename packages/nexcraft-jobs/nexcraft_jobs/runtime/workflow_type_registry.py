"""Maps workflow_type strings to input Pydantic models — Phase J.0."""
from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from nexcraft_jobs.runtime.multihop_models import MultiHopWorkflowInput
from nexcraft_jobs.runtime.pipeline_models import DstoolsPipelineInput
from nexcraft_jobs.schemas import FedSQLQueryInput

T = TypeVar("T", bound=type[BaseModel])

_REGISTRY: dict[str, type[BaseModel]] = {}


def register_workflow_type(name: str, input_model: type[BaseModel]) -> type[BaseModel]:
    """Decorator for workflow classes; also callable directly."""
    _REGISTRY[name] = input_model
    return input_model


def workflow_type(name: str, *, input_model: type[BaseModel]):
    """Class decorator: ``@workflow_type("nexcraft_fedsql_query", input_model=FedSQLQueryInput)``."""

    def decorator(cls: T) -> T:
        register_workflow_type(name, input_model)
        return cls

    return decorator


def get_workflow_input_model(workflow_type_name: str) -> type[BaseModel] | None:
    return _REGISTRY.get(workflow_type_name)


def registered_workflow_types() -> frozenset[str]:
    return frozenset(_REGISTRY)


def validate_workflow_input(workflow_type_name: str, payload: dict) -> BaseModel:
    model = get_workflow_input_model(workflow_type_name)
    if model is None:
        raise KeyError(f"Unknown workflow_type: {workflow_type_name!r}")
    return model.model_validate(payload)


# Built-in registrations (recipe inline/staged use dataclass payloads — not registered here)
register_workflow_type("nexcraft_fedsql_query", FedSQLQueryInput)
register_workflow_type("nexcraft_cornerstone_multihop", MultiHopWorkflowInput)
register_workflow_type("nexcraft_dstools_pipeline", DstoolsPipelineInput)
