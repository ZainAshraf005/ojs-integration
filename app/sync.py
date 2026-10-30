"""Core sync logic: OJS -> our database + file storage.

Every function here is idempotent: running it twice gives the same result, so
webhooks, backfills and the nightly reconcile can all safely overlap.
"""

import logging
from datetime import UTC, date, datetime
from itertools import groupby

from cryptography.fernet import InvalidToken
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import JOURNAL_NUMBER_START, Article, ArticleVersion, File, Journal, Source, SyncEvent, redirect_url
from app.ojs.client import STATUS_PUBLISHED, OJSClient, OJSError
from app.ojs.metadata import clean_publication, localized, metadata_hash, strip_html
from app.security import decrypt
from app.storage import files as storage

log = logging.getLogger(__name__)


def client_for(source: Source) -> OJSClient:
    try:
        token = decrypt(source.api_token_enc)
    except InvalidToken:
        raise OJSError(f"Cannot read the stored API key for '{source.name}': APP_SECRET_KEY changed since it was "
                       "saved. Re-enter the key on the install's page in the dashboard.") from None
    return OJSClient(source.base_url, token)


async def record(session: AsyncSession, kind: str, message: str, *, source_id: int | None = None,
                 level: str = "info", ref: str = "", data: dict | None = None) -> None:
    session.add(SyncEvent(kind=kind, message=message, source_id=source_id, level=level, ref=ref, data=data or {}))
    {"error": log.error, "warning": log.warning}.get(level, log.info)("%s %s %s", kind, ref, message)


def author_names(meta: dict) -> str:
    """'A, B, C' from the author records (OJS's authorsString adds roles like "(Author)")."""
    names = [a.get("fullName") or f"{localized(a.get('givenName'))} {localized(a.get('familyName'))}".strip()
             for a in meta.get("authors") or []]
    return ", ".join(n for n in names if n) or meta.get("authorsString", "")


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


# --- Journals -------------------------------------------------------------------


async def discover_journals(session: AsyncSession, source: Source, ojs: OJSClient) -> list[Journal]:
    """Create/refresh a Journal row for every journal in the install.

    New journals get the next free public number. Existing numbers never change.
    """
    existing = {j.ojs_context_id: j for j in (await session.scalars(select(Journal).where(Journal.source_id == source.id)))}
    next_number = (await session.scalar(select(func.max(Journal.number)))) or JOURNAL_NUMBER_START - 1

    journals = []
    async for ctx in ojs.contexts():
        journal = existing.get(ctx["id"])
        if journal is None:
            next_number += 1
            journal = Journal(source_id=source.id, ojs_context_id=ctx["id"], number=next_number)
            session.add(journal)
            await record(session, "journal", f"new journal {ctx['urlPath']} -> number {next_number}",
                         source_id=source.id, ref=ctx["urlPath"])
        elif journal.ojs_path != ctx["urlPath"]:
            # Same journal (OJS's internal id), new address: keep its number, refresh every article's link
            await record(session, "journal", f"path changed {journal.ojs_path} -> {ctx['urlPath']}; number "
                         f"{journal.number} kept, re-syncing its articles", source_id=source.id, ref=ctx["urlPath"])
            await resync_all(session, Article.journal_id == journal.id)
        journal.ojs_path = ctx["urlPath"]
        journal.names = ctx.get("name") or {}
        journal.name = localized(journal.names) or ctx["urlPath"]
        journal.acronym = localized(ctx.get("acronym")) or None
        journals.append(journal)
    await session.commit()
    return journals


# --- Articles -------------------------------------------------------------------


