import io
from pathlib import Path

import pandas as pd
import pytest

import app as app_module


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOPIFY_JOB_ROOT", str(tmp_path / "jobs"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def _xlsx_bytes(frames: dict[str, pd.DataFrame], header: bool = True) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, frame in frames.items():
            frame.to_excel(writer, index=False, header=header, sheet_name=name)
    return buffer.getvalue()


def _uploads(include_dropbox: bool):
    content = pd.DataFrame(
        {
            "BZ CODE": ["HUFC3614041K2"],
            "Product Title": ["Akiki London Maldon For Women|Trunk Bag|Small|Top Handle (Red)"],
            "Brand Name": ["Akiki London"],
            "Colour": ["RED"],
            "Product Category": ["handbag"],
            "Subcategory": ["Trunk Bag"],
            "Bullet Point 1": ["Roomy"],
        }
    )
    gs1 = pd.DataFrame(
        {
            "Article": ["HUFC3614041K2"],
            "EAN/UPC": [8903606612252],
            "Colour": ["RED"],
            "Size": ["SMALL"],
            "NEW MRP": [6499],
            "Country": ["CHINA"],
            "Family": ["MALDON"],
        }
    )
    products = pd.DataFrame({"SKU": ["HUFC3614041K2"], "Title": [""], "Image Src": [""], "Variant Barcode": [""]})
    data = {
        "content_master": (io.BytesIO(_xlsx_bytes({"New Format": content})), "content.xlsx"),
        "gs1": (io.BytesIO(_xlsx_bytes({"Master": gs1})), "gs1.xlsx"),
        "product_listing": (io.BytesIO(products.to_csv(index=False).encode()), "products.csv"),
    }
    if include_dropbox:
        dropbox = pd.DataFrame([["HUFC3614041K2", "https://www.dropbox.com/scl/fi/a/1.jpg?dl=0"]])
        data["dropbox_links"] = (io.BytesIO(_xlsx_bytes({"Sheet1": dropbox}, header=False)), "dropbox.xlsx")
    return data


def test_home_lists_both_stores(client):
    response = client.get("/")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "LAVIE" in html and "AKIKI LONDON" in html
    assert "/store/lavie" in html and "/store/akiki" in html


@pytest.mark.parametrize("store", ["lavie", "akiki"])
def test_store_page_shows_four_uploads(client, store):
    html = client.get(f"/store/{store}").get_data(as_text=True)
    for field in ("content_master", "gs1", "dropbox_links", "product_listing"):
        assert f'name="{field}"' in html
    assert "Optional" in html
    assert html.count("Required") >= 3


def test_unknown_store_is_404(client):
    assert client.get("/store/other").status_code == 404


def test_process_without_dropbox_shows_cross_check_message(client):
    response = client.post("/store/akiki/process", data=_uploads(False), content_type="multipart/form-data")
    html = response.get_data(as_text=True)
    assert response.status_code == 200, html
    assert "Please cross-check before you import to Shopify" in html
    assert "Maldon Small Red Trunk Bag" in html
    assert "8903606612252" in html


def test_process_with_dropbox_adds_image_links(client):
    response = client.post("/store/akiki/process", data=_uploads(True), content_type="multipart/form-data")
    html = response.get_data(as_text=True)
    assert response.status_code == 200, html
    assert "dl.dropboxusercontent.com" in html


def test_missing_required_file_shows_error(client):
    data = _uploads(False)
    data.pop("gs1")
    response = client.post("/store/lavie/process", data=data, content_type="multipart/form-data")
    assert response.status_code == 400
    assert "Please upload the GS sheet file." in response.get_data(as_text=True)


class FakeBlobClient:
    def __init__(self):
        self.calls = []

    def download_to(self, url, path, access="public"):
        self.calls.append(("download_file", url, access))
        Path(path).write_bytes(b"data")
        return Path(path)

    def put(self, path, body, access="public", content_type=None):
        self.calls.append(("put", path, access))
        return {"url": f"https://store.{access}.blob.vercel-storage.com/{path}"}

    def read(self, path, access="public"):
        self.calls.append(("get", path, access))
        return b"SKU,Title\nA,B\n"


def test_private_blob_store_is_used_for_uploads_and_downloads(client, tmp_path, monkeypatch):
    fake = FakeBlobClient()
    monkeypatch.setattr(app_module, "blob_client", lambda: fake)
    monkeypatch.setattr(app_module, "blob_uploads_enabled", lambda: True)
    monkeypatch.delenv("BLOB_ACCESS", raising=False)

    path = app_module.download_blob_upload(
        tmp_path,
        "product_listing",
        {"url": "https://abc.private.blob.vercel-storage.com/x/products.csv", "filename": "products.csv"},
    )
    assert path.read_bytes() == b"data"
    assert fake.calls[-1] == ("download_file", "https://abc.private.blob.vercel-storage.com/x/products.csv", "private")

    out = tmp_path / "out"
    out.mkdir()
    (out / "lavie_shopify_import_ready.csv").write_text("x")
    links = app_module.upload_outputs_to_blob(out, {"csv": "lavie_shopify_import_ready.csv"}, "job123")
    assert links == {}  # private store: links go through /download
    assert fake.calls[-1] == ("put", "shopify-listing/outputs/job123/lavie_shopify_import_ready.csv", "private")

    response = client.get("/download/job123/lavie_shopify_import_ready.csv")
    assert response.status_code == 200
    assert response.data == b"SKU,Title\nA,B\n"
    assert "attachment" in response.headers["Content-Disposition"]
    assert fake.calls[-1] == ("get", "shopify-listing/outputs/job123/lavie_shopify_import_ready.csv", "private")


def test_public_blob_store_returns_direct_links(tmp_path, monkeypatch):
    fake = FakeBlobClient()
    monkeypatch.setattr(app_module, "blob_client", lambda: fake)
    monkeypatch.setattr(app_module, "blob_uploads_enabled", lambda: True)
    monkeypatch.setenv("BLOB_ACCESS", "public")
    out = tmp_path / "out"
    out.mkdir()
    (out / "a.csv").write_text("x")

    links = app_module.upload_outputs_to_blob(out, {"csv": "a.csv"}, "job1")

    assert links["csv"].startswith("https://store.public.blob")


def test_blob_cleanup_only_deletes_temporary_inputs(client, monkeypatch):
    deleted = []
    monkeypatch.setattr(app_module, "blob_uploads_enabled", lambda: True)
    monkeypatch.setattr(app_module, "delete_blob_uploads", lambda uploads: deleted.extend(i["url"] for i in uploads.values()))

    response = client.post(
        "/blob-cleanup",
        json={
            "urls": [
                "https://abc.private.blob.vercel-storage.com/shopify-listing/input/job/a.xlsx",
                "https://abc.private.blob.vercel-storage.com/shopify-listing/outputs/job/a.csv",
                "https://evil.example.com/shopify-listing/input/job/a.xlsx",
            ]
        },
    )

    assert response.get_json() == {"deleted": 1}
    assert deleted == ["https://abc.private.blob.vercel-storage.com/shopify-listing/input/job/a.xlsx"]
