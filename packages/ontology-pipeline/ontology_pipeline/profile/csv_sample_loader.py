"""CsvSampleLoader — feeds foundry's bundle_from_pandas from local CSV files.

Drop-in replacement for the psycopg-backed sampler when the pipeline runs
in local-preview mode. Returns the same `pandas.DataFrame` shape that
`bundle_from_pandas` expects.

File layout convention:
    <data_dir>/<table>.csv

The schema name is ignored (the introspector only emits tables from one
schema at a time, but CSVs flatten the schema). For multi-schema dumps,
prefix CSV filenames with the schema (e.g., `public.users_core.csv`) and
configure `schema_prefix=True` on the loader.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd

logger = logging.getLogger(__name__)


class CsvSampleLoader:
    """Reads a sample of rows from `<data_dir>/<table>.csv`.

    Args:
        data_dir: directory containing one CSV per table. File name matches
            the table name (without schema prefix), with `.csv` extension.
        schema_prefix: when True, CSV file names are `<schema>.<table>.csv`
            (lets the same data dir back multiple schemas). Default False.
        delimiter: CSV delimiter. Defaults to ',' but configurable for
            datasets that ship with `;` or `\\t`.

    Returns from `__call__`:
        a pandas DataFrame with up to `limit` rows. Empty DataFrame when
        the CSV is missing or empty — the profiler treats this as "no
        bundle for this asset" and skips it gracefully.
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        schema_prefix: bool = False,
        delimiter: str = ",",
    ) -> None:
        self._data_dir = Path(data_dir)
        self._schema_prefix = schema_prefix
        self._delimiter = delimiter

    def __call__(
        self, source_id: str, schema: str, table: str, limit: int,
    ) -> "pd.DataFrame":
        """SampleLoader signature: `(source_id, schema, table, limit) -> DataFrame`."""
        import pandas as pd

        if self._schema_prefix:
            path = self._data_dir / f"{schema}.{table}.csv"
        else:
            path = self._data_dir / f"{table}.csv"

        if not path.exists():
            logger.info(
                "CsvSampleLoader: no CSV for %s.%s at %s; skipping profile",
                schema, table, path,
            )
            return pd.DataFrame()

        try:
            df = pd.read_csv(
                path,
                nrows=limit,
                delimiter=self._delimiter,
                low_memory=False,
            )
        except Exception as exc:  # noqa: BLE001 — defensive on bad CSVs
            logger.warning(
                "CsvSampleLoader: failed to read %s: %s; skipping profile",
                path, exc,
            )
            return pd.DataFrame()
        return df


def build_csv_sample_loader(data_dir: Path) -> Callable[..., "pd.DataFrame"]:
    """Convenience factory mirroring `_psycopg_sample_loader` shape.

    Returns the loader as a plain callable so it can be passed as
    `sample_loader=...` directly to `TableProfiler`.
    """
    loader = CsvSampleLoader(data_dir=data_dir)
    return loader  # __call__ matches the SampleLoader protocol