async def _store_galley(session: AsyncSession, ojs: OJSClient, journal: Journal, version: ArticleVersion,
                        submission_id: int, galley: dict) -> File | None:
    """Download one galley file if we don't already have it. Returns the File if newly stored."""
    existing = await session.scalar(
        select(File).where(File.version_id == version.id, File.ojs_galley_id == galley["id"])
    )
    if existing and existing.status in ("stored", "remote"):
        return None

    file_info = galley.get("file") or {}
    row = existing or File(version_id=version.id, ojs_galley_id=galley["id"])
    row.label = galley.get("label") or ""
    row.ojs_submission_file_id = galley.get("submissionFileId")
    row.original_name = localized(file_info.get("name"))
    row.mimetype = file_info.get("mimetype") or ""
    session.add(row)

    if galley.get("urlRemote"):
        row.status, row.source_url = "remote", galley["urlRemote"]
        return None

    row.source_url = ojs.galley_download_url(journal.ojs_path, submission_id, version.ojs_publication_id, galley["id"])
    try:
        dl = await ojs.download(row.source_url, storage.storage_root() / "tmp")
    except OJSError as exc:
        row.status, row.error = "failed", str(exc)
        await record(session, "file", f"download failed: {exc}", level="error",
                     source_id=journal.source_id, ref=f"{journal.ojs_path}/{submission_id}")
        return None

    row.mimetype = row.mimetype or dl.content_type
    row.storage_key = storage.store(dl.tmp_path, dl.sha256, storage.extension_for(row.mimetype, row.original_name))
    row.sha256, row.size, row.status, row.error = dl.sha256, dl.size, "stored", None
    return row


async def sync_article(session: AsyncSession, source: Source, journal: Journal, ojs: OJSClient,
                       submission_id: int) -> list[int]:
    """Bring one article fully up to date. Returns ids of newly stored files."""
    ref = f"{journal.ojs_path}/{submission_id}"
    article = await session.scalar(
        select(Article).where(Article.journal_id == journal.id, Article.ojs_submission_id == submission_id)
    )

    try:
        sub = await ojs.submission(journal.ojs_path, submission_id)
    except OJSError as exc:
        if "HTTP 404" in str(exc) and article and article.status == "published":
            article.status = "withdrawn"
            await record(session, "withdrawn", "deleted in OJS; archive copy kept", source_id=source.id, ref=ref)
            await session.commit()
            return []
        raise

    if sub.get("status") != STATUS_PUBLISHED:
        if article and article.status == "published":
            article.status = "withdrawn"
            await record(session, "withdrawn", "unpublished in OJS; archive copy kept", source_id=source.id, ref=ref)
            await session.commit()
        return []

    if article is None:
        clash = await session.scalar(
            select(Article).where(Article.journal_id == journal.id, Article.number == submission_id)
        )
        if clash:
            await record(session, "conflict", f"article number {submission_id} already used by OJS submission "
                         f"{clash.ojs_submission_id}; needs manual review", level="error", source_id=source.id, ref=ref)
            await session.commit()
            return []
        article = Article(journal_id=journal.id, number=submission_id, ojs_submission_id=submission_id)
        session.add(article)
        await session.flush()
        await record(session, "new", f"new article -> {journal.number}/{article.number}", source_id=source.id, ref=ref)

    identifier = redirect_url(journal.number, article.number)
    new_files: list[File] = []
    current: ArticleVersion | None = None
    for pub_summary in sub.get("publications", []):
        if pub_summary.get("status") != STATUS_PUBLISHED:
            continue
        pub = await ojs.publication(journal.ojs_path, submission_id, pub_summary["id"])
        if pub.get("archiveId") != identifier:  # set by the archiveSync plugin; missing until we push it
            article.ojs_id_pushed_at = None
        meta = clean_publication(pub)
        digest = metadata_hash(meta)

        version = await session.scalar(
            select(ArticleVersion).where(ArticleVersion.article_id == article.id,
                                         ArticleVersion.ojs_publication_id == pub["id"])
        )
        if version is None:
            version = ArticleVersion(article_id=article.id, ojs_publication_id=pub["id"])
            session.add(version)
        elif version.metadata_hash != digest:
            await record(session, "metadata", f"metadata changed for version {pub.get('version')}",
                         source_id=source.id, ref=ref)
        version.version = pub.get("version") or 1
        version.date_published = _parse_date(pub.get("datePublished"))
        version.origin_url = pub.get("urlPublished")
        version.metadata_, version.metadata_hash = meta, digest
        await session.flush()

        for galley in pub.get("galleys") or []:
            if stored := await _store_galley(session, ojs, journal, version, submission_id, galley):
                new_files.append(stored)

        if current is None or version.version >= current.version:
            current = version

    if current is not None:
        meta = current.metadata_
        article.title = strip_html(localized(meta.get("fullTitle") or meta.get("title")))  # OJS titles may hold <b>, <i>
        article.authors = author_names(meta)
        article.date_published = current.date_published
        article.doi = meta.get("doi")
    article.status = "published"
    article.origin_url = sub.get("urlPublished")
    article.ojs_last_modified = sub.get("lastModified")
    await session.commit()
    return [f.id for f in new_files]


