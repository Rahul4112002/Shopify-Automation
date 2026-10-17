import io

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
