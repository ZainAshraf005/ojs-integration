"""Webhook + resolver behaviour against a real Postgres (archive_test database)."""

import json
import time

import httpx
import pytest

from app.db import SessionLocal, engine
from app.main import app
from app.models import Article, ArticleVersion, Base, File, Journal, Source
from app.security import encrypt, sign_webhook


class FakeRedis:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.jobs.append((name, args, kwargs))


@pytest.fixture
async def client():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    app.state.redis = FakeRedis()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c
    await engine.dispose()


async def seed(*, source_up=True, origin_ok=True, status="published"):
    async with SessionLocal() as s:
        source = Source(name="Main", base_url="https://ojs.example/journal", api_token_enc=encrypt("t"),
                        webhook_secret="hook-secret", is_up=source_up)
        s.add(source)
        await s.flush()
        journal = Journal(number=1001, source_id=source.id, ojs_context_id=3, ojs_path="jest", name="JEST")
        s.add(journal)
        await s.flush()
        article = Article(journal_id=journal.id, number=188, ojs_submission_id=188, title="Urbanization",
                          status=status, origin_url="https://ojs.example/journal/index.php/jest/article/view/188",
                          origin_ok=origin_ok)
        s.add(article)
        await s.flush()
        version = ArticleVersion(article_id=article.id, ojs_publication_id=188, version=1,
                                 metadata_={"title": {"en": "Urbanization"}, "authors": [{"fullName": "Maryam"}],
                                            "abstract": {"en": "<p>Abstract <script>alert(1)</script></p>"}})
        s.add(version)
        await s.flush()
        s.add(File(version_id=version.id, ojs_galley_id=149, label="pdf", mimetype="application/pdf",
                   status="stored", sha256="ab" * 32, storage_key="ab/ab/x.pdf", size=1000))
        await s.commit()
        return source.id


def signed(body: dict, secret="hook-secret", ts=None):
    raw = json.dumps(body).encode()
    ts = str(ts or int(time.time()))
    return raw, {"X-ArchiveSync-Timestamp": ts, "X-ArchiveSync-Signature": sign_webhook(secret, ts, raw),
                 "Content-Type": "application/json"}


async def test_resolver_redirects_when_origin_healthy(client):
    await seed()
    r = await client.get("/1001/188")
    assert r.status_code == 302
    assert r.headers["location"].endswith("/jest/article/view/188")


async def test_resolver_shows_archive_when_origin_down(client):
    await seed(source_up=False)
    r = await client.get("/1001/188")
    assert r.status_code == 200
    assert "currently unavailable" in r.text
    assert "Urbanization" in r.text
    assert "<script>alert(1)</script>" not in r.text  # OJS HTML is never rendered raw
    assert 'name="citation_pdf_url"' in r.text


async def test_resolver_withdrawn(client):
    await seed(status="withdrawn")
    for url in ("/1001/188", "/1001/188?archive"):
        r = await client.get(url)
        assert r.status_code == 200 and "no longer published" in r.text


async def test_resolver_archive_param_skips_redirect(client):
    await seed()
    r = await client.get("/1001/188?archive")
    assert r.status_code == 200 and "viewing the preserved copy" in r.text


async def test_unknown_ids_and_non_numeric_paths(client):
    await seed()
    assert (await client.get("/1001/999")).status_code == 404
    assert (await client.get("/9999")).status_code == 404
    assert (await client.get("/1001")).status_code == 200
    assert (await client.get("/search?q=urban")).status_code == 200
    r = await client.get("/search?q=1001/188")
    assert r.status_code == 302 and r.headers["location"] == "/1001/188?archive"


async def test_webhook_queues_sync(client):
    source_id = await seed()
    raw, headers = signed({"events": [{"event": "publish", "contextId": 3, "contextPath": "jest", "submissionId": 188}]})
    r = await client.post(f"/hooks/ojs/{source_id}", content=raw, headers=headers)
    assert r.status_code == 202 and r.json() == {"queued": 1}
    name, args, kwargs = app.state.redis.jobs[0]
    assert name == "sync_article_job" and args[2] == 188 and kwargs["_defer_by"] > 0


async def test_webhook_unknown_journal_triggers_discovery(client):
    source_id = await seed()
    raw, headers = signed({"events": [{"event": "publish", "contextId": 99, "contextPath": "newj", "submissionId": 1}]})
    r = await client.post(f"/hooks/ojs/{source_id}", content=raw, headers=headers)
    assert r.status_code == 202
    assert app.state.redis.jobs[0][0] == "reconcile_source_job"


async def test_webhook_rejects_bad_signature_and_replays(client):
    source_id = await seed()
    raw, headers = signed({"events": []}, secret="wrong")
    assert (await client.post(f"/hooks/ojs/{source_id}", content=raw, headers=headers)).status_code == 401
    raw, headers = signed({"events": []}, ts=int(time.time()) - 3600)
    assert (await client.post(f"/hooks/ojs/{source_id}", content=raw, headers=headers)).status_code == 401
    assert app.state.redis.jobs == []
