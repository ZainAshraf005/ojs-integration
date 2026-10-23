"""Staff dashboard at /admin.

    /admin                      all OJS installs
    /admin/sources/new          add an install (tests the API key, then starts importing)
    /admin/sources/{id}         its journals
    /admin/journals/{id}        its articles, each with its ID (copy) and PDF
    /admin/articles/{id}        one article: files, versions, sync history
    /admin/log                  everything that happened

OJS stays the source of truth: article content isn't edited here, it's synced.
"""

import asyncio
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import get_session
from app.models import Article, ArticleVersion, File, Journal, Source, SyncEvent, User
from app.ojs.client import OJSClient, OJSError
from app.security import encrypt, verify_password
from app.storage import files as storage
from app.sync import record
from app.templating import render
from app.worker.main import sync_job_id

router = APIRouter(prefix="/admin")


class LoginRequired(Exception):
    pass


# --- Auth, CSRF, flash messages -------------------------------------------------


async def current_user(request: Request, session: AsyncSession = Depends(get_session)) -> User:
    user_id = request.session.get("user_id")
    user = await session.get(User, user_id) if user_id else None
    if not user or not user.is_active:
        raise LoginRequired()
    return user


def csrf_token(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(32)
    return request.session["csrf"]


async def check_csrf(request: Request) -> None:
    form = await request.form()
    expected = request.session.get("csrf", "")
    if not expected or not secrets.compare_digest(str(form.get("csrf", "")), expected):
        raise HTTPException(400, "Form expired. Go back, refresh the page and try again.")


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, message])


async def page(request: Request, name: str, user: User, status_code: int = 200, **ctx) -> HTMLResponse:
    redis = request.app.state.redis
    worker_alive = bool(await redis.exists("arq:queue:health-check"))
    queued = await redis.zcard("arq:queue")
    messages = request.session.pop("flash", [])
    return render(request, name, status_code=status_code, user=user, csrf=csrf_token(request),
                  worker_alive=worker_alive, queued=queued, messages=messages, **ctx)


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return render(request, "admin/login.html", csrf=csrf_token(request), error=None)


@router.post("/login", dependencies=[Depends(check_csrf)])
async def login(request: Request, email: str = Form(...), password: str = Form(...),
                session: AsyncSession = Depends(get_session)):
    user = await session.scalar(select(User).where(User.email == email.strip().lower(), User.is_active.is_(True)))
    if not user or not verify_password(password, user.password_hash):
        await asyncio.sleep(1)  # slow down password guessing
        return render(request, "admin/login.html", status_code=401, csrf=csrf_token(request),
                      error="Wrong email or password.")
    request.session.clear()
    request.session["user_id"] = user.id
    target = request.query_params.get("next", "")
    return back(target if target.startswith("/admin") else "/admin")  # never redirect off-site


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return back("/admin/login")


# --- Dashboard: all installs ----------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(
            Source,
            func.count(func.distinct(Journal.id)),
            func.count(func.distinct(Article.id)),
            func.count(func.distinct(case((Article.id.is_not(None), Journal.id)))),
        )
        .outerjoin(Journal, Journal.source_id == Source.id)
        .outerjoin(Article, Article.journal_id == Journal.id)
        .group_by(Source.id)
        .order_by(Source.id)
    )).all()
    totals = {
        "journals": sum(r[1] for r in rows),
        "articles": sum(r[2] for r in rows),
        "files": await session.scalar(select(func.count(File.id)).where(File.status == "stored")),
        "bytes": await session.scalar(select(func.coalesce(func.sum(File.size), 0)).where(File.status == "stored")),
    }
    problems = list(await session.scalars(
        select(SyncEvent).where(SyncEvent.level == "error").order_by(SyncEvent.created_at.desc()).limit(5)
    ))
    return await page(request, "admin/dashboard.html", user, rows=rows, totals=totals, problems=problems)


@router.get("/sources/new", response_class=HTMLResponse)
async def new_source_form(request: Request, user: User = Depends(current_user)):
    return await page(request, "admin/source_new.html", user, error=None, form={})


