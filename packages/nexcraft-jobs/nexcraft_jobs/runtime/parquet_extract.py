from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def _record_batches(table_or_reader: pa.Table | pa.RecordBatchReader) -> Iterator[pa.RecordBatch]:
    if isinstance(table_or_reader, pa.Table):
        yield from table_or_reader.to_batches(max_chunksize=65536)
        return
    yield from table_or_reader


def write_named_dataset_to_parquet(
    dest: Path,
    table_or_reader: pa.Table | pa.RecordBatchReader,
    *,
    on_batch: Callable[[pa.RecordBatch, int, int], None] | None = None,
) -> tuple[int, int]:
    """Stream batches to Parquet (zstd). Hook receives (batch, cumulative_rows, cumulative_bytes)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    rows = 0
    nbytes = 0
    writer: pq.ParquetWriter | None = None
    try:
        for batch in _record_batches(table_or_reader):
            if writer is None:
                writer = pq.ParquetWriter(
                    str(tmp),
                    batch.schema,
                    compression="zstd",
                    compression_level=3,
                )
            writer.write_batch(batch)
            rows += batch.num_rows
            nbytes += int(batch.nbytes)
            if on_batch is not None:
                on_batch(batch, rows, nbytes)

        if writer is None:
            schema = table_or_reader.schema
            pq.write_table(
                pa.Table.from_batches([], schema=schema),
                str(tmp),
                compression="zstd",
                compression_level=3,
            )
        else:
            writer.close()
            writer = None

        tmp.replace(dest)
        return rows, nbytes
    finally:
        if writer is not None:
            writer.close()
        if tmp.exists() and not dest.exists():
            tmp.unlink(missing_ok=True)
