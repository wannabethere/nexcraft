"""v0.0.1 bearer-token auth.

Validates an `authorization: Bearer <token>` Flight client header against a
list of allowed tokens (env-driven). Multi-tenant scoping, JWT issuance, mTLS,
and OAuth all land later; this is the smallest defensible shape that's not
"completely open".

Set `NEXCRAFT_DRIVER_TOKENS` to a comma-separated list of allowed tokens.
If unset, the driver runs in `--insecure` mode and warns at startup.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import pyarrow.flight as fl


@dataclass(frozen=True)
class TenantIdentity:
    tenant_id: str
    principal: str   # the bearer subject — token suffix in v0.0.1, JWT claim later


class AuthMiddleware(fl.ServerMiddleware):
    def __init__(self, identity: TenantIdentity) -> None:
        self.identity = identity


class AuthMiddlewareFactory(fl.ServerMiddlewareFactory):
    """Reads the Authorization header off each call, validates against an
    allowlist, and attaches a TenantIdentity to the call context."""

    HEADER = "authorization"

    def __init__(self, allowed_tokens: list[str] | None = None, *, insecure: bool = False) -> None:
        self._allowed = set(allowed_tokens or [])
        self._insecure = insecure
        if not self._allowed and not insecure:
            raise ValueError(
                "AuthMiddlewareFactory needs at least one allowed token, or insecure=True"
            )

    def start_call(self, info, headers):
        if self._insecure:
            return AuthMiddleware(TenantIdentity(tenant_id="default", principal="insecure"))
        auth = (headers.get(self.HEADER) or headers.get(self.HEADER.title()) or [""])
        token = auth[0] if isinstance(auth, list) else auth
        if not token.lower().startswith("bearer "):
            raise fl.FlightUnauthenticatedError("missing or malformed Authorization header")
        bearer = token[len("Bearer "):].strip()
        if bearer not in self._allowed:
            raise fl.FlightUnauthenticatedError("token not in allowlist")
        return AuthMiddleware(TenantIdentity(tenant_id="default", principal=bearer[:8]))


def factory_from_env() -> AuthMiddlewareFactory:
    """Build a middleware factory from env vars. Recognized:
      NEXCRAFT_DRIVER_TOKENS=tok1,tok2,...   — allowed bearer tokens
      NEXCRAFT_DRIVER_INSECURE=1             — disable auth entirely (dev only)
    """
    if os.environ.get("NEXCRAFT_DRIVER_INSECURE") == "1":
        return AuthMiddlewareFactory(insecure=True)
    raw = os.environ.get("NEXCRAFT_DRIVER_TOKENS", "")
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    return AuthMiddlewareFactory(allowed_tokens=tokens)
