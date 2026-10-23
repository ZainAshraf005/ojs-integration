"""Turn raw OJS publication JSON into the metadata we store and show publicly.

Only whitelisted fields are kept. OJS also returns author emails and ORCID
access/refresh tokens; those must never be stored or displayed.
"""

import hashlib
import json
import re
from html import unescape
from typing import Any

PUBLICATION_FIELDS = [
    "title", "subtitle", "fullTitle", "prefix", "abstract", "keywords", "subjects",
    "disciplines", "coverage", "type", "source", "rights", "licenseUrl",
    "copyrightHolder", "copyrightYear", "pages", "datePublished", "version",
    "locale", "citationsRaw", "dataAvailability", "supportingAgencies",
    "issueId", "sectionId", "urlPublished", "accessStatus",
]
AUTHOR_FIELDS = ["givenName", "familyName", "fullName", "preferredPublicName", "country", "orcid", "seq", "userGroupName"]


def _clean_author(author: dict) -> dict:
    out = {k: author.get(k) for k in AUTHOR_FIELDS if author.get(k) not in (None, "", {})}
    affiliations = [a.get("name") for a in author.get("affiliations") or [] if a.get("name")]
    if affiliations:
        out["affiliations"] = affiliations
    if author.get("ror"):
        out["ror"] = author["ror"]
    return out


def clean_publication(pub: dict) -> dict:
    meta: dict[str, Any] = {k: pub.get(k) for k in PUBLICATION_FIELDS if pub.get(k) not in (None, "", {}, [])}
    meta["authors"] = [_clean_author(a) for a in pub.get("authors") or []]
    meta["authorsString"] = pub.get("authorsString", "")
    doi = (pub.get("doiObject") or {}).get("doi")
    if doi:
        meta["doi"] = doi
    meta["galleys"] = [
        {
            "id": g.get("id"),
            "label": g.get("label"),
            "locale": g.get("locale"),
            "urlRemote": g.get("urlRemote"),
            "mimetype": (g.get("file") or {}).get("mimetype"),
            "name": (g.get("file") or {}).get("name"),
        }
        for g in pub.get("galleys") or []
    ]
    return meta


def metadata_hash(meta: dict) -> str:
    return hashlib.sha256(json.dumps(meta, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def localized(value: Any, prefer: str | None = None) -> str:
    """Pick one string out of an OJS {"en": "...", "fr": "..."} value."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict) or not value:
        return ""
    for key in (prefer, "en", "en_US"):
        if key and value.get(key):
            return value[key]
    return next((v for v in value.values() if v), "")


def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def paragraphs(html: str) -> list[str]:
    """OJS abstracts are HTML. Show them as plain-text paragraphs (no raw HTML = no XSS)."""
    parts = re.split(r"</p\s*>|<br\s*/?>|\n\s*\n", html or "", flags=re.I)
    return [p for p in (strip_html(part) for part in parts) if p]


def keyword_list(value: Any, prefer: str | None = None) -> list[str]:
    """OJS 3.5 keywords: {"en": [{"name": "a, b"}]}. Older: {"en": ["a", "b"]}."""
    if not isinstance(value, dict) or not value:
        return []
    items = value.get(prefer) or value.get("en") or next(iter(value.values()), [])
    words: list[str] = []
    for item in items or []:
        name = item.get("name", "") if isinstance(item, dict) else str(item)
        words.extend(w.strip() for w in name.split(",") if w.strip())
    return words
