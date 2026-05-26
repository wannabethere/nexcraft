from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ontology_foundry.llm.provider import ModelRole

RuntimeKind = Literal["native", "langchain", "skill", "foundry"]
ModelKind = Literal["openai", "claude", "gemini", "deepseek"]

_SKILL_PATH = Path(__file__).resolve().parent.parent / "weekend_skill_system.txt"


@dataclass(frozen=True)
class ModelSpec:
    kind: ModelKind
    label: str
    env_key: str
    default_model: str
    base_url_env: str | None = None
    default_base_url: str | None = None


MODEL_SPECS: dict[ModelKind, ModelSpec] = {
    "openai": ModelSpec(
        kind="openai",
        label="OpenAI",
        env_key="OPENAI_API_KEY",
        default_model="gpt-4o-mini",
        base_url_env=None,
        default_base_url="https://api.openai.com/v1",
    ),
    "deepseek": ModelSpec(
        kind="deepseek",
        label="DeepSeek",
        env_key="DEEPSEEK_API_KEY",
        default_model="deepseek-chat",
        base_url_env="DEEPSEEK_BASE_URL",
        default_base_url="https://api.deepseek.com",
    ),
    "claude": ModelSpec(
        kind="claude",
        label="Claude",
        env_key="ANTHROPIC_API_KEY",
        default_model="claude-sonnet-4-20250514",
    ),
    "gemini": ModelSpec(
        kind="gemini",
        label="Gemini",
        env_key="GOOGLE_API_KEY",
        default_model="gemini-2.0-flash",
    ),
}


def _skill_system_text() -> str:
    if _SKILL_PATH.is_file():
        return _SKILL_PATH.read_text(encoding="utf-8").strip()
    return (
        "You are a tabular QA skill. Answer only from context. "
        'Reply JSON: {"answer": "...", "evidence_column": "..."}'
    )


def _user_prompt(context: str, question: str) -> str:
    return (
        "Answer ONLY from the context below. If the fact is missing, set answer to UNKNOWN.\n"
        'Reply with a single JSON object: {"answer": "<value>", "evidence_column": "<column>"}\n\n'
        f"CONTEXT:\n{context}\n\n"
        f"QUESTION: {question}\n"
    )


def model_available(kind: ModelKind) -> bool:
    spec = MODEL_SPECS[kind]
    if kind == "gemini":
        return bool(os.environ.get(spec.env_key) or os.environ.get("GEMINI_API_KEY"))
    return bool(os.environ.get(spec.env_key))


def resolve_api_key(kind: ModelKind) -> str | None:
    spec = MODEL_SPECS[kind]
    if kind == "gemini":
        return os.environ.get(spec.env_key) or os.environ.get("GEMINI_API_KEY")
    return os.environ.get(spec.env_key)


def resolve_model_name(kind: ModelKind, override: str | None) -> str:
    if override:
        return override
    env_map = {
        "openai": "WEEKEND_OPENAI_MODEL",
        "deepseek": "WEEKEND_DEEPSEEK_MODEL",
        "claude": "WEEKEND_CLAUDE_MODEL",
        "gemini": "WEEKEND_GEMINI_MODEL",
    }
    from_env = os.environ.get(env_map[kind])
    if from_env:
        return from_env
    return MODEL_SPECS[kind].default_model


