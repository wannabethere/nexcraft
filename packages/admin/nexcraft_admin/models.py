from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExtractorConfig(BaseModel):
    kind: str
    enabled: bool = True
    options: dict[str, str] = Field(default_factory=dict)


class RetrievalAgentConfig(BaseModel):
    kind: str
    enabled: bool = True
    options: dict[str, str] = Field(default_factory=dict)


class OntologyFoundryConfig(BaseModel):
    extractors: list[ExtractorConfig] = Field(default_factory=list)
    retrieval_agents: list[RetrievalAgentConfig] = Field(default_factory=list)


class NexcraftAdminSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEXCRAFT_", extra="ignore")

    environment: str = "local"
    default_llm_provider: str = "openai"
    default_llm_model: str = "gpt-4o-mini"
