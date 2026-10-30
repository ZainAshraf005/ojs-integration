from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.models import pid_url
from app.ojs.metadata import keyword_list, localized, paragraphs, strip_html

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals.update(site_name=get_settings().site_name, base_url=get_settings().public_base_url)
templates.env.filters.update(localized=localized, keywords=keyword_list, paragraphs=paragraphs, plain=strip_html)


def filesize(size: int | None) -> str:
    if not size:
        return ""
    return f"{size / 1048576:.1f} MB" if size >= 1048576 else f"{size // 1024} KB"


STATIC_DIR = Path(__file__).parent / "static"


def static_url(name: str) -> str:
    """/static/<name>?v=<modified time>: browsers fetch the new file as soon as it changes, not a stale cached one."""
    return f"/static/{name}?v={int((STATIC_DIR / name).stat().st_mtime)}"


templates.env.globals["pid_url"] = pid_url
templates.env.globals["static_url"] = static_url
templates.env.filters["filesize"] = filesize


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
