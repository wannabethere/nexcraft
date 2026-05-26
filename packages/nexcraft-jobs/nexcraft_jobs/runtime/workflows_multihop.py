"""Temporal workflow: two SQL Server hops + DuckDB merge (small result sets)."""

from __future__ import annotations

from temporalio import workflow


@workflow.defn(name="nexcraft_cornerstone_multihop")
class CornerstoneMultiHopWorkflow:
    """Orchestrates hop1 fetch, hop2 fetch, then DuckDB combine."""

    @workflow.run
    async def run(self, payload: dict) -> dict:
        with workflow.unsafe.imports_passed_through():
            from datetime import timedelta

            from nexcraft_jobs.runtime.activities_multihop import duckdb_combine_hops, fetch_sql_server_rows
            from nexcraft_jobs.runtime.multihop_models import DuckDbCombineInput, default_cornerstone_combine_sql

            hop1_rows = await workflow.execute_activity(
                fetch_sql_server_rows,
                payload["hop1"],
                start_to_close_timeout=timedelta(minutes=10),
            )
            hop2_rows = await workflow.execute_activity(
                fetch_sql_server_rows,
                payload["hop2"],
                start_to_close_timeout=timedelta(minutes=10),
            )

            combine_payload = payload.get("combine") or None
            if combine_payload:
                merge_kw = dict(combine_payload)
                merge_kw["hop1_rows"] = hop1_rows
                merge_kw["hop2_rows"] = hop2_rows
                merge_kw.setdefault("hop1_table", "hop1")
                merge_kw.setdefault("hop2_table", "hop2")
                combine_model = DuckDbCombineInput(**merge_kw)
            else:
                combine_model = DuckDbCombineInput(
                    hop1_table="hop1",
                    hop2_table="hop2",
                    hop1_rows=hop1_rows,
                    hop2_rows=hop2_rows,
                    combine_sql=default_cornerstone_combine_sql(),
                )

            final_out = await workflow.execute_activity(
                duckdb_combine_hops,
                combine_model.model_dump(mode="json"),
                start_to_close_timeout=timedelta(minutes=5),
            )

            if hasattr(final_out, "model_dump"):
                final_dict = final_out.model_dump(by_alias=True, mode="json")
            else:
                final_dict = final_out  # type: ignore[assignment]

            return {
                "hop1_row_count": len(hop1_rows),
                "hop2_row_count": len(hop2_rows),
                "result": final_dict,
            }
