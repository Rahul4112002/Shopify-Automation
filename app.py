from __future__ import annotations

import os
import json
import mimetypes
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from flask import Flask, abort, redirect, render_template, request, send_from_directory, url_for
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent


def load_local_env(path: Path = BASE_DIR / ".env") -> None:
    """Tiny .env loader for local runs (Vercel injects environment variables itself)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_local_env()

from ai_titles import build_title_generator, configured_models, gemini_api_key  # noqa: E402
from catalog_rules import STORES, get_store  # noqa: E402
from processors import BARCODE_COLUMNS, run_pipeline  # noqa: E402


ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".xlsb"}
IS_VERCEL = os.environ.get("VERCEL") == "1"
IS_SERVERLESS = IS_VERCEL or bool(
    os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or os.environ.get("LAMBDA_TASK_ROOT")
)
DEFAULT_MAX_UPLOAD_MB = 4 if IS_VERCEL else 200


def int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


MAX_UPLOAD_MB = int_env("MAX_UPLOAD_MB", DEFAULT_MAX_UPLOAD_MB)
JOB_TTL_SECONDS = int_env("JOB_TTL_SECONDS", 2 * 60 * 60)
UPLOAD_FIELDS = {
    "content_master": {"extensions": {".xlsx", ".xls"}, "required": True},
    "gs1": {"extensions": {".xlsx", ".xls", ".xlsb"}, "required": True},
    "dropbox_links": {"extensions": {".xlsx", ".xls", ".csv"}, "required": False},
    "product_listing": {"extensions": {".csv", ".xlsx", ".xls"}, "required": True},
}
# Kept for compatibility with older imports.
BLOB_UPLOAD_FIELDS = UPLOAD_FIELDS
INSTANCE_PATH = (
    Path(tempfile.gettempdir()) / "shopify-listing-instance"
    if IS_SERVERLESS
    else BASE_DIR / "instance"
)


def ensure_directory(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except OSError:
        fallback = Path(tempfile.gettempdir()) / path.name
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


app = Flask(
    __name__,
    instance_relative_config=True,
    instance_path=str(ensure_directory(INSTANCE_PATH)),
    static_folder=None,
)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or (
    uuid.uuid4().hex if IS_VERCEL else "local-dev-secret-key"
)


# ---------------------------------------------------------------------------
# Upload guidance shown on each store page
# ---------------------------------------------------------------------------


def upload_cards(store_key: str) -> list[dict]:
    is_lavie = store_key == "lavie"
    return [
        {
            "name": "content_master",
            "step": 1,
            "label": "Content Master sheet",
            "required": True,
            "accept": ".xlsx,.xls",
            "formats": "XLSX or XLS",
            "summary": "The CONTENT MASTER workbook. The “New Format” sheet is picked automatically.",
            "uses": [
                ("BZ CODE", "matched with the SKU"),
                ("Subcategory", "becomes Type (spelling cleaned)"),
                (
                    "Product Title, BZ Description",
                    "read by AI to write the title name" if is_lavie else "backup when GS has no Family",
                ),
                ("Bullet Points, Special Features", "become Body (HTML)"),
                ("Brand Name, Description, Gender, Material…", "Vendor, SEO and metafields"),
            ],
        },
        {
            "name": "gs1",
            "step": 2,
            "label": "GS sheet (GS1 Master)",
            "required": True,
            "accept": ".xlsb,.xlsx,.xls",
            "formats": "XLSB, XLSX or XLS",
            "summary": "The GS1 Master workbook. The “Master” sheet and its header row are found automatically.",
            "uses": [
                ("Article", "matched with the SKU"),
                ("EAN/UPC", "Variant Barcode and Google Shopping MPN, as plain numbers"),
                ("Colour", "Option1 Value (Color)"),
                ("Size", "Option2 Value (Size)"),
                ("NEW MRP", "Price and Compare-at price"),
                ("Family", "title name" if not is_lavie else "extra context for AI titles"),
                ("Country, System Dimension", "origin, manufacturer and dimensions"),
            ],
        },
        {
            "name": "dropbox_links",
            "step": 3,
            "label": "Dropbox image links",
            "required": False,
            "accept": ".xlsx,.xls,.csv",
            "formats": "XLSX, XLS or CSV",
            "summary": "Optional. Upload it if you have image links; skip it and the file is made without image links.",
            "uses": [
                ("Column A", "SKU, no header row needed"),
                ("Column B onwards", "image links in display order (image 1, 2, 3…)"),
                ("Result", "one Shopify image row per link, alt text = title"),
            ],
        },
        {
            "name": "product_listing",
            "step": 4,
            "label": "Product list (SKUs)",
            "required": True,
            "accept": ".csv,.xlsx,.xls",
            "formats": "CSV, XLSX or XLS",
            "summary": "Your Shopify product export template with the SKU column filled for the products to list.",
            "uses": [
                ("SKU", "one row per product to list"),
                ("Other columns", "Shopify import columns, filled in by the tool"),
                ("Rows without SKU", "kept exactly as they are"),
            ],
        },
    ]


def ai_status(store) -> dict:
    enabled = store.title_strategy == "ai"
    has_key = bool(gemini_api_key())
    return {
        "applies": enabled,
        "configured": has_key,
        "model": configured_models()[0] if has_key else None,
    }


# ---------------------------------------------------------------------------
# Job storage and uploads
# ---------------------------------------------------------------------------


def job_root() -> Path:
    configured = os.environ.get("SHOPIFY_JOB_ROOT")
    if configured:
        root = Path(configured)
    elif IS_SERVERLESS:
        root = Path(tempfile.gettempdir()) / "shopify-listing-jobs"
    else:
        root = Path(app.instance_path) / "jobs"
    return ensure_directory(root)


def cleanup_old_jobs() -> None:
    root = job_root()
    cutoff = time.time() - JOB_TTL_SECONDS
    for child in root.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


def validate_extension(filename: str, field_name: str) -> str:
    suffix = Path(filename).suffix.lower()
    allowed = UPLOAD_FIELDS[field_name]["extensions"]
    if suffix not in allowed:
        pretty = ", ".join(sorted(allowed))
        raise ValueError(f"{filename} is not supported for {field_label(field_name)}. Allowed: {pretty}")
    return suffix


def field_label(field_name: str) -> str:
    return {
        "content_master": "Content Master",
        "gs1": "GS sheet",
        "dropbox_links": "Dropbox image links",
        "product_listing": "Product list",
    }.get(field_name, field_name)


def save_upload(job_dir: Path, field_name: str) -> Path | None:
    uploaded = request.files.get(field_name)
    if uploaded is None or uploaded.filename == "":
        if UPLOAD_FIELDS[field_name]["required"]:
            raise ValueError(f"Please upload the {field_label(field_name)} file.")
        return None

    original_name = secure_filename(uploaded.filename) or uploaded.filename
    suffix = validate_extension(original_name, field_name)
    upload_dir = job_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / f"{field_name}{suffix}"
    uploaded.save(path)
    return path


def blob_read_write_token() -> str | None:
    return os.environ.get("BLOB_READ_WRITE_TOKEN") or os.environ.get(
        "BLOB_READ_WRITE_TOKEN_READ_WRITE_TOKEN"
    )


def blob_uploads_enabled() -> bool:
    return bool(blob_read_write_token()) and (
        IS_SERVERLESS or os.environ.get("ENABLE_BLOB_UPLOADS") == "1"
    )


def validate_blob_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc.endswith(".blob.vercel-storage.com"):
        raise ValueError("Invalid Vercel Blob URL received.")


def parse_blob_uploads() -> dict:
    raw = request.form.get("blob_uploads", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid Blob upload metadata.") from exc

    if not isinstance(data, dict):
        raise ValueError("Invalid Blob upload metadata.")

    for field_name, config in UPLOAD_FIELDS.items():
        item = data.get(field_name)
        if item is None and not config["required"]:
            continue
        if not isinstance(item, dict):
            raise ValueError(f"Missing upload for {field_label(field_name)}.")
        filename = secure_filename(str(item.get("filename") or item.get("pathname") or ""))
        url = str(item.get("url") or item.get("downloadUrl") or item.get("download_url") or "")
        if not filename or not url:
            raise ValueError(f"Incomplete Blob upload metadata for {field_label(field_name)}.")
        validate_extension(filename, field_name)
        validate_blob_url(url)
    return data


def download_blob_upload(job_dir: Path, field_name: str, upload_info: dict | None) -> Path | None:
    if not upload_info:
        return None
    filename = secure_filename(str(upload_info.get("filename") or upload_info.get("pathname") or ""))
    suffix = validate_extension(filename, field_name)
    url = str(upload_info.get("downloadUrl") or upload_info.get("download_url") or upload_info.get("url") or "")
    validate_blob_url(url)

    upload_dir = job_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / f"{field_name}{suffix}"
    with urlopen(url, timeout=120) as response:
        with path.open("wb") as destination:
            shutil.copyfileobj(response, destination)
    return path


def blob_result_value(result, *names: str) -> str:
    if isinstance(result, dict):
        for name in names:
            value = result.get(name)
            if value:
                return value
    for name in names:
        value = getattr(result, name, None)
        if value:
            return value
    return ""


def upload_outputs_to_blob(output_dir: Path, downloads: dict, job_id: str) -> dict:
    if not blob_uploads_enabled():
        return {}

    try:
        from vercel.blob import BlobClient
    except ImportError as exc:
        raise RuntimeError("The Python package 'vercel' is required for Blob output uploads.") from exc

    client = BlobClient(token=blob_read_write_token())
    uploaded = {}
    for key, filename in downloads.items():
        path = output_dir / filename
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        blob = client.put(
            f"shopify-listing/outputs/{job_id}/{path.name}",
            path.read_bytes(),
            access="public",
            content_type=content_type,
            overwrite=True,
        )
        uploaded[key] = blob_result_value(blob, "download_url", "downloadUrl", "url")
    return uploaded


def delete_blob_uploads(blob_uploads: dict) -> None:
    urls = []
    for item in blob_uploads.values():
        if isinstance(item, dict):
            url = item.get("url") or item.get("downloadUrl") or item.get("download_url")
            if url:
                urls.append(url)
    if not urls or not blob_uploads_enabled():
        return

    try:
        from vercel.blob import BlobClient

        BlobClient(token=blob_read_write_token()).delete(urls)
    except Exception:
        app.logger.warning("Could not delete temporary input Blob uploads.", exc_info=True)


def optional_text(name: str) -> str | None:
    value = request.form.get(name, "").strip()
    return value or None


# ---------------------------------------------------------------------------
# Result presentation
# ---------------------------------------------------------------------------


def build_preview(df, title_rows: list[dict]) -> dict:
    barcode_col = next((col for col in BARCODE_COLUMNS if col in df.columns), None)
    preferred = [
        ("SKU", "SKU"),
        ("Title", "Title"),
        ("Type", "Type"),
        ("Option1 Value", "Colour"),
        ("Option2 Value", "Size"),
        (barcode_col, "Barcode"),
        ("Variant Price", "Price"),
        ("Tags", "Tags"),
        ("Image Position", "Img #"),
        ("Image Src", "Image"),
    ]
    columns = [(col, label) for col, label in preferred if col and col in df.columns]
    sources = {row["SKU"]: row["Title Source"] for row in title_rows}

    rows = []
    for record in df[[col for col, _ in columns]].head(40).fillna("").to_dict("records"):
        cells = []
        for col, _ in columns:
            value = record.get(col, "")
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            cells.append({"key": col, "value": str(value)})
        rows.append({"cells": cells, "source": sources.get(str(record.get("SKU", ""))) if record.get("Title") else None})
    return {"columns": [label for _, label in columns], "keys": [col for col, _ in columns], "rows": rows}


def all_warnings(pipeline_result) -> list[str]:
    warnings: list[str] = []
    for key in ("content_info", "gs1_info", "product_info", "content_transform_info", "fill_stats", "image_stats"):
        warnings.extend(pipeline_result[key].get("warnings", []))
    return list(dict.fromkeys(warnings))


def build_report_stats(pipeline_result):
    fill_stats = pipeline_result["fill_stats"]
    image_stats = pipeline_result["image_stats"]
    title_info = fill_stats.get("title_info", {})
    warnings = all_warnings(pipeline_result)

    missing_cm = set(fill_stats.get("missing_cm_skus", []))
    missing_gs1 = set(fill_stats.get("missing_gs1_skus", []))
    missing_barcode = set(fill_stats.get("missing_barcode_skus", []))
    images_skipped = bool(image_stats.get("skipped"))
    missing_images = set() if images_skipped else set(image_stats.get("missing_image_skus", []))
    review_skus = missing_cm | missing_gs1 | missing_barcode | missing_images
    total_skus = int(fill_stats.get("rows_with_sku", 0) or 0)

    return {
        "status": "Ready" if not review_skus else "Needs review",
        "total_skus": total_skus,
        "ready_skus": max(total_skus - len(review_skus), 0),
        "review_skus": len(review_skus),
        "missing_content": len(missing_cm),
        "missing_gs1": len(missing_gs1),
        "missing_barcode": len(missing_barcode),
        "missing_images": len(missing_images),
        "images_skipped": images_skipped,
        "warning_count": len(warnings),
        "output_rows": image_stats.get("result_rows", 0),
        "images_written": image_stats.get("image_urls_written", 0),
        "ai_titles": title_info.get("ai_titles", 0),
        "rule_titles": title_info.get("rule_titles", 0),
        "family_titles": title_info.get("family_titles", 0),
        "ai_model": title_info.get("model"),
        "ai_errors": title_info.get("errors", [])[:3],
    }


def render_store(store, *, result=None, error=None, status=200):
    return (
        render_template(
            "store.html",
            store=store,
            active_store=store.key,
            cards=upload_cards(store.key),
            ai=ai_status(store),
            result=result,
            error=error,
            upload_limit_mb=MAX_UPLOAD_MB,
            is_vercel=IS_VERCEL,
            blob_uploads_enabled=blob_uploads_enabled(),
        ),
        status,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.after_request
def apply_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.errorhandler(RequestEntityTooLarge)
def handle_large_upload(_error):
    message = (
        f"Upload is larger than the configured {MAX_UPLOAD_MB} MB limit. "
        "On Vercel, Function request payloads are capped at 4.5 MB; "
        "large production uploads need Vercel Blob enabled."
    )
    store_key = (request.view_args or {}).get("store_key", "lavie")
    try:
        store = get_store(store_key)
    except ValueError:
        store = get_store("lavie")
    return render_store(store, error=message, status=413)


@app.get("/")
def index():
    return render_template(
        "home.html",
        stores=list(STORES.values()),
        ai_configured=bool(gemini_api_key()),
    )


@app.get("/store/<store_key>")
def store_page(store_key: str):
    try:
        store = get_store(store_key)
    except ValueError:
        abort(404)
    return render_store(store)


@app.get("/process")
def process_get():
    return redirect(url_for("index"))


@app.get("/healthz")
def healthz():
    return {
        "status": "ok",
        "runtime": "vercel" if IS_VERCEL else "local",
        "ai_titles": "configured" if gemini_api_key() else "not configured",
    }


@app.get("/static/<path:filename>")
def static_asset(filename: str):
    return send_from_directory(BASE_DIR / "public" / "static", filename)


@app.post("/store/<store_key>/process")
def process(store_key: str):
    try:
        store = get_store(store_key)
    except ValueError:
        abort(404)

    cleanup_old_jobs()
    job_id = uuid.uuid4().hex
    job_dir = job_root() / job_id
    output_dir = job_dir / "outputs"
    job_dir.mkdir(parents=True, exist_ok=True)
    blob_uploads = {}

    try:
        blob_uploads = parse_blob_uploads()
        if blob_uploads:
            paths = {
                field: download_blob_upload(job_dir, field, blob_uploads.get(field))
                for field in UPLOAD_FIELDS
            }
        else:
            paths = {field: save_upload(job_dir, field) for field in UPLOAD_FIELDS}
        for field, config in UPLOAD_FIELDS.items():
            if config["required"] and paths.get(field) is None:
                raise ValueError(f"Please upload the {field_label(field)} file.")

        use_ai = request.form.get("use_ai", "1") == "1"
        title_generator = build_title_generator() if (store.title_strategy == "ai" and use_ai) else None

        pipeline_result = run_pipeline(
            paths["content_master"],
            paths["gs1"],
            paths["dropbox_links"],
            paths["product_listing"],
            output_dir,
            content_sheet=optional_text("content_sheet"),
            gs1_sheet=optional_text("gs1_sheet"),
            dropbox_sheet=optional_text("dropbox_sheet"),
            store=store.key,
            title_generator=title_generator,
        )

        shutil.rmtree(job_dir / "uploads", ignore_errors=True)
        blob_downloads = upload_outputs_to_blob(output_dir, pipeline_result["downloads"], job_id)
        delete_blob_uploads(blob_uploads)

        fill_stats = pipeline_result["fill_stats"]
        result = {
            "job_id": job_id,
            "preview": build_preview(pipeline_result["final_df"], fill_stats.get("title_rows", [])),
            "content_info": pipeline_result["content_info"],
            "gs1_info": pipeline_result["gs1_info"],
            "product_info": pipeline_result["product_info"],
            "fill_stats": fill_stats,
            "image_stats": pipeline_result["image_stats"],
            "report_stats": build_report_stats(pipeline_result),
            "warnings": all_warnings(pipeline_result),
            "downloads": pipeline_result["downloads"],
            "blob_downloads": blob_downloads,
            "use_ai": use_ai,
        }
        return render_store(store, result=result)
    except Exception as exc:
        app.logger.exception("Pipeline failed")
        delete_blob_uploads(blob_uploads)
        shutil.rmtree(job_dir, ignore_errors=True)
        return render_store(store, error=str(exc), status=400)


@app.get("/download/<job_id>/<filename>")
def download(job_id: str, filename: str):
    if not job_id.replace("-", "").isalnum():
        abort(404)
    safe_name = secure_filename(filename)
    directory = job_root() / job_id / "outputs"
    if not (directory / safe_name).exists():
        abort(404)
    return send_from_directory(directory, safe_name, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