async def resync_all(session: AsyncSession, where) -> None:
    """Make the next reconcile re-fetch these articles from OJS even though OJS says they're unchanged."""
    await session.execute(update(Article).where(where).values(ojs_last_modified=None))


async def move_source(session: AsyncSession, source: Source, new_base_url: str) -> None:
    """The install now lives at another address. IDs stay; stored links switch to the new address
    right away, and the next reconcile refreshes every article from OJS to confirm them."""
    old, new = f"{source.base_url}/", f"{new_base_url}/"
    journal_ids = select(Journal.id).where(Journal.source_id == source.id)
    article_ids = select(Article.id).where(Article.journal_id.in_(journal_ids))
    version_ids = select(ArticleVersion.id).where(ArticleVersion.article_id.in_(article_ids))
    for model, column, scope in ((Article, Article.origin_url, Article.journal_id.in_(journal_ids)),
                                 (ArticleVersion, ArticleVersion.origin_url, ArticleVersion.article_id.in_(article_ids)),
                                 (File, File.source_url, File.version_id.in_(version_ids))):
        await session.execute(
            update(model).where(scope, column.startswith(old, autoescape=True))
            .values({column: new + func.substr(column, len(old) + 1)})
        )
    await resync_all(session, Article.journal_id.in_(journal_ids))
    await record(session, "source", f"address changed {source.base_url} -> {new_base_url}", source_id=source.id)
    source.base_url = new_base_url
    source.checked_at = None  # health unknown at the new address until the next check


# --- Writing our IDs back to OJS ---------------------------------------------------

PUSH_BATCH = 50


async def push_identifiers(session: AsyncSession, source: Source, ojs: OJSClient) -> int:
    """Send every published article's ID that OJS doesn't have yet to the archiveSync plugin,
    which stores it on the article. Returns how many OJS confirmed. Raises OJSError if the
    plugin can't be reached; unconfirmed articles stay pending for the next run.
    """
    rows = (await session.execute(
        select(Journal, Article).join(Article.journal)
        .where(Journal.source_id == source.id, Journal.enabled.is_(True),
               Article.status == "published", Article.ojs_id_pushed_at.is_(None))
        .order_by(Journal.id, Article.id)
    )).all()

    stored_total = 0
    for journal, group in groupby(rows, key=lambda row: row[0]):
        articles = [article for _, article in group]
        for i in range(0, len(articles), PUSH_BATCH):
            batch = {a.ojs_submission_id: a for a in articles[i:i + PUSH_BATCH]}
            items = [{"submissionId": sid, "identifier": redirect_url(journal.number, a.number)} for sid, a in batch.items()]
            result = await ojs.push_identifiers(journal.ojs_path, items, source.webhook_secret)
            now = datetime.now(UTC)
            for sid in result.get("stored") or []:
                if article := batch.get(sid):
                    article.ojs_id_pushed_at = now
                    stored_total += 1
            for skip in result.get("skipped") or []:
                await record(session, "ojs-id", f"OJS did not store the ID: {skip.get('reason')}", level="warning",
                             source_id=source.id, ref=f"{journal.ojs_path}/{skip.get('submissionId')}")
            await session.commit()
    return stored_total


# --- Reconcile ------------------------------------------------------------------


async def find_changes(session: AsyncSession, journal: Journal, ojs: OJSClient) -> list[int]:
    """Submission ids in this journal that are new, changed, or no longer published."""
    live: dict[int, str] = {}
    async for sub in ojs.published_submissions(journal.ojs_path):
        live[sub["id"]] = sub.get("lastModified") or ""

    ours = {a.ojs_submission_id: a for a in await session.scalars(select(Article).where(Article.journal_id == journal.id))}
    changed = [sid for sid, modified in live.items() if sid not in ours or ours[sid].ojs_last_modified != modified]
    gone = [sid for sid, a in ours.items() if sid not in live and a.status == "published"]
    return changed + gone
