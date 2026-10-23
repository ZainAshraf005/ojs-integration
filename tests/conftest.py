import os

# Tests use their own database next to the dev one (same Postgres container).
os.environ["DATABASE_URL"] = "postgresql+asyncpg://archive:archive@localhost:5432/archive_test"
os.environ.setdefault("APP_SECRET_KEY", "test-secret")
os.environ["PUBLIC_BASE_URL"] = "http://testserver"

import asyncpg
import pytest

from app.config import get_settings

get_settings.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def _create_test_database():
    import asyncio

    async def create():
        try:
            conn = await asyncpg.connect("postgresql://archive:archive@localhost:5432/archive")
        except OSError:
            pytest.skip("Postgres not running (docker compose up -d db)", allow_module_level=True)
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = 'archive_test'")
        if not exists:
            await conn.execute("CREATE DATABASE archive_test")
        await conn.close()

    asyncio.run(create())