class ChatRunner:
    """Unified completion entry for native / langchain / skill / foundry."""

    def __init__(
        self,
        *,
        model_kind: ModelKind,
        runtime: RuntimeKind,
        model_name: str | None = None,
        temperature: float = 0.0,
    ) -> None:
        self.model_kind = model_kind
        self.runtime = runtime
        self.spec = MODEL_SPECS[model_kind]
        self.model_name = resolve_model_name(model_kind, model_name)
        self.temperature = temperature
        self._api_key = resolve_api_key(model_kind)
        if not self._api_key:
            raise RuntimeError(
                f"{self.spec.env_key} (or GEMINI_API_KEY for gemini) is not set for {model_kind}"
            )

    def complete(self, *, context: str, question: str) -> str:
        user = _user_prompt(context, question)
        if self.runtime == "skill":
            return self._complete_skill(user)
        if self.runtime == "langchain":
            return self._complete_langchain(user)
        if self.runtime == "foundry":
            return self._complete_foundry(user)
        return self._complete_native(user)

    # ── native SDKs ─────────────────────────────────────────────────────

    def _complete_native(self, user_prompt: str) -> str:
        if self.model_kind in ("openai", "deepseek"):
            return self._native_openai_compat(user_prompt, system=None)
        if self.model_kind == "claude":
            return self._native_anthropic(user_prompt, system=None)
        return self._native_gemini(user_prompt, system=None)

    def _complete_skill(self, user_prompt: str) -> str:
        system = _skill_system_text()
        if self.model_kind in ("openai", "deepseek"):
            return self._native_openai_compat(user_prompt, system=system)
        if self.model_kind == "claude":
            return self._native_anthropic(user_prompt, system=system)
        return self._native_gemini(user_prompt, system=system)

    def _native_openai_compat(self, user_prompt: str, *, system: str | None) -> str:
        from openai import OpenAI

        base_url = self.spec.default_base_url
        if self.spec.base_url_env:
            base_url = os.environ.get(self.spec.base_url_env, base_url)
        client = OpenAI(api_key=self._api_key, base_url=base_url)
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_prompt})
        resp = client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            temperature=self.temperature,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content
        return content if isinstance(content, str) else ""

    def _native_anthropic(self, user_prompt: str, *, system: str | None) -> str:
        import anthropic

        client = anthropic.Anthropic(api_key=self._api_key)
        kwargs: dict[str, Any] = {
            "model": self.model_name,
            "max_tokens": 1024,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if system:
            kwargs["system"] = system
        msg = client.messages.create(**kwargs)
        parts = []
        for block in msg.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "".join(parts)

    def _native_gemini(self, user_prompt: str, *, system: str | None) -> str:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self._api_key)
        config = types.GenerateContentConfig(
            temperature=self.temperature,
            response_mime_type="application/json",
        )
        if system:
            config.system_instruction = system
        resp = client.models.generate_content(
            model=self.model_name,
            contents=user_prompt,
            config=config,
        )
        return resp.text or ""

    # ── langchain ───────────────────────────────────────────────────────

    def _complete_langchain(self, user_prompt: str) -> str:
        llm = self._build_langchain_chat()
        from langchain_core.messages import HumanMessage, SystemMessage

        messages = [HumanMessage(content=user_prompt)]
        resp = llm.invoke(messages)
        content = getattr(resp, "content", resp)
        if isinstance(content, list):
            return "".join(
                p.get("text", "") if isinstance(p, dict) else str(p) for p in content
            )
        return str(content)

    def _build_langchain_chat(self) -> Any:
        if self.model_kind in ("openai", "deepseek"):
            from langchain_openai import ChatOpenAI

            base_url = self.spec.default_base_url
            if self.spec.base_url_env:
                base_url = os.environ.get(self.spec.base_url_env, base_url)
            return ChatOpenAI(
                model=self.model_name,
                temperature=self.temperature,
                openai_api_key=self._api_key,
                base_url=base_url,
                model_kwargs={"response_format": {"type": "json_object"}},
            )
        if self.model_kind == "claude":
            from langchain_anthropic import ChatAnthropic

            return ChatAnthropic(
                model=self.model_name,
                temperature=self.temperature,
                api_key=self._api_key,
            )
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=self.model_name,
            temperature=self.temperature,
            google_api_key=self._api_key,
        )

    # ── foundry ModelProvider (OpenAI-compatible only) ───────────────

    def _complete_foundry(self, user_prompt: str) -> str:
        if self.model_kind not in ("openai", "deepseek"):
            raise RuntimeError(
                f"foundry runtime only supports openai/deepseek via OpenAIChatProvider, not {self.model_kind}"
            )
        from pydantic import BaseModel

        from ontology_foundry.llm.openai_provider import OpenAIChatProvider

        class QAResponse(BaseModel):
            answer: str
            evidence_column: str = ""

        base_url = self.spec.default_base_url
        if self.spec.base_url_env:
            base_url = os.environ.get(self.spec.base_url_env, base_url)
        provider = OpenAIChatProvider(
            model=self.model_name,
            api_key=self._api_key,
            base_url=base_url,
        )
        return provider.complete(
            ModelRole.VALIDATOR,
            user_prompt,
            response_format=QAResponse,
        )


def runtime_available(runtime: RuntimeKind, model_kind: ModelKind) -> tuple[bool, str]:
    if runtime == "foundry" and model_kind not in ("openai", "deepseek"):
        return False, "foundry uses ontology_foundry OpenAIChatProvider (openai/deepseek only)"
    if runtime == "langchain":
        try:
            if model_kind in ("openai", "deepseek"):
                import langchain_openai  # noqa: F401
            elif model_kind == "claude":
                import langchain_anthropic  # noqa: F401
            else:
                import langchain_google_genai  # noqa: F401
        except ImportError as e:
            return False, f"langchain extra missing: {e}"
    if runtime in ("native", "skill"):
        if model_kind in ("openai", "deepseek"):
            try:
                import openai  # noqa: F401
            except ImportError as e:
                return False, str(e)
        elif model_kind == "claude":
            try:
                import anthropic  # noqa: F401
            except ImportError as e:
                return False, str(e)
        else:
            try:
                from google import genai  # noqa: F401
            except ImportError as e:
                return False, str(e)
    if runtime == "foundry":
        try:
            import openai  # noqa: F401
        except ImportError as e:
            return False, str(e)
    return True, "ok"
