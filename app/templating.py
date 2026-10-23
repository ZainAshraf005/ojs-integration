from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.ojs.metadata import keyword_list, localized, paragraphs, strip_html

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals.update(site_name=get_settings().site_name, base_url=get_settings().public_base_url)
templates.env.filters.update(localized=localized, keywords=keyword_list, paragraphs=paragraphs, plain=strip_html)


def pid_url(journal_number: int, article_number: int | None = None) -> str:
    """The public persistent identifier, e.g. https://sch.org/1003/188"""
    base = f"{get_settings().public_base_url}/{journal_number}"
    return base if article_number is None else f"{base}/{article_number}"


def filesize(size: int | None) -> str:
    if not size:
        return ""
    return f"{size / 1048576:.1f} MB" if size >= 1048576 else f"{size // 1024} KB"


templates.env.globals["pid_url"] = pid_url
templates.env.filters["filesize"] = filesize


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
