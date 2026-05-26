from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse


def extract_parquet_path(staging_root: str, tenant_id: str, job_id: str, dataset_name: str) -> Path:
    safe_name = dataset_name.replace("/", "_").replace("..", "")
    return Path(staging_root) / tenant_id / job_id / "extract" / f"{safe_name}.parquet"


def extract_parquet_uri(path: Path) -> str:
    return path.resolve().as_uri()


def compute_parquet_path(
    staging_root: str, tenant_id: str, job_id: str, dataset_name: str
) -> Path:
    safe_name = dataset_name.replace("/", "_").replace("..", "")
    return Path(staging_root) / tenant_id / job_id / "compute" / f"{safe_name}.parquet"


def compute_parquet_uri(path: Path) -> str:
    return path.resolve().as_uri()


def local_path_from_uri(uri: str) -> str:
    """Strip a ``file://`` prefix; pass non-file URIs through unchanged.

    PyArrow's ParquetFile takes a path or filesystem object; for v0.1 staging
    is local, so we resolve ``file://`` URIs back to plain paths.
    """
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        return parsed.path
    return uri
