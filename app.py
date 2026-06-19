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

from flask import Flask, abort, render_template, request, send_from_directory
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from processors import run_pipeline


BASE_DIR = Path(__file__).resolve().parent
ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".xlsb"}
IS_VERCEL = os.environ.get("VERCEL") == "1"
DEFAULT_MAX_UPLOAD_MB = 4 if IS_VERCEL else 200
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", DEFAULT_MAX_UPLOAD_MB))
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", 2 * 60 * 60))
BLOB_UPLOAD_FIELDS = {
    "content_master": {"extensions": {".xlsx", ".xls"}},
    "gs1": {"extensions": {".xlsx", ".xls", ".xlsb"}},
    "dropbox_links": {"extensions": {".xlsx", ".xls"}},
    "product_listing": {"extensions": {".csv", ".xlsx", ".xls"}},
}
INSTANCE_PATH = (
    Path(tempfile.gettempdir()) / "shopify-listing-instance"
    if IS_VERCEL
    else BASE_DIR / "instance"
)


app = Flask(
    __name__,
    instance_relative_config=True,
    instance_path=str(INSTANCE_PATH),
    static_folder=None,
)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or (
    uuid.uuid4().hex if IS_VERCEL else "local-dev-secret-key"
)
Path(app.instance_path).mkdir(parents=True, exist_ok=True)


def job_root() -> Path:
    configured = os.environ.get("SHOPIFY_JOB_ROOT")
    if configured:
        root = Path(configured)
    elif IS_VERCEL:
        root = Path(tempfile.gettempdir()) / "shopify-listing-jobs"
    else:
        root = Path(app.instance_path) / "jobs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def cleanup_old_jobs() -> None:
    root = job_root()
    cutoff = time.time() - JOB_TTL_SECONDS
    for child in root.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


def save_upload(job_dir: Path, field_name: str) -> Path:
    uploaded = request.files.get(field_name)
    if uploaded is None or uploaded.filename == "":
        raise ValueError(f"Missing required upload: {field_name}")

    original_name = secure_filename(uploaded.filename)
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise ValueError(f"{uploaded.filename} is not supported. Allowed: {allowed}")

    upload_dir = job_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / f"{field_name}{suffix}"
    uploaded.save(path)
    return path


def blob_uploads_enabled() -> bool:
    return bool(os.environ.get("BLOB_READ_WRITE_TOKEN")) and (
        IS_VERCEL or os.environ.get("ENABLE_BLOB_UPLOADS") == "1"
    )


def validate_extension(filename: str, field_name: str) -> str:
    suffix = Path(filename).suffix.lower()
    allowed = BLOB_UPLOAD_FIELDS[field_name]["extensions"]
    if suffix not in allowed:
        pretty = ", ".join(sorted(allowed))
        raise ValueError(f"{filename} is not supported for {field_name}. Allowed: {pretty}")
    return suffix


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

    for field_name in BLOB_UPLOAD_FIELDS:
        item = data.get(field_name)
        if not isinstance(item, dict):
            raise ValueError(f"Missing Blob upload metadata for {field_name}.")
        filename = secure_filename(str(item.get("filename") or item.get("pathname") or ""))
        url = str(item.get("url") or item.get("downloadUrl") or item.get("download_url") or "")
        if not filename or not url:
            raise ValueError(f"Incomplete Blob upload metadata for {field_name}.")
        validate_extension(filename, field_name)
        validate_blob_url(url)
    return data


def download_blob_upload(job_dir: Path, field_name: str, upload_info: dict) -> Path:
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

    client = BlobClient()
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

        BlobClient().delete(urls)
    except Exception:
        app.logger.warning("Could not delete temporary input Blob uploads.", exc_info=True)


def optional_text(name: str) -> str | None:
    value = request.form.get(name, "").strip()
    return value or None


def preview_table(df):
    preferred = [
        "SKU",
        "Title",
        "Variant Barcode",
        "Vendor",
        "Image Src",
        "Image Position",
        "Variant Image",
        "Type",
        "Product Category",
        "Variant Price",
        "Variant SKU",
        "Status",
    ]
    columns = [col for col in preferred if col in df.columns]
    if len(columns) < 4:
        for col in df.columns:
            if col not in columns and len(columns) < 8:
                columns.append(col)
    return (
        df[columns]
        .head(25)
        .fillna("")
        .to_html(index=False, classes="preview-table", border=0)
    )


