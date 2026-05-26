"""Temporal activities that execute dstools registry tools (SQL templates + Python).

Registered on the nexcraft-jobs worker alongside recipe and multihop activities.
Activity type names remain ``dstools.run_sql_template`` and ``dstools.run_python_tool``
for compatibility with existing Temporal workflow code.
"""

from __future__ import annotations

from typing import Any

from temporalio import activity

from dstools.contracts.inputs import PythonToolInput, SqlTemplateInput
from dstools.contracts.outputs import ToolOutput
from dstools.execution.runner import execute_tool


@activity.defn(name="dstools.run_sql_template")
async def run_sql_template(payload: SqlTemplateInput) -> str:
    """Renders + translates the SQL. Warehouse execution is a downstream activity."""
    return execute_tool(payload.template, payload.params, dialect=payload.dialect)


@activity.defn(name="dstools.run_python_tool")
async def run_python_tool(payload: PythonToolInput) -> ToolOutput | dict[str, Any]:
    out = execute_tool(payload.tool, payload.params)
    if isinstance(out, ToolOutput):
        return out
    return {"tool": payload.tool, "raw": out}
