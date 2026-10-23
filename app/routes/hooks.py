"""Webhook receiver for the ArchiveSync OJS plugin.

The plugin POSTs {"events": [...]} signed with the source's secret. We only
verify and queue here; the worker does the real fetching.
"""

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import Journal, Source
from app.security import verify_webhook
from app.sync import record
from app.worker.main import sync_job_id

router = APIRouter()


@router.post("/hooks/ojs/{source_id}", status_code=202)
async def ojs_webhook(source_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    source = await session.get(Source, source_id)
    if not source or not source.enabled:
        raise HTTPException(404)

    body = await request.body()
    timestamp = request.headers.get("x-archivesync-timestamp", "")
    signature = request.headers.get("x-archivesync-signature", "")
    if not timestamp.isdigit() or abs(time.time() - int(timestamp)) > get_settings().webhook_max_skew:
        raise HTTPException(401, "stale or missing timestamp")
    if not verify_webhook(source.webhook_secret, timestamp, body, signature):
        raise HTTPException(401, "bad signature")

    try:
        events = json.loads(body)["events"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "expected {\"events\": [...]}")

    redis = request.app.state.redis
    delay = get_settings().webhook_sync_delay
    queued = 0
    for event in events[:500]:
        try:
            context_id, submission_id = int(event["contextId"]), int(event["submissionId"])
        except (KeyError, TypeError, ValueError):
            continue
        journal = await session.scalar(
            select(Journal).where(Journal.source_id == source.id, Journal.ojs_context_id == context_id)
        )
        if journal is None:
            # A journal we haven't seen yet: discover it and sync it.
            await redis.enqueue_job("reconcile_source_job", source.id, event.get("contextPath"),
                                    _job_id=f"reconcile:{source.id}:{context_id}", _defer_by=delay)
        else:
            await redis.enqueue_job("sync_article_job", source.id, journal.id, submission_id,
                                    _job_id=sync_job_id(source.id, journal.id, submission_id), _defer_by=delay)
        queued += 1

    await record(session, "webhook", f"{len(events)} event(s), {queued} queued", source_id=source.id,
                 data={"events": events[:50]})
    await session.commit()
    return {"queued": queued}
