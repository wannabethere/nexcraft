"""Typed payloads for multi-hop Cornerstone-style jobs (SQL Server → DuckDB)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SqlServerFetchInput(BaseModel):
    """Run a read-only query through ODBC (pyodbc)."""

    odbc_driver: str = Field(
        default="ODBC Driver 17 for SQL Server",
        description="ODBC driver name as shown in `odbcinst -q -d`.",
    )
    server: str = Field(..., description="hostname or host,port")
    database: str
    uid: str
    pwd: str
    sql: str = Field(..., description="Literal SQL for this demo path.")
    trust_server_certificate: bool = Field(
        default=True,
        description="Sets TrustServerCertificate=yes for lab setups.",
    )
    encrypt: str = Field(default="yes", description="ODBC Encrypt= value")


class DuckDbCombineInput(BaseModel):
    """Register two row sets as DuckDB tables and run a combining query."""

    hop1_table: str = Field(default="hop1", description="Alias for first result set.")
    hop2_table: str = Field(default="hop2", description="Alias for second result set.")
    hop1_rows: list[dict]
    hop2_rows: list[dict]
    combine_sql: str = Field(
        ...,
        description="DuckDB SQL referencing hop1_table / hop2_table (unquoted identifiers).",
    )


class MultiHopWorkflowInput(BaseModel):
    """Two ODBC fetches then local DuckDB merge."""

    hop1: SqlServerFetchInput
    hop2: SqlServerFetchInput
    combine: DuckDbCombineInput | None = Field(
        default=None,
        description="If omitted, default join on User_ID for Cornerstone-shaped rows.",
    )


def default_cornerstone_combine_sql() -> str:
    return """
    SELECT
      h1.User_ID,
      h1.Training_Title,
      h1.Transcript_Status,
      h1.Completed_Date,
      h2.Position AS user_position,
      h2.Division AS user_division
    FROM hop1 h1
    LEFT JOIN hop2 h2 ON CAST(h1.User_ID AS VARCHAR) = CAST(h2.User_ID AS VARCHAR)
    """.strip()


def example_hop1_sql() -> str:
    """Transcript-style pull; replace table/column names with your CSOD schema."""
    return """
    SELECT TOP 200
      User_ID,
      Division,
      Position,
      Manager_Name,
      Training_Title,
      Training_Type,
      Transcript_Status,
      Assigned_Date,
      Due_Date,
      Completed_Date
    FROM csod_training_records
    """.strip()


def example_hop2_sql() -> str:
    """User/core hop; replace with your CSOD tables."""
    return """
    SELECT TOP 200
      User_ID,
      Division,
      Position,
      Email
    FROM csod_user_core
    """.strip()
