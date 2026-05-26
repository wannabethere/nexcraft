import os

import pytest

from ontology_foundry.llm.defaults import (
    DEFAULT_CHAT_MODEL,
    get_chat_api_key,
    get_chat_base_url,
    get_chat_model,
)


def test_deepseek_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    assert get_chat_api_key() is None
    assert get_chat_base_url() == "https://api.deepseek.com"
    assert get_chat_model() == DEFAULT_CHAT_MODEL


def test_deepseek_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/")
    monkeypatch.setenv("LLM_MODEL", "deepseek-reasoner")

    assert get_chat_api_key() == "test-key"
    assert get_chat_base_url() == "https://api.deepseek.com"
    assert get_chat_model() == "deepseek-reasoner"
