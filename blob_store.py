"""Minimal Vercel Blob client (standard library only).

Mirrors the requests made by the official SDK, without depending on the
`vercel` Python package (which is not importable inside Vercel's own Python
runtime). Supports public and private stores.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import quote, urlencode, urlparse


class BlobStoreError(RuntimeError):
    pass


def _api_url(path: str = "") -> str:
    base = os.environ.get("VERCEL_BLOB_API_URL") or "https://vercel.com/api/blob"
    return f"{base.rstrip('/')}{path}"


def _api_version() -> str:
    return os.environ.get("VERCEL_BLOB_API_VERSION_OVERRIDE") or "11"


class BlobStore:
    def __init__(self, token: str, timeout: float = 120.0, retries: int = 3) -> None:
        if not token:
            raise BlobStoreError("Missing Vercel Blob read-write token.")
        self.token = token
        parts = token.split("_")
        self.store_id = parts[3] if len(parts) > 3 else ""
        self.timeout = timeout
        self.retries = max(1, retries)

    # -- helpers -------------------------------------------------------------
    def _api_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "authorization": f"Bearer {self.token}",
            "x-api-version": _api_version(),
            "x-api-blob-request-id": f"{self.store_id}:{int(time.time() * 1000)}:{uuid.uuid4().hex[:8]}",
            "x-api-blob-request-attempt": "0",
        }
        headers.update(extra or {})
        return headers

    def _send(self, req: request.Request) -> Any:
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            try:
                return request.urlopen(req, timeout=self.timeout)
            except error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                if exc.code < 500 and exc.code != 429:
                    raise BlobStoreError(f"Vercel Blob HTTP {exc.code}: {detail}") from exc
                last_exc = BlobStoreError(f"Vercel Blob HTTP {exc.code}: {detail}")
            except (error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
            time.sleep(1.5 * (attempt + 1))
        raise BlobStoreError(f"Vercel Blob request failed: {last_exc}")

    def url_for(self, pathname: str, access: str) -> str:
        if not self.store_id:
            raise BlobStoreError("Could not read the store id from the Blob token.")
        return f"https://{self.store_id}.{access}.blob.vercel-storage.com/{quote(pathname.lstrip('/'))}"

    # -- operations ----------------------------------------------------------
    def put(
        self,
        pathname: str,
        data: bytes,
        *,
        access: str = "private",
        content_type: str = "application/octet-stream",
        overwrite: bool = True,
    ) -> dict[str, Any]:
        headers = self._api_headers(
            {
                "x-vercel-blob-access": access,
                "x-content-type": content_type,
                "x-add-random-suffix": "0",
                "x-allow-overwrite": "1" if overwrite else "0",
                "content-type": content_type,
            }
        )
        req = request.Request(
            f"{_api_url()}/?{urlencode({'pathname': pathname})}",
            data=data,
            headers=headers,
            method="PUT",
        )
        with self._send(req) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    def delete(self, urls: list[str]) -> None:
        if not urls:
            return
        req = request.Request(
            _api_url("/delete"),
            data=json.dumps({"urls": urls}).encode("utf-8"),
            headers=self._api_headers({"content-type": "application/json"}),
            method="POST",
        )
        with self._send(req) as resp:
            resp.read()

    def _get_request(self, url: str, access: str) -> request.Request:
        host = urlparse(url).netloc
        if not host.endswith(".blob.vercel-storage.com"):
            raise BlobStoreError("Refusing to fetch a non-Blob URL.")
        headers = {"authorization": f"Bearer {self.token}"} if access == "private" else {}
        return request.Request(url, headers=headers, method="GET")

    def download_to(self, url: str, destination: str | Path, *, access: str = "private") -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._send(self._get_request(url, access)) as resp, destination.open("wb") as fh:
            shutil.copyfileobj(resp, fh)
        return destination

    def read(self, pathname: str, *, access: str = "private") -> bytes:
        with self._send(self._get_request(self.url_for(pathname, access), access)) as resp:
            return resp.read()
