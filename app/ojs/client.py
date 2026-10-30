"""Async client for the OJS 3.5 REST API and public galley downloads."""

import asyncio
import hashlib
import json
import os
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings
from app.security import sign_webhook

STATUS_PUBLISHED = 3
PAGE_SIZE = 100


class OJSError(Exception):
    pass


@dataclass
class Download:
    tmp_path: Path
    sha256: str
    size: int
    content_type: str


class OJSClient:
    """One client per OJS install. Use as `async with OJSClient(...) as ojs:`."""

    def __init__(self, base_url: str, api_token: str, delay: float | None = None):
        settings = get_settings()
        self.base_url = base_url.rstrip("/")
        self.delay = settings.ojs_request_delay if delay is None else delay
        self._http = httpx.AsyncClient(
            timeout=settings.ojs_timeout,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
        )
        self._auth = {"Authorization": f"Bearer {api_token}"}
        self._max_file_bytes = settings.max_file_bytes

    async def __aenter__(self) -> "OJSClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.aclose()

    # --- REST API -------------------------------------------------------------

    def api_url(self, journal_path: str, endpoint: str) -> str:
        # "_" is the site-wide context in OJS 3.5
        return f"{self.base_url}/index.php/{journal_path}/api/v1/{endpoint.lstrip('/')}"

    async def get_json(self, journal_path: str, endpoint: str, params: dict | None = None) -> Any:
        url = self.api_url(journal_path, endpoint)
        for attempt in range(3):
            try:
                resp = await self._http.get(url, params=params, headers=self._auth)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise OJSError(f"GET {url}: {exc}") from exc
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if self.delay:
                await asyncio.sleep(self.delay)
            if resp.status_code in (429, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(5 * (attempt + 1))
                continue
            if resp.status_code != 200:
                raise OJSError(f"GET {url} -> HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise OJSError(f"GET {url}: gave up")

    async def _paged(self, journal_path: str, endpoint: str, params: dict) -> AsyncIterator[dict]:
        offset = 0
        while True:
            page = await self.get_json(journal_path, endpoint, {**params, "count": PAGE_SIZE, "offset": offset})
            items = page.get("items", [])
            for item in items:
                yield item
            offset += len(items)
            if not items or offset >= page.get("itemsMax", 0):
                return

    def contexts(self) -> AsyncIterator[dict]:
        """All journals in this install."""
        return self._paged("_", "contexts", {"isEnabled": "all"})

    def published_submissions(self, journal_path: str) -> AsyncIterator[dict]:
        return self._paged(
            journal_path,
            "submissions",
            {"status": STATUS_PUBLISHED, "orderBy": "lastModified", "orderDirection": "DESC"},
        )

    async def submission(self, journal_path: str, submission_id: int) -> dict:
        return await self.get_json(journal_path, f"submissions/{submission_id}")

    async def publication(self, journal_path: str, submission_id: int, publication_id: int) -> dict:
        return await self.get_json(journal_path, f"submissions/{submission_id}/publications/{publication_id}")

    # --- archiveSync plugin ---------------------------------------------------

    def plugin_url(self, journal_path: str, op: str) -> str:
        return f"{self.base_url}/index.php/{journal_path}/archivesync/{op}"

    async def push_identifiers(self, journal_path: str, items: list[dict], secret: str) -> dict:
        """Ask the archiveSync plugin to store our IDs on these articles.

        Signed like the plugin's webhooks, in the other direction. Returns
        {"stored": [submissionId, ...], "skipped": [{"submissionId": .., "reason": ..}]}.
        """
        url = self.plugin_url(journal_path, "identifiers")
        body = json.dumps({"items": items}, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        headers = {"Content-Type": "application/json", "X-ArchiveSync-Timestamp": timestamp,
                   "X-ArchiveSync-Signature": sign_webhook(secret, timestamp, body)}
        try:
            resp = await self._http.post(url, content=body, headers=headers)
        except httpx.TransportError as exc:
            raise OJSError(f"POST {url}: {exc}") from exc
        if self.delay:
            await asyncio.sleep(self.delay)
        if resp.status_code == 404:
            raise OJSError(f"POST {url} -> HTTP 404: archiveSync plugin 1.1+ not installed on this OJS")
        try:
            result = resp.json()
        except ValueError:
            result = None
        if resp.status_code != 200 or not isinstance(result, dict):
            raise OJSError(f"POST {url} -> HTTP {resp.status_code}: {resp.text[:300]}")
        return result

    # --- Public pages & files -------------------------------------------------

    def galley_download_url(self, journal_path: str, submission_id: int, publication_id: int, galley_id: int) -> str:
        return (
            f"{self.base_url}/index.php/{journal_path}/article/download/"
            f"{submission_id}/version/{publication_id}/{galley_id}"
        )

    async def download(self, url: str, dest_dir: Path) -> Download:
        """Stream a file to a temp file inside dest_dir, hashing as we go."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=dest_dir, prefix=".dl-")
        tmp_path = Path(tmp_name)
        digest, size = hashlib.sha256(), 0
        try:
            async with self._http.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise OJSError(f"download {url} -> HTTP {resp.status_code}")
                with os.fdopen(fd, "wb") as fh:
                    async for chunk in resp.aiter_bytes(1024 * 256):
                        size += len(chunk)
                        if size > self._max_file_bytes:
                            raise OJSError(f"download {url}: larger than {self._max_file_bytes} bytes")
                        digest.update(chunk)
                        fh.write(chunk)
                content_type = resp.headers.get("content-type", "").split(";")[0].strip()
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        if self.delay:
            await asyncio.sleep(self.delay)
        if size == 0:
            tmp_path.unlink(missing_ok=True)
            raise OJSError(f"download {url}: empty file")
        return Download(tmp_path, digest.hexdigest(), size, content_type)

    async def fetch_page(self, url: str) -> httpx.Response:
        return await self._http.get(url, headers={"Accept": "text/html"})