@router.post("/sources/new", dependencies=[Depends(check_csrf)])
async def new_source(request: Request, name: str = Form(...), base_url: str = Form(...), api_key: str = Form(...),
                     user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    base_url = base_url.strip().rstrip("/").removesuffix("/index.php")
    form = {"name": name, "base_url": base_url}

    async def fail(error: str):
        return await page(request, "admin/source_new.html", user, status_code=400, error=error, form=form)

    if not base_url.startswith(("http://", "https://")):
        return await fail("The URL must start with https://")
    if await session.scalar(select(Source).where((Source.base_url == base_url) | (Source.name == name.strip()))):
        return await fail("An install with this name or URL is already added.")
    try:
        async with OJSClient(base_url, api_key.strip(), delay=0) as ojs:
            journal_count = 0
            async for _ in ojs.contexts():
                journal_count += 1
    except OJSError as exc:
        hint = " Check the API key." if "401" in str(exc) or "403" in str(exc) else ""
        return await fail(f"Could not connect to OJS: {exc}.{hint}")

    source = Source(name=name.strip(), base_url=base_url, api_token_enc=encrypt(api_key.strip()),
                    webhook_secret=secrets.token_hex(32))
    session.add(source)
    await session.flush()
    await record(session, "source", f"added by {user.email}; {journal_count} journals found", source_id=source.id)
    await session.commit()
    await request.app.state.redis.enqueue_job("reconcile_source_job", source.id, _job_id=f"reconcile:{source.id}")
    flash(request, f"Connected: {journal_count} journals found. Importing has started in the background.")
    return back(f"/admin/sources/{source.id}")


# --- One install: its journals --------------------------------------------------


@router.get("/sources/{source_id}", response_class=HTMLResponse)
async def source_page(request: Request, source_id: int, user: User = Depends(current_user),
                      session: AsyncSession = Depends(get_session)):
    source = await session.get(Source, source_id)
    if not source:
        raise HTTPException(404)
    files_per_journal = (
        select(Article.journal_id, func.count(File.id).label("n"))
        .join(ArticleVersion, ArticleVersion.article_id == Article.id)
        .join(File, and_(File.version_id == ArticleVersion.id, File.status == "stored"))
        .group_by(Article.journal_id).subquery()
    )
    article_count = func.count(Article.id)
    journals = (await session.execute(
        select(Journal, article_count, func.coalesce(func.max(files_per_journal.c.n), 0))
        .outerjoin(Article, Article.journal_id == Journal.id)
        .outerjoin(files_per_journal, files_per_journal.c.journal_id == Journal.id)
        .where(Journal.source_id == source.id)
        .group_by(Journal.id)
        .order_by((article_count > 0).desc(), Journal.number)
    )).all()
    return await page(request, "admin/source.html", user, source=source, journals=journals,
                      webhook_url=f"{get_settings().public_base_url}/hooks/ojs/{source.id}")


@router.post("/sources/{source_id}/sync", dependencies=[Depends(check_csrf)])
async def source_sync(request: Request, source_id: int, user: User = Depends(current_user)):
    await request.app.state.redis.enqueue_job("reconcile_source_job", source_id, _job_id=f"reconcile:{source_id}")
    flash(request, "Sync queued: new journals and articles will appear as they are imported.")
    return back(f"/admin/sources/{source_id}")


@router.post("/sources/{source_id}/key", dependencies=[Depends(check_csrf)])
async def source_key(request: Request, source_id: int, api_key: str = Form(...), user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    source = await session.get(Source, source_id)
    if not source:
        raise HTTPException(404)
    try:
        async with OJSClient(source.base_url, api_key.strip(), delay=0) as ojs:
            async for _ in ojs.contexts():
                break
    except OJSError as exc:
        flash(request, f"Key not saved, OJS rejected it: {exc}", "error")
        return back(f"/admin/sources/{source_id}")
    source.api_token_enc = encrypt(api_key.strip())
    await record(session, "source", f"API key replaced by {user.email}", source_id=source.id)
    await session.commit()
    flash(request, "API key updated and working.")
    return back(f"/admin/sources/{source_id}")


@router.post("/sources/{source_id}/health", dependencies=[Depends(check_csrf)])
async def source_health(request: Request, source_id: int, user: User = Depends(current_user)):
    await request.app.state.redis.enqueue_job("health_source_job", source_id, _job_id=f"health:{source_id}")
    flash(request, "Health check queued.")
    return back(f"/admin/sources/{source_id}")


# --- One journal: its articles --------------------------------------------------


@router.get("/journals/{journal_id}", response_class=HTMLResponse)
async def journal_page(request: Request, journal_id: int, user: User = Depends(current_user),
                       session: AsyncSession = Depends(get_session)):
    journal = await session.scalar(
        select(Journal).options(selectinload(Journal.source)).where(Journal.id == journal_id)
    )
    if not journal:
        raise HTTPException(404)
    articles = list(await session.scalars(
        select(Article)
        .options(selectinload(Article.versions).selectinload(ArticleVersion.files))
        .where(Article.journal_id == journal.id)
        .order_by(Article.date_published.desc().nulls_last(), Article.number.desc())
    ))
    return await page(request, "admin/journal.html", user, journal=journal, source=journal.source, articles=articles)


@router.post("/journals/{journal_id}/sync", dependencies=[Depends(check_csrf)])
async def journal_sync(request: Request, journal_id: int, user: User = Depends(current_user),
                       session: AsyncSession = Depends(get_session)):
    journal = await session.get(Journal, journal_id)
    if not journal:
        raise HTTPException(404)
    await request.app.state.redis.enqueue_job("reconcile_source_job", journal.source_id, journal.ojs_path,
                                              _job_id=f"reconcile:{journal.source_id}:{journal.ojs_path}")
    flash(request, f"Sync queued for {journal.name}.")
    return back(f"/admin/journals/{journal_id}")


@router.post("/journals/{journal_id}/toggle", dependencies=[Depends(check_csrf)])
async def journal_toggle(request: Request, journal_id: int, user: User = Depends(current_user),
                         session: AsyncSession = Depends(get_session)):
    journal = await session.get(Journal, journal_id)
    if not journal:
        raise HTTPException(404)
    journal.enabled = not journal.enabled
    await record(session, "journal", f"archiving {'enabled' if journal.enabled else 'paused'} by {user.email}",
                 source_id=journal.source_id, ref=journal.ojs_path)
    await session.commit()
    flash(request, f"Archiving {'resumed' if journal.enabled else 'paused'} for {journal.name}.")
    return back(f"/admin/journals/{journal_id}")


# --- One article ----------------------------------------------------------------


@router.get("/articles/{article_id}", response_class=HTMLResponse)
async def article_page(request: Request, article_id: int, user: User = Depends(current_user),
                       session: AsyncSession = Depends(get_session)):
    article = await session.scalar(
        select(Article)
        .options(selectinload(Article.journal).selectinload(Journal.source),
                 selectinload(Article.versions).selectinload(ArticleVersion.files))
        .where(Article.id == article_id)
    )
    if not article:
        raise HTTPException(404)
    journal = article.journal
    events = list(await session.scalars(
        select(SyncEvent)
        .where(SyncEvent.source_id == journal.source_id, SyncEvent.ref == f"{journal.ojs_path}/{article.ojs_submission_id}")
        .order_by(SyncEvent.created_at.desc()).limit(30)
    ))
    version = article.versions[-1] if article.versions else None
    return await page(request, "admin/article.html", user, article=article, journal=journal, source=journal.source,
                      version=version, meta=version.metadata_ if version else {}, events=events)


@router.post("/articles/{article_id}/sync", dependencies=[Depends(check_csrf)])
async def article_sync(request: Request, article_id: int, user: User = Depends(current_user),
                       session: AsyncSession = Depends(get_session)):
    article = await session.scalar(select(Article).options(selectinload(Article.journal)).where(Article.id == article_id))
    if not article:
        raise HTTPException(404)
    j = article.journal
    await request.app.state.redis.enqueue_job("sync_article_job", j.source_id, j.id, article.ojs_submission_id,
                                              _job_id=sync_job_id(j.source_id, j.id, article.ojs_submission_id))
    flash(request, "Re-sync queued.")
    return back(f"/admin/articles/{article_id}")


@router.get("/files/{file_id}")
async def open_file(file_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    row = await session.get(File, file_id)
    if not row or not row.storage_key or not storage.path_for(row.storage_key).exists():
        raise HTTPException(404, "File not stored")
    return FileResponse(storage.path_for(row.storage_key), media_type=row.mimetype or None,
                        filename=row.original_name or Path(row.storage_key).name, content_disposition_type="inline")


# --- Sync log -------------------------------------------------------------------


@router.get("/log", response_class=HTMLResponse)
async def log_page(request: Request, level: str = "", p: int = 1, user: User = Depends(current_user),
                   session: AsyncSession = Depends(get_session)):
    query = select(SyncEvent).order_by(SyncEvent.created_at.desc())
    if level in ("error", "warning"):
        query = query.where(SyncEvent.level == level)
    p = max(p, 1)
    events = list(await session.scalars(query.offset((p - 1) * 100).limit(101)))
    sources = {s.id: s.name for s in await session.scalars(select(Source))}
    return await page(request, "admin/log.html", user, events=events[:100], has_more=len(events) > 100,
                      p=p, level=level, sources=sources)
