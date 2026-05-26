"""Execute a TemporalJobSpec via temporalio Client."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from nexcraft_jobs.yaml_jobs.spec import TemporalJobSpec

logger = logging.getLogger(__name__)


async def run_job_spec(spec: TemporalJobSpec, *, temporal_target_override: str | None = None) -> Any:
    """Start workflow from spec; optionally await result."""

    from temporalio.client import Client

    target = temporal_target_override or spec.temporal_target
    client = await Client.connect(target)

    if spec.workflow_id:
        wid = spec.workflow_id
    elif spec.workflow_id_prefix:
        wid = f"{spec.workflow_id_prefix}-{uuid.uuid4()}"
    else:
        wid = f"{spec.workflow_type}-{uuid.uuid4()}"

    label = spec.name or spec.workflow_type
    logger.info("Starting workflow %s (%s) id=%s queue=%s", label, spec.workflow_type, wid, spec.task_queue)

    handle = await client.start_workflow(
        spec.workflow_type,
        spec.input,
        id=wid,
        task_queue=spec.task_queue,
    )

    if not spec.wait_for_result:
        return {"workflow_id": wid, "started": True}

    result = await handle.result()
    logger.info("Workflow %s completed", wid)
    return result
