"""Public pages: the resolver, archive landing pages, search and file downloads.

    /{journal}/{article}           -> always our page (archive copy)
    /{journal}/{article}?redirect  -> the article on OJS if it answers right now, else our page
                                      (this is the link the archiveSync plugin shows in OJS)
    /{journal}/{article}/v{n}      -> a specific version (also takes ?redirect)
    /{journal}/{article}/file/{id}-> the stored file (PDF)
    /{journal}                    -> journal page with its articles
"""

import re
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.health import origin_reachable
from app.models import REDIRECT_PARAM, Article, ArticleVersion, File, Journal, Source
from app.storage import files as storage
from app.templating import render

router = APIRouter()

PAGE_SIZE = 50


def not_found(request: Request, what: str = "This identifier") -> HTMLResponse:
    return render(request, "not_found.html", status_code=404, what=what)


@router.get("/healthz")
async def healthz(session: AsyncSession = Depends(get_session)):
    await session.execute(select(1))
    return {"ok": True}


@router.get("/robots.txt")
async def robots():
    return Response("User-agent: *\nDisallow: /admin\nDisallow: /hooks\n", media_type="text/plain")


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, session: AsyncSession = Depends(get_session)):
    journals = (await session.execute(
        select(Journal, func.count(Article.id))
        .join(Article, Article.journal_id == Journal.id)
        .group_by(Journal.id)
        .order_by(Journal.number)
    )).all()
    stats = {
        "journals": len(journals),
        "articles": await session.scalar(select(func.count(Article.id))),
        "files": await session.scalar(select(func.count(File.id)).where(File.status == "stored")),
    }
    return render(request, "home.html", journals=journals, stats=stats)


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", session: AsyncSession = Depends(get_session)):
    q = q.strip()
    if m := re.fullmatch(r"(?:https?://[^/]+/)?(\d+)/(\d+)", q):  # someone pasted an identifier
        return RedirectResponse(f"/{m[1]}/{m[2]}", status_code=302)
    results = []
    if len(q) >= 2:
        pattern = f"%{q}%"
        results = list(await session.scalars(
            select(Article).options(selectinload(Article.journal))
            .where(or_(Article.title.ilike(pattern), Article.authors.ilike(pattern), Article.doi == q.lower().removeprefix("https://doi.org/")))
            .order_by(Article.date_published.desc().nulls_last())
            .limit(100)
        ))
    return render(request, "search.html", q=q, results=results)


@router.get("/{journal_number:int}", response_class=HTMLResponse)
async def journal_page(request: Request, journal_number: int, page: int = 1, session: AsyncSession = Depends(get_session)):
    journal = await session.scalar(select(Journal).where(Journal.number == journal_number))
    if not journal:
        return not_found(request, "This journal")
    page = max(page, 1)
    total = await session.scalar(select(func.count(Article.id)).where(Article.journal_id == journal.id))
    articles = list(await session.scalars(
        select(Article).where(Article.journal_id == journal.id)
        .order_by(Article.date_published.desc().nulls_last(), Article.number.desc())
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    ))
    return render(request, "journal.html", journal=journal, articles=articles, page=page,
                  pages=max(1, -(-total // PAGE_SIZE)), total=total)


async def _load(session: AsyncSession, journal_number: int, article_number: int) -> Article | None:
    return await session.scalar(
        select(Article)
        .join(Journal)
        .options(
            selectinload(Article.journal).selectinload(Journal.source),
            selectinload(Article.versions).selectinload(ArticleVersion.files),
        )
        .where(Journal.number == journal_number, Article.number == article_number)
    )


def _origin_healthy(article: Article, source: Source) -> bool:
    return bool(article.origin_url) and article.status == "published" and source.is_up and article.origin_ok


async def _resolve(request: Request, session: AsyncSession, j: int, a: int, version_no: int | None):
    article = await _load(session, j, a)
    if not article or not article.versions:
        return not_found(request)

    if version_no is None:
        version = article.versions[-1]
    else:
        version = next((v for v in article.versions if v.version == version_no), None)
        if not version:
            return not_found(request, "This version")

    reason = "withdrawn" if article.status == "withdrawn" else "direct"
    healthy = _origin_healthy(article, article.journal.source)  # last background check, for the "open on journal" link
    if REDIRECT_PARAM in request.query_params and reason == "direct":
        target = (version.origin_url if version_no is not None else None) or article.origin_url
        if target and await origin_reachable(target, article.title):
            return RedirectResponse(target, status_code=302)
        reason, healthy = "unavailable", False
    return render(request, "article.html", article=article, journal=article.journal, version=version,
                  meta=version.metadata_, files=[f for f in version.files if f.status == "stored"],
                  reason=reason, healthy=healthy)


@router.get("/{journal_number:int}/{article_number:int}", response_class=HTMLResponse)
async def resolve(request: Request, journal_number: int, article_number: int, session: AsyncSession = Depends(get_session)):
    return await _resolve(request, session, journal_number, article_number, None)


@router.get("/{journal_number:int}/{article_number:int}/v{version_no:int}", response_class=HTMLResponse)
async def resolve_version(request: Request, journal_number: int, article_number: int, version_no: int,
                          session: AsyncSession = Depends(get_session)):
    return await _resolve(request, session, journal_number, article_number, version_no)


@router.get("/{journal_number:int}/{article_number:int}/file/{file_id:int}")
async def download(request: Request, journal_number: int, article_number: int, file_id: int,
                   session: AsyncSession = Depends(get_session)):
    row = await session.scalar(
        select(File).join(ArticleVersion).join(Article).join(Journal)
        .where(File.id == file_id, File.status == "stored", Article.number == article_number, Journal.number == journal_number)
    )
    if not row or not storage.path_for(row.storage_key).exists():
        return not_found(request, "This file")
    filename = f"{journal_number}-{article_number}{Path(row.storage_key).suffix}"
    return FileResponse(storage.path_for(row.storage_key), media_type=row.mimetype or None,
                        filename=filename, content_disposition_type="inline",
                        headers={"Cache-Control": "public, max-age=86400", "X-Content-SHA256": row.sha256})
