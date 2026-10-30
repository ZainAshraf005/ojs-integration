import io
import re
import tarfile

import pytest

from app.db import SessionLocal
from app.main import app
from app.models import User
from app.security import hash_password
from tests.test_web import client, seed  # noqa: F401  (fixture reuse)


class AdminRedis:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.jobs.append((name, args, kwargs))

    async def exists(self, key):
        return 1

    async def zcard(self, key):
        return 0


@pytest.fixture
async def admin(client):  # noqa: F811
    app.state.redis = AdminRedis()
    async with SessionLocal() as s:
        s.add(User(email="staff@example.com", password_hash=hash_password("a-long-password")))
        await s.commit()
    return client


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html)[1]


async def login(c, password="a-long-password"):
    token = csrf_of((await c.get("/admin/login")).text)
    return await c.post("/admin/login", data={"csrf": token, "email": "Staff@Example.com", "password": password})


async def test_admin_requires_login(admin):
    r = await admin.get("/admin/journals/1")
    assert r.status_code == 303 and r.headers["location"] == "/admin/login?next=/admin/journals/1"


async def test_wrong_password_rejected(admin):
    assert (await login(admin, "nope-nope-nope")).status_code == 401


async def test_login_without_csrf_rejected(admin):
    r = await admin.post("/admin/login", data={"email": "staff@example.com", "password": "a-long-password"})
    assert r.status_code == 400


async def test_hierarchy_pages_show_ids_and_pdfs(admin):
    await seed()
    r = await login(admin)
    assert r.status_code == 303 and r.headers["location"] == "/admin"

    home = await admin.get("/admin")
    assert home.status_code == 200 and "Main" in home.text
    assert "data-theme-toggle" in home.text and 'localStorage.getItem("theme")' in home.text

    source = await admin.get("/admin/sources/1")
    assert source.status_code == 200
    assert 'data-copy="http://testserver/1001"' in source.text
    assert "webhook_secret" in source.text  # plugin setup block

    journal = await admin.get("/admin/journals/1")
    assert journal.status_code == 200
    assert 'data-copy="http://testserver/1001/188"' in journal.text
    assert 'href="/admin/files/1"' in journal.text

    article = await admin.get("/admin/articles/1")
    assert article.status_code == 200 and "Urbanization" in article.text

    assert (await admin.get("/admin/log")).status_code == 200


async def test_sync_buttons_queue_jobs(admin):
    await seed()
    await login(admin)
    token = csrf_of((await admin.get("/admin/journals/1")).text)
    r = await admin.post("/admin/journals/1/sync", data={"csrf": token})
    assert r.status_code == 303
    assert app.state.redis.jobs[-1][0] == "reconcile_source_job"
    r = await admin.post("/admin/articles/1/sync", data={"csrf": token})
    assert app.state.redis.jobs[-1][0] == "sync_article_job"


async def test_login_never_redirects_off_site(admin):
    token = csrf_of((await admin.get("/admin/login")).text)
    r = await admin.post("/admin/login?next=https://evil.example", data={
        "csrf": token, "email": "staff@example.com", "password": "a-long-password"})
    assert r.headers["location"] == "/admin"


async def test_plugin_download_is_an_uploadable_package(admin):
    assert (await admin.get("/admin/plugin/archiveSync.tar.gz")).status_code == 303  # login required
    await login(admin)
    r = await admin.get("/admin/plugin/archiveSync.tar.gz")
    assert r.status_code == 200 and "archiveSync.tar.gz" in r.headers["content-disposition"]
    names = tarfile.open(fileobj=io.BytesIO(r.content)).getnames()
    assert "archiveSync/version.xml" in names and "archiveSync/ArchiveSyncPlugin.php" in names
    assert "archiveSync/ArchiveSyncHandler.php" in names


class FakeOJSClient:
    """Stands in for OJSClient in the edit form: an install whose journals have these OJS ids."""

    context_ids = [3]

    def __init__(self, base_url, token, delay=None):
        self.base_url, self.token = base_url, token

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def contexts(self):
        for cid in self.context_ids:
            yield {"id": cid, "urlPath": f"j{cid}"}


async def test_edit_install_address_keeps_ids(admin, monkeypatch):
    monkeypatch.setattr("app.routes.admin.OJSClient", FakeOJSClient)
    await seed()
    await login(admin)
    token = csrf_of((await admin.get("/admin/sources/1/edit")).text)
    r = await admin.post("/admin/sources/1/edit", data={
        "csrf": token, "name": "Main", "base_url": "https://new.example/ojs/index.php", "api_key": ""})
    assert r.status_code == 303 and r.headers["location"] == "/admin/sources/1"
    assert [job[0] for job in app.state.redis.jobs] == ["reconcile_source_job", "health_source_job"]
    page = await admin.get("/admin/articles/1")
    assert 'href="https://new.example/ojs/index.php/jest/article/view/188"' in page.text  # "Original in OJS"
    assert 'data-copy="http://testserver/1001/188"' in page.text  # same ID


async def test_edit_refuses_a_different_install(admin, monkeypatch):
    monkeypatch.setattr("app.routes.admin.OJSClient", FakeOJSClient)
    monkeypatch.setattr(FakeOJSClient, "context_ids", [7, 8])
    await seed()
    await login(admin)
    token = csrf_of((await admin.get("/admin/sources/1/edit")).text)
    r = await admin.post("/admin/sources/1/edit", data={
        "csrf": token, "name": "Main", "base_url": "https://other.example", "api_key": ""})
    assert r.status_code == 400 and "different OJS install" in r.text
    assert app.state.redis.jobs == []