def build_report_stats(pipeline_result):
    fill_stats = pipeline_result["fill_stats"]
    image_stats = pipeline_result["image_stats"]
    warnings = []
    warnings.extend(pipeline_result["content_info"].get("warnings", []))
    warnings.extend(pipeline_result["gs1_info"].get("warnings", []))
    warnings.extend(pipeline_result["product_info"].get("warnings", []))
    warnings.extend(pipeline_result["content_transform_info"].get("warnings", []))
    warnings.extend(fill_stats.get("warnings", []))
    warnings.extend(image_stats.get("warnings", []))

    missing_cm = set(fill_stats.get("missing_cm_skus", []))
    missing_gs1 = set(fill_stats.get("missing_gs1_skus", []))
    missing_images = set(image_stats.get("missing_image_skus", []))
    failed_skus = missing_cm | missing_gs1 | missing_images
    total_skus = int(fill_stats.get("rows_with_sku", 0) or 0)
    passed_skus = max(total_skus - len(failed_skus), 0)
    lookup_failed = missing_cm | missing_gs1
    lookup_passed = max(total_skus - len(lookup_failed), 0)

    return {
        "status": "Passed" if not failed_skus and not warnings else "Needs Review",
        "total_skus": total_skus,
        "passed_skus": passed_skus,
        "failed_skus": len(failed_skus),
        "lookup_passed": lookup_passed,
        "missing_content": len(missing_cm),
        "missing_gs1": len(missing_gs1),
        "missing_images": len(missing_images),
        "warning_count": len(warnings),
        "output_rows": image_stats.get("result_rows", 0),
        "images_written": image_stats.get("image_urls_written", 0),
    }


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
        "On Vercel, Function request and response payloads are capped at 4.5 MB; "
        "large production uploads need Vercel Blob or another direct-upload storage."
    )
    return render_template(
        "index.html",
        result=None,
        error=message,
        upload_limit_mb=MAX_UPLOAD_MB,
        is_vercel=IS_VERCEL,
        blob_uploads_enabled=blob_uploads_enabled(),
    ), 413


@app.get("/")
def index():
    return render_template(
        "index.html",
        result=None,
        error=None,
        upload_limit_mb=MAX_UPLOAD_MB,
        is_vercel=IS_VERCEL,
        blob_uploads_enabled=blob_uploads_enabled(),
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok", "runtime": "vercel" if IS_VERCEL else "local"}


@app.get("/static/<path:filename>")
def static_asset(filename: str):
    return send_from_directory(BASE_DIR / "public" / "static", filename)


@app.post("/process")
def process():
    cleanup_old_jobs()
    job_id = uuid.uuid4().hex
    job_dir = job_root() / job_id
    output_dir = job_dir / "outputs"
    job_dir.mkdir(parents=True, exist_ok=True)
    blob_uploads = {}

    try:
        blob_uploads = parse_blob_uploads()
        if blob_uploads:
            content_path = download_blob_upload(job_dir, "content_master", blob_uploads["content_master"])
            gs1_path = download_blob_upload(job_dir, "gs1", blob_uploads["gs1"])
            dropbox_path = download_blob_upload(job_dir, "dropbox_links", blob_uploads["dropbox_links"])
            product_path = download_blob_upload(job_dir, "product_listing", blob_uploads["product_listing"])
        else:
            content_path = save_upload(job_dir, "content_master")
            gs1_path = save_upload(job_dir, "gs1")
            dropbox_path = save_upload(job_dir, "dropbox_links")
            product_path = save_upload(job_dir, "product_listing")

        pipeline_result = run_pipeline(
            content_path,
            gs1_path,
            dropbox_path,
            product_path,
            output_dir,
            content_sheet=optional_text("content_sheet"),
            gs1_sheet=optional_text("gs1_sheet"),
            dropbox_sheet=optional_text("dropbox_sheet"),
        )

        shutil.rmtree(job_dir / "uploads", ignore_errors=True)
        blob_downloads = upload_outputs_to_blob(
            output_dir,
            pipeline_result["downloads"],
            job_id,
        )
        delete_blob_uploads(blob_uploads)

        result = {
            "job_id": job_id,
            "preview_html": preview_table(pipeline_result["final_df"]),
            "content_info": pipeline_result["content_info"],
            "gs1_info": pipeline_result["gs1_info"],
            "product_info": pipeline_result["product_info"],
            "content_transform_info": pipeline_result["content_transform_info"],
            "fill_stats": pipeline_result["fill_stats"],
            "image_stats": pipeline_result["image_stats"],
            "report_stats": build_report_stats(pipeline_result),
            "downloads": pipeline_result["downloads"],
            "blob_downloads": blob_downloads,
        }
        return render_template(
            "index.html",
            result=result,
            error=None,
            upload_limit_mb=MAX_UPLOAD_MB,
            is_vercel=IS_VERCEL,
            blob_uploads_enabled=blob_uploads_enabled(),
        )
    except Exception as exc:
        delete_blob_uploads(blob_uploads)
        shutil.rmtree(job_dir, ignore_errors=True)
        return render_template(
            "index.html",
            result=None,
            error=str(exc),
            upload_limit_mb=MAX_UPLOAD_MB,
            is_vercel=IS_VERCEL,
            blob_uploads_enabled=blob_uploads_enabled(),
        ), 400


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
