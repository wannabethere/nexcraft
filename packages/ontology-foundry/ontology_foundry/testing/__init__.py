"""
Re-exports for test and demo code. Synthetic datasets live in
:mod:`ontology_foundry.scenarios`; import from here for a shorter path in tests.
"""

from ontology_foundry.scenarios.org_training_dataset import (
    DepartmentRow,
    EmployeeRow,
    OrgTrainingDataset,
    TrainingAssignmentRow,
    build_org_training_dataset,
    dataset_to_extractable_bundle,
)

__all__ = [
    "DepartmentRow",
    "EmployeeRow",
    "OrgTrainingDataset",
    "TrainingAssignmentRow",
    "build_org_training_dataset",
    "dataset_to_extractable_bundle",
]
