class NexcraftError(Exception):
    """Base for all library errors."""


class TimeoutError(NexcraftError):
    """Deadline exceeded."""


class CancelledError(NexcraftError):
    """ctx.cancel was set or the stream was torn down due to cancellation."""


class ConnectionError(NexcraftError):
    """Could not acquire a connection or connection was lost mid-query."""


class AuthenticationError(ConnectionError):
    """Credentials rejected by source."""


class SourceSyntaxError(NexcraftError):
    def __init__(self, message: str, *, source_message: str) -> None:
        super().__init__(message)
        self.source_message = source_message


class SourceRuntimeError(NexcraftError):
    def __init__(self, message: str, *, source_message: str) -> None:
        super().__init__(message)
        self.source_message = source_message


class SchemaMismatchError(NexcraftError):
    """Result schema did not match describe()."""


class BudgetExceededError(NexcraftError):
    def __init__(
        self,
        message: str,
        *,
        budget_kind: str,
        limit: int,
        observed: int,
    ) -> None:
        super().__init__(message)
        self.budget_kind = budget_kind
        self.limit = limit
        self.observed = observed


class ConfigurationError(NexcraftError):
    """Invalid source descriptor or connection config."""


class InternalError(NexcraftError):
    """Unexpected failure inside nexcraft."""
