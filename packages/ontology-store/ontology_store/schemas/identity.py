"""Pydantic schemas for T0/T1/T2 identity records — write inputs."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class OrganizationIn(BaseModel):
    org_id: str
    display_name: str
    legal_name: str | None = None
    industry: str | None = None
    sub_industry: str | None = None
    headquarters: dict[str, Any] | None = None
    primary_language: str | None = None
    supported_languages: list[str] | None = None
    locale_defaults: dict[str, Any] | None = None
    compliance_regimes: list[str] | None = None
    org_size_class: str | None = None
    business_context: str | None = None


class SourceIn(BaseModel):
    source_id: str
    org_id: str
    kind: str = "postgres"
    instance_name: str
    display_name: str
    environment: str = "prod"
    role: str = "analytical_warehouse"
    purpose: str | None = None
    business_context: str | None = None
    business_owner: str | None = None
    technical_owner: str | None = None
    region_id: str | None = None
    vendor_details: dict[str, Any] | None = None
    refresh_cadence: dict[str, Any] | None = None
    declared_residency: list[str] | None = None
    residency_check_mode: str = "best_effort"
    sensitivity_class: str | None = None
    pii_categories: list[str] | None = None
    notes: str | None = None


class CatalogIn(BaseModel):
    """Input shape for upserting a catalog. catalog_uid is derived if not provided."""
    source_id: str
    catalog_name: str
    display_name: str | None = None
    description: str | None = None
    purpose: str | None = None
    lifecycle_stage: str = "production"
    access_pattern: str = "read_only"
    business_owner: str | None = None
    technical_owner: str | None = None
    sensitivity_class: str | None = None
    pii_categories: list[str] | None = None
    managed_by: str | None = None
    dbt_project_ref: str | None = None
    notes: str | None = None

    @property
    def catalog_uid(self) -> str:
        return f"{self.source_id}::catalog::{self.catalog_name}"
