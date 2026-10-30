import os

os.environ.setdefault("APP_SECRET_KEY", "test-secret")

import httpx
import pytest

from app.health import page_shows_article
from app.ojs.client import OJSClient, OJSError
from app.ojs.metadata import clean_publication, keyword_list, localized, metadata_hash, paragraphs
from app.security import encrypt, decrypt, hash_password, sign_webhook, verify_password, verify_webhook

RAW_PUBLICATION = {
    "id": 188,
    "title": {"en": "URBANIZATION AND ENVIRONMENTAL CHALLENGES"},
    "fullTitle": {"en": "URBANIZATION AND ENVIRONMENTAL CHALLENGES"},
    "abstract": {"en": "<p>First <em>para</em>.</p><p>Second &amp; last.</p>"},
    "keywords": {"en": [{"name": "Urbanization, air pollution , climate change"}]},
    "authorsString": "Maryam ()",
    "authors": [{
        "fullName": "Maryam",
        "givenName": {"en": "Maryam"},
        "familyName": {"en": ""},
        "email": "secret@example.com",
        "orcidAccessToken": "SECRET-TOKEN",
        "orcidRefreshToken": "SECRET-REFRESH",
        "country": "PK",
        "affiliations": [{"name": {"en": "University of Okara"}, "authorId": 161}],
    }],
    "galleys": [{"id": 149, "label": "pdf", "file": {"mimetype": "application/pdf", "name": {"en": "x.pdf"}}}],
    "doiObject": None,
    "datePublished": "2025-12-30",
}


def test_private_author_fields_are_dropped():
    meta = clean_publication(RAW_PUBLICATION)
    dumped = str(meta)
    assert "secret@example.com" not in dumped
    assert "SECRET-TOKEN" not in dumped and "SECRET-REFRESH" not in dumped
    assert meta["authors"][0]["affiliations"] == [{"en": "University of Okara"}]
    assert meta["galleys"][0]["mimetype"] == "application/pdf"


def test_metadata_hash_is_stable():
    assert metadata_hash(clean_publication(RAW_PUBLICATION)) == metadata_hash(clean_publication(dict(RAW_PUBLICATION)))


def test_localized_and_keywords_and_paragraphs():
    assert localized({"fr": "Bonjour", "en": "Hello"}) == "Hello"
    assert localized({"fr": "Bonjour"}) == "Bonjour"
    assert localized(None) == ""
    assert keyword_list(RAW_PUBLICATION["keywords"]) == ["Urbanization", "air pollution", "climate change"]
    assert keyword_list({"en": ["a", "b"]}) == ["a", "b"]
    assert paragraphs(RAW_PUBLICATION["abstract"]["en"]) == ["First para .", "Second & last."]


def test_webhook_signature_roundtrip():
    sig = sign_webhook("s3cret", "1700000000", b'{"events":[]}')
    assert verify_webhook("s3cret", "1700000000", b'{"events":[]}', sig)
    assert not verify_webhook("s3cret", "1700000001", b'{"events":[]}', sig)
    assert not verify_webhook("other", "1700000000", b'{"events":[]}', sig)


def test_php_and_python_signatures_match():
    # PHP: hash_hmac('sha256', '1700000000.' . '{"events":[]}', 's3cret')
    assert sign_webhook("s3cret", "1700000000", b'{"events":[]}') == (
        __import__("hmac").new(b"s3cret", b'1700000000.{"events":[]}', "sha256").hexdigest()
    )


def test_passwords_and_encryption():
    stored = hash_password("correct horse battery")
    assert verify_password("correct horse battery", stored)
    assert not verify_password("wrong", stored)
    assert decrypt(encrypt("token-123")) == "token-123"


def test_health_needs_article_title_not_just_200():
    page = "<html><title>\n  Urbanization and Environmental Challenges | JEST</title><body class='pkp_structure_page'>"
    assert page_shows_article(page, "URBANIZATION AND ENVIRONMENTAL CHALLENGES")
    assert not page_shows_article("<h1>Account suspended</h1>", "URBANIZATION AND ENVIRONMENTAL CHALLENGES")
    # OJS 3.5 titles can contain inline HTML
    assert page_shows_article("<h1>The <b>Effectiveness</b> of CLT</h1>", "<b>THE EFFECTIVENESS OF CLT</b>")


async def test_push_identifiers_reports_plugin_refusal_sent_with_http_200():
    async with OJSClient("https://ojs.example/journal", "t", delay=0) as ojs:
        ojs._http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"error": "bad signature"})))
        with pytest.raises(OJSError, match="bad signature"):
            await ojs.push_identifiers("jest", [{"submissionId": 1, "identifier": "https://x/1001/1?redirect"}], "s")
