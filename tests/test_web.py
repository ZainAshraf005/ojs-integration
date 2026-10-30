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


@pytest.fixture
def ojs_reachable(monkeypatch):
    """Controls the live check behind ?redirect links; records the URLs it was asked about."""
    state = {"up": True, "checked": []}

    async def fake(url, title):
        state["checked"].append(url)
        return state["up"]

    monkeypatch.setattr("app.routes.public.origin_reachable", fake)
    return state


async def test_plain_link_always_shows_our_page(client, ojs_reachable):
    await seed()
    r = await client.get("/1001/188")
    assert r.status_code == 200 and "viewing the preserved copy" in r.text
    assert ojs_reachable["checked"] == []  # no live check for plain links


async def test_redirect_link_goes_to_ojs_when_reachable(client, ojs_reachable):
    await seed()
    r = await client.get("/1001/188?redirect")
    assert r.status_code == 302
    assert r.headers["location"].endswith("/jest/article/view/188")
    assert ojs_reachable["checked"] == ["https://ojs.example/journal/index.php/jest/article/view/188"]


async def test_redirect_link_shows_our_page_when_ojs_down(client, ojs_reachable):
    await seed()  # the background check still says healthy: the live check decides
    ojs_reachable["up"] = False
    r = await client.get("/1001/188?redirect")
    assert r.status_code == 200
    assert "currently unavailable" in r.text
    assert "Urbanization" in r.text
    assert "<script>alert(1)</script>" not in r.text  # OJS HTML is never rendered raw
    assert 'name="citation_pdf_url"' in r.text


async def test_resolver_withdrawn(client, ojs_reachable):
    await seed(status="withdrawn")
    for url in ("/1001/188", "/1001/188?redirect"):
        r = await client.get(url)
        assert r.status_code == 200 and "no longer published" in r.text
    assert ojs_reachable["checked"] == []


async def test_live_check_is_cached(monkeypatch):
    from app import health

    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text="<h1>Urbanization and growth</h1>")

    monkeypatch.setattr(health, "_reachable_cache", {})
    monkeypatch.setattr(health, "_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await health.origin_reachable("https://ojs.example/a/188", "Urbanization and growth")
    assert await health.origin_reachable("https://ojs.example/a/188", "Urbanization and growth")
    assert len(calls) == 1


async def test_unknown_ids_and_non_numeric_paths(client):
    await seed()
    assert (await client.get("/1001/999")).status_code == 404
    assert (await client.get("/9999")).status_code == 404
    assert (await client.get("/1001")).status_code == 200
    assert (await client.get("/search?q=urban")).status_code == 200
    r = await client.get("/search?q=1001/188")
    assert r.status_code == 302 and r.headers["location"] == "/1001/188"


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


class FakeOJS:
    """Stands in for OJSClient: one published article (188) whose publication may already carry our ID."""

    def __init__(self, archive_id=None, stored=None):
        self.archive_id, self.stored, self.pushed = archive_id, stored, []

    async def submission(self, path, sid):
        return {"id": sid, "status": 3, "publications": [{"id": 188, "status": 3}], "lastModified": "2026-10-29"}

    async def publication(self, path, sid, pub_id):
        return {"id": pub_id, "version": 1, "title": {"en": "Urbanization"}, "archiveId": self.archive_id}

    async def push_identifiers(self, path, items, secret):
        self.pushed.append((path, items, secret))
        return {"stored": [i["submissionId"] for i in items] if self.stored is None else self.stored,
                "skipped": []}


async def test_ids_are_pushed_to_ojs_once(client):
    from app import sync

    await seed()
    async with SessionLocal() as s:
        source = await s.get(Source, 1)
        ojs = FakeOJS()
        assert await sync.push_identifiers(s, source, ojs) == 1
        assert ojs.pushed == [("jest", [{"submissionId": 188, "identifier": "http://testserver/1001/188?redirect"}], "hook-secret")]
        assert (await s.get(Article, 1)).ojs_id_pushed_at is not None
        assert await sync.push_identifiers(s, source, ojs) == 0  # nothing pending any more
        assert len(ojs.pushed) == 1


async def test_sync_requeues_id_when_ojs_lost_it(client):
    from datetime import UTC, datetime

    from app import sync

    await seed()
    async with SessionLocal() as s:
        article = await s.get(Article, 1)
        article.ojs_id_pushed_at = datetime.now(UTC)
        await s.commit()
        source, journal = await s.get(Source, 1), await s.get(Journal, 1)

        await sync.sync_article(s, source, journal, FakeOJS(archive_id="http://testserver/1001/188?redirect"), 188)
        assert (await s.get(Article, 1)).ojs_id_pushed_at is not None  # OJS has it: leave alone

        await sync.sync_article(s, source, journal, FakeOJS(archive_id=None), 188)
        assert (await s.get(Article, 1)).ojs_id_pushed_at is None  # OJS lost it: push again


async def test_journal_path_change_keeps_number_and_resyncs(client):
    from app import sync

    class Contexts:
        async def contexts(self):
            yield {"id": 3, "urlPath": "jest-new", "name": {"en": "JEST"}}

    await seed()
    async with SessionLocal() as s:
        article = await s.get(Article, 1)
        article.ojs_last_modified = "2026-10-29"
        await s.commit()
        [journal] = await sync.discover_journals(s, await s.get(Source, 1), Contexts())
        assert (journal.number, journal.ojs_path) == (1001, "jest-new")
        await s.refresh(article)
        assert article.ojs_last_modified is None  # next reconcile re-fetches it, with its new URL


async def test_move_source_rewrites_links_and_keeps_ids(client):
    from app import sync

    await seed()
    async with SessionLocal() as s:
        source = await s.get(Source, 1)
        await sync.move_source(s, source, "https://new.example/ojs")
        await s.commit()
    async with SessionLocal() as s:
        article = await s.get(Article, 1)
        assert (await s.get(Source, 1)).base_url == "https://new.example/ojs"
        assert article.number == 188 and (await s.get(Journal, 1)).number == 1001
        assert article.origin_url == "https://new.example/ojs/index.php/jest/article/view/188"
        assert article.ojs_last_modified is None
