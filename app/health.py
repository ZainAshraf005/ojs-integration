"""Is the original OJS page actually working?

The resolver never checks OJS live (a slow Hostinger would make every visitor
wait). These checks run in the background and store the result.
"""

import re
from datetime import UTC, datetime, timedelta
from html import unescape

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Article, Journal, Source


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", unescape(text).lower()).strip()


def page_shows_article(html: str, title: str) -> bool:
    """A 200 response is not enough: suspended hosting or OJS error pages also return 200."""
    if not title:
        return "pkp_structure_page" in html or "obj_article_details" in html
    needle = _normalize(re.sub(r"<[^>]+>", " ", title))[:60]
    return bool(needle) and needle in _normalize(re.sub(r"<[^>]+>", " ", html))


def _client() -> httpx.AsyncClient:
    s = get_settings()
    # Hostinger's CDN answers 403 to requests without an Accept header.
    return httpx.AsyncClient(timeout=20, follow_redirects=True, headers={
        "User-Agent": s.user_agent, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})


async def check_source(session: AsyncSession, source: Source) -> bool:
    async with _client() as http:
        try:
            resp = await http.get(f"{source.base_url}/index.php/index")
            up = resp.status_code == 200 and "pkp_structure" in resp.text
            error = None if up else f"HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            up, error = False, f"{type(exc).__name__}: {exc}"
    source.is_up, source.last_error, source.checked_at = up, error, datetime.now(UTC)
    await session.commit()
    return up


async def check_articles(session: AsyncSession, source: Source) -> tuple[int, int]:
    """Re-check the articles whose status is oldest. Returns (checked, failing)."""
    s = get_settings()
    stale_before = datetime.now(UTC) - timedelta(hours=s.health_article_max_age_hours)
    articles = list(await session.scalars(
        select(Article)
        .join(Journal)
        .where(Journal.source_id == source.id, Article.status == "published", Article.origin_url.is_not(None))
        .where(or_(Article.origin_checked_at.is_(None), Article.origin_checked_at < stale_before))
        .order_by(Article.origin_checked_at.asc().nulls_first())
        .limit(s.health_article_batch)
    ))
    failing = 0
    async with _client() as http:
        for article in articles:
            try:
                resp = await http.get(article.origin_url)
                ok = resp.status_code == 200 and page_shows_article(resp.text, article.title)
            except httpx.HTTPError:
                ok = False
            article.origin_ok, article.origin_checked_at = ok, datetime.now(UTC)
            failing += not ok
    await session.commit()
    return len(articles), failing
