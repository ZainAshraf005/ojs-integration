"""Background worker (arq). Run with:  arq app.worker.main.WorkerSettings

All slow or retryable work happens here, never inside a web request.
"""

import logging
from datetime import UTC, datetime

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import select

from app import health, sync
from app.config import get_settings
from app.db import SessionLocal
from app.models import Article, ArticleVersion, File, Journal, Source
from app.ojs.client import OJSError
from app.storage import files as storage

log = logging.getLogger(__name__)


def sync_job_id(source_id: int, journal_id: int, submission_id: int) -> str:
    return f"sync:{source_id}:{journal_id}:{submission_id}"


async def _enqueue_sync(ctx, source_id: int, journal_id: int, submission_id: int, defer: int = 0) -> None:
    await ctx["redis"].enqueue_job("sync_article_job", source_id, journal_id, submission_id,
                                   _job_id=sync_job_id(source_id, journal_id, submission_id), _defer_by=defer)


# --- Jobs -------------------------------------------------------------------------


async def sync_article_job(ctx, source_id: int, journal_id: int, submission_id: int) -> int:
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        journal = await session.get(Journal, journal_id)
        if not (source and journal and source.enabled and journal.enabled):
            return 0
        try:
            async with sync.client_for(source) as ojs:
                new_file_ids = await sync.sync_article(session, source, journal, ojs, submission_id)
        except Exception as exc:
            await session.rollback()
            await sync.record(session, "error", f"sync failed: {exc}", level="error",
                              source_id=source_id, ref=f"{journal.ojs_path}/{submission_id}")
            await session.commit()
            raise
    if get_settings().s3_enabled:
        for file_id in new_file_ids:
            await ctx["redis"].enqueue_job("replicate_file_job", file_id, _job_id=f"replicate:{file_id}")
    return len(new_file_ids)


async def reconcile_source_job(ctx, source_id: int, journal_path: str | None = None) -> int:
    """Discover journals, then queue a sync for every new/changed/removed article.

    The first run of this on a source IS the backfill.
    """
    queued = 0
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        if not source or not source.enabled:
            return 0
        try:
            ojs = sync.client_for(source)
        except OJSError as exc:
            await sync.record(session, "error", str(exc), level="error", source_id=source_id)
            await session.commit()
            return 0
        async with ojs:
            journals = await sync.discover_journals(session, source, ojs)
            for journal in journals:
                if not journal.enabled or (journal_path and journal.ojs_path != journal_path):
                    continue
                for submission_id in await sync.find_changes(session, journal, ojs):
                    await _enqueue_sync(ctx, source.id, journal.id, submission_id)
                    queued += 1

        # Retry files that failed to download last time
        failed = await session.execute(
            select(Journal.id, Article.ojs_submission_id).distinct()
            .select_from(File).join(ArticleVersion).join(Article).join(Journal)
            .where(File.status == "failed", Journal.source_id == source.id)
        )
        for journal_id, submission_id in failed:
            await _enqueue_sync(ctx, source.id, journal_id, submission_id)
            queued += 1

        source.last_reconciled_at = datetime.now(UTC)
        await sync.record(session, "reconcile", f"queued {queued} article syncs", source_id=source.id)
        await session.commit()
    return queued


async def replicate_file_job(ctx, file_id: int) -> None:
    async with SessionLocal() as session:
        row = await session.get(File, file_id)
        if not row or row.status != "stored" or row.replicated_at:
            return
        await storage.replicate(row.storage_key, row.mimetype)
        row.replicated_at = datetime.now(UTC)
        await session.commit()


# --- Scheduled (cron) -------------------------------------------------------------


async def _source_ids() -> list[int]:
    async with SessionLocal() as session:
        return list(await session.scalars(select(Source.id).where(Source.enabled.is_(True))))


async def reconcile_all(ctx) -> None:
    for source_id in await _source_ids():
        await ctx["redis"].enqueue_job("reconcile_source_job", source_id, _job_id=f"reconcile:{source_id}")


async def health_check_all(ctx) -> None:
    for source_id in await _source_ids():
        await ctx["redis"].enqueue_job("health_source_job", source_id, _job_id=f"health:{source_id}")


async def health_source_job(ctx, source_id: int) -> None:
    async with SessionLocal() as session:
        source = await session.get(Source, source_id)
        if not source:
            return
        if await health.check_source(session, source):
            checked, failing = await health.check_articles(session, source)
            if failing:
                log.warning("%s: %d of %d checked articles failing", source.name, failing, checked)
        else:
            log.warning("%s is DOWN: %s", source.name, source.last_error)


async def verify_files(ctx) -> None:
    """Weekly: re-hash every stored file and flag anything missing or corrupted."""
    async with SessionLocal() as session:
        for row in await session.scalars(select(File).where(File.status == "stored")):
            if await storage.verify(row.storage_key, row.sha256):
                row.verified_at = datetime.now(UTC)
            else:
                row.status, row.error = "failed", "checksum mismatch or missing on disk"
                await sync.record(session, "integrity", f"file {row.id} failed verification", level="error",
                                  data={"file_id": row.id, "storage_key": row.storage_key})
        await session.commit()


async def replicate_pending(ctx) -> None:
    if not get_settings().s3_enabled:
        return
    async with SessionLocal() as session:
        for file_id in await session.scalars(
            select(File.id).where(File.status == "stored", File.replicated_at.is_(None))
        ):
            await ctx["redis"].enqueue_job("replicate_file_job", file_id, _job_id=f"replicate:{file_id}")


class WorkerSettings:
    functions = [sync_article_job, reconcile_source_job, replicate_file_job, health_source_job]
    cron_jobs = [
        cron(reconcile_all, hour={2}, minute={0}),
        cron(health_check_all, minute=set(range(0, 60, 10))),
        cron(verify_files, weekday={6}, hour={3}, minute={30}),
        cron(replicate_pending, minute={15}),
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 4  # be gentle with Hostinger
    job_timeout = 600
    max_tries = 3
    keep_result = 0  # so the same article can be re-queued right after it finishes
    health_check_interval = 60  # the dashboard shows "worker stopped" if this goes stale
