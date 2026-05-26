"""
Synthetic relational dataset: departments → employees → training assignments.

Lives under ``ontology_foundry.scenarios`` for integration demos and tests.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from pydantic import BaseModel, Field


class DepartmentRow(BaseModel):
    department_id: str
    name: str


class EmployeeRow(BaseModel):
    employee_id: str
    department_id: str
    tenure_months: float


class TrainingAssignmentRow(BaseModel):
    employee_id: str
    progress_percent: float
    is_overdue: int = Field(ge=0, le=1)


@dataclass
class OrgTrainingDataset:
    """Materialized rows plus flat analytic columns (aligned by employee order)."""

    departments: list[DepartmentRow]
    employees: list[EmployeeRow]
    training_assignments: list[TrainingAssignmentRow]
    tenure_months: list[float]
    progress_percent: list[float]
    is_overdue: list[int]
    seed_concepts: tuple[str, ...] = (
        "TrainingCompletionRate",
        "TenureMonths",
        "OverdueRisk",
        "SecurityAwarenessProgram",
    )


def build_org_training_dataset(
    *,
    n_employees: int = 220,
    n_departments: int = 6,
    seed: int = 42,
) -> OrgTrainingDataset:
    """
    Generate related tables with plausible dependence:
    higher tenure ↔ higher completion (with noise); overdue flags for low progress.
    """
    rng = np.random.default_rng(seed)

    departments = [
        DepartmentRow(
            department_id=f"dept-{i}",
            name=f"Department {chr(65 + i)}",
        )
        for i in range(n_departments)
    ]
    dept_ids = [d.department_id for d in departments]

    tenure_months: list[float] = []
    progress_percent: list[float] = []
    is_overdue: list[int] = []
    employees: list[EmployeeRow] = []
    training_assignments: list[TrainingAssignmentRow] = []

    for i in range(n_employees):
        eid = f"emp-{i:04d}"
        dept = dept_ids[int(rng.integers(0, n_departments))]
        tenure = float(rng.uniform(1.0, 120.0))
        employees.append(
            EmployeeRow(employee_id=eid, department_id=dept, tenure_months=tenure)
        )
        base = 25.0 + 0.45 * tenure + rng.normal(0.0, 12.0)
        prog = float(np.clip(base, 0.0, 100.0))
        overdue = 1 if prog < 88.0 and rng.random() > 0.35 else 0
        if overdue:
            prog = min(prog, float(rng.uniform(40.0, 85.0)))

        tenure_months.append(tenure)
        progress_percent.append(prog)
        is_overdue.append(overdue)
        training_assignments.append(
            TrainingAssignmentRow(
                employee_id=eid,
                progress_percent=prog,
                is_overdue=overdue,
            )
        )

    return OrgTrainingDataset(
        departments=departments,
        employees=employees,
        training_assignments=training_assignments,
        tenure_months=tenure_months,
        progress_percent=progress_percent,
        is_overdue=is_overdue,
    )


def dataset_to_extractable_bundle(ds: OrgTrainingDataset) -> dict[str, object]:
    """Shape suitable for JSON-ish export / downstream card builders."""
    return {
        "schema": {
            "departments": [r.model_dump() for r in ds.departments[:3]],
            "departments_total": len(ds.departments),
            "employees_sample": [r.model_dump() for r in ds.employees[:3]],
            "employees_total": len(ds.employees),
            "training_sample": [r.model_dump() for r in ds.training_assignments[:3]],
            "training_total": len(ds.training_assignments),
        },
        "seed_concepts": list(ds.seed_concepts),
        "flat_columns": {
            "tenure_months": ds.tenure_months[:20],
            "progress_percent": ds.progress_percent[:20],
            "is_overdue": ds.is_overdue[:20],
        },
    }
