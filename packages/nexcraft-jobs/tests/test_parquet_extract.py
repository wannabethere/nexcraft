from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from nexcraft_jobs.runtime.parquet_extract import write_named_dataset_to_parquet
from nexcraft_jobs.runtime.staging_paths import extract_parquet_path, extract_parquet_uri


def test_write_table_roundtrip(tmp_path: Path) -> None:
    tbl = pa.table({"a": [1, 2, 3], "b": [1.0, 2.0, 3.0]})
    dest = tmp_path / "out.parquet"
    rows, nbytes = write_named_dataset_to_parquet(dest, tbl)
    assert rows == 3
    assert nbytes > 0
    back = pq.read_table(dest)
    assert back.equals(tbl)


def test_staging_path_uri_roundtrip(tmp_path: Path) -> None:
    p = extract_parquet_path(str(tmp_path), "t1", "job1", "sales")
    assert p.parent.name == "extract"
    uri = extract_parquet_uri(p)
    assert uri.startswith("file:")
