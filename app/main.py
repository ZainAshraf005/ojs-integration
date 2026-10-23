import logging
from contextlib import asynccontextmanager
from urllib.parse import quote

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.routes import admin, hooks, public

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    yield
    await app.state.redis.aclose()


settings = get_settings()
app = FastAPI(title=settings.site_name, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=settings.app_secret_key, max_age=12 * 3600,
                   https_only=settings.public_base_url.startswith("https"))
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.exception_handler(admin.LoginRequired)
async def to_login(request: Request, exc: admin.LoginRequired):
    return RedirectResponse(f"/admin/login?next={quote(request.url.path)}", status_code=303)


app.include_router(hooks.router)
app.include_router(admin.router)
app.include_router(public.router)  # last: it owns the numeric /{journal}/{article} paths
