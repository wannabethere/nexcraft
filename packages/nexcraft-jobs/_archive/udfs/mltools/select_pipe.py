"""Reserved for select/pipeline-specific helpers mirroring genieml ``select_pipe``.

Catalog entries in this slice are covered by ``invoke_sql_function``; extend this module when
adding dedicated column-to-payload adapters.
"""

from __future__ import annotations

FUNCTION_NAMES: frozenset[str] = frozenset()

__all__ = ["FUNCTION_NAMES"]
