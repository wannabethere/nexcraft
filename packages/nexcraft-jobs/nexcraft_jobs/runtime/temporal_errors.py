"""Map nexcraft (and recipe) failures to Temporal ApplicationError retry semantics."""

from __future__ import annotations

from typing import NoReturn

import nexcraft.errors as nexcraft_errors
from temporalio.exceptions import ApplicationError


_NON_RETRYABLE_TYPES: tuple[type[BaseException], ...] = (
    nexcraft_errors.BudgetExceededError,
    nexcraft_errors.SourceSyntaxError,
    nexcraft_errors.AuthenticationError,
    nexcraft_errors.ConfigurationError,
    nexcraft_errors.CancelledError,
    nexcraft_errors.SchemaMismatchError,
    ValueError,
    KeyError,
)


def raise_application_error(
    exc: BaseException,
    *,
    non_retryable: bool | None = None,
    chain_from: BaseException | None = None,
) -> NoReturn:
    """Raise Temporal ApplicationError; optionally chain from an upstream cause (e.g. MemoryError)."""
    if isinstance(exc, ApplicationError):
        raise exc
    name = type(exc).__name__
    nr = non_retryable
    if nr is None:
        nr = isinstance(exc, _NON_RETRYABLE_TYPES)
    application_exc = ApplicationError(str(exc), type=name, non_retryable=nr)
    if chain_from is not None:
        raise application_exc from chain_from
    raise application_exc from exc
