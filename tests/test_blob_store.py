import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from blob_store import BlobStore, BlobStoreError

TOKEN = "vercel_blob_rw_StoreAbc123_secretpart"


@pytest.fixture()
def fake_api(monkeypatch):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, payload):
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_PUT(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append(("PUT", self.path, dict(self.headers), body))
            self._reply({"url": "https://storeabc123.private.blob.vercel-storage.com/x.csv", "pathname": "x.csv"})

        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append(("POST", self.path, dict(self.headers), body))
            self._reply({})

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("VERCEL_BLOB_API_URL", f"http://127.0.0.1:{server.server_port}/api/blob")
    yield seen
    server.shutdown()


def test_put_sends_private_access_and_token(fake_api):
    store = BlobStore(TOKEN)
    result = store.put("shopify-listing/outputs/job/a.csv", b"abc", access="private", content_type="text/csv")

    method, path, headers, body = fake_api[0]
    lower = {k.lower(): v for k, v in headers.items()}
    assert method == "PUT"
    assert path == "/api/blob/?pathname=shopify-listing%2Foutputs%2Fjob%2Fa.csv"
    assert lower["authorization"] == f"Bearer {TOKEN}"
    assert lower["x-vercel-blob-access"] == "private"
    assert lower["x-allow-overwrite"] == "1"
    assert lower["x-content-type"] == "text/csv"
    assert body == b"abc"
    assert result["pathname"] == "x.csv"


def test_delete_posts_urls(fake_api):
    BlobStore(TOKEN).delete(["https://storeabc123.private.blob.vercel-storage.com/a"])
    method, path, _headers, body = fake_api[0]
    assert (method, path) == ("POST", "/api/blob/delete")
    assert json.loads(body) == {"urls": ["https://storeabc123.private.blob.vercel-storage.com/a"]}


def test_private_reads_use_store_host_and_token():
    store = BlobStore(TOKEN)
    url = store.url_for("shopify-listing/outputs/job/a b.csv", "private")
    assert url == "https://StoreAbc123.private.blob.vercel-storage.com/shopify-listing/outputs/job/a%20b.csv"
    req = store._get_request(url, "private")
    assert req.get_header("Authorization") == f"Bearer {TOKEN}"
    assert store._get_request(url, "public").get_header("Authorization") is None
    with pytest.raises(BlobStoreError):
        store._get_request("https://example.com/a.csv", "private")


def test_missing_token_is_rejected():
    with pytest.raises(BlobStoreError):
        BlobStore("")
