from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import pandas as pd

from catalog_rules import (
    StoreConfig,
    clean_ai_name,
    clean_text,
    compose_title,
    derive_style_name,
    get_store,
    normalize_color,
    normalize_product_type,
    normalize_size,
    smart_title,
    style_group_key,
)


CONTENT_GENERATED_COLUMNS = [
    "Title",
    "Bullet Points HTML",
    "HTML content",
    "All Special Features HTML",
]

UNMAPPED_FINAL_COLUMNS = [
    "Color (product.metafields.shopify.color-pattern)",
    "Size (product.metafields.shopify.size)",
    "Target gender (product.metafields.shopify.target-gender)",
]


@dataclass
class LoadInfo:
    sheet_name: str | None = None
    header_row: int | None = None
    rows: int = 0
    columns: int = 0
    warnings: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "sheet_name": self.sheet_name,
            "header_row": self.header_row,
            "rows": self.rows,
            "columns": self.columns,
            "warnings": self.warnings or [],
        }


def normalize_name(value: Any) -> str:
    text = str(value).strip().lower()
    return re.sub(r"\s+", " ", text)


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except TypeError:
        return False
    text = str(value).strip()
    return text == "" or text.upper() in {"NA", "N/A", "NONE", "NAN"}


def normalize_sku(value: Any) -> str:
    if is_blank(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def unique_preserve(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = normalize_sku(value)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def strip_columns(df: pd.DataFrame, drop_unnamed: bool = False) -> pd.DataFrame:
    df = df.copy()
    cleaned_columns: list[str] = []
    for col in df.columns:
        text = str(col).strip()
        cleaned_columns.append(text)
    df.columns = cleaned_columns
    if drop_unnamed:
        valid = [
            col
            for col in df.columns
            if col and not normalize_name(col).startswith("unnamed")
        ]
        df = df[valid]
    return df


def resolve_column(columns: Iterable[Any], aliases: str | Iterable[str]) -> str | None:
    if isinstance(aliases, str):
        aliases = [aliases]
    normalized_aliases = {normalize_name(alias) for alias in aliases}
    for col in columns:
        if normalize_name(col) in normalized_aliases:
            return str(col)
    return None


def get_value(row: pd.Series, aliases: str | Iterable[str]) -> Any:
    col = resolve_column(row.index, aliases)
    if col is None:
        return None
    value = row.get(col)
    return None if is_blank(value) else value


def set_if_present(
    df: pd.DataFrame,
    index: Any,
    target: str,
    value: Any,
    warnings: set[str],
    allow_blank: bool = False,
) -> None:
    col = resolve_column(df.columns, target)
    if col is None:
        warnings.add(f"Target column missing in Product Listing: {target}")
        return
    if allow_blank or not is_blank(value):
        df.at[index, col] = value


def clear_if_present(df: pd.DataFrame, index: Any, target: str) -> None:
    col = resolve_column(df.columns, target)
    if col is not None:
        df.at[index, col] = ""


def excel_engine(path: str | Path) -> str | None:
    suffix = Path(path).suffix.lower()
    if suffix == ".xlsb":
        return "pyxlsb"
    if suffix in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
        return "openpyxl"
    return None


def read_excel_file(
    path: str | Path,
    sheet_name: str | int = 0,
    header: int | None = 0,
    nrows: int | None = None,
) -> pd.DataFrame:
    engine = excel_engine(path)
    return pd.read_excel(
        path,
        sheet_name=sheet_name,
        header=header,
        nrows=nrows,
        engine=engine,
    )


def get_excel_file(path: str | Path) -> pd.ExcelFile:
    engine = excel_engine(path)
    return pd.ExcelFile(path, engine=engine)


def load_product_list(path: str | Path) -> tuple[pd.DataFrame, LoadInfo]:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        last_error: Exception | None = None
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                df = pd.read_csv(path, encoding=encoding)
                break
            except UnicodeDecodeError as exc:
                last_error = exc
        else:
            raise last_error or ValueError("Unable to read Product Listing CSV")
        sheet_name = None
    else:
        df = read_excel_file(path, sheet_name=0)
        sheet_name = "first sheet"

    df = strip_columns(df)
    info = LoadInfo(sheet_name=sheet_name, rows=len(df), columns=len(df.columns), warnings=[])
    if resolve_column(df.columns, "SKU") is None:
        info.warnings.append("Product Listing must contain a SKU column.")
    return df, info


def find_content_sheet(path: str | Path) -> str:
    xls = get_excel_file(path)
    if len(xls.sheet_names) == 1:
        return xls.sheet_names[0]

    best_sheet = xls.sheet_names[0]
    best_score = -1
    for sheet in xls.sheet_names:
        try:
            header_df = read_excel_file(path, sheet_name=sheet, nrows=0)
            cols = [normalize_name(col) for col in header_df.columns]
            score = 0
            score += 4 if "bz code" in cols else 0
            score += 4 if "product title" in cols else 0
            score += 2 if any(col.startswith("bullet point") for col in cols) else 0
            score += 2 if any(col.startswith("special feature") for col in cols) else 0
            score -= sum(1 for col in cols if col.startswith("unnamed"))
            if score > best_score:
                best_sheet = sheet
                best_score = score
        except Exception:
            continue
    return best_sheet


def load_content_master(
    path: str | Path,
    sheet_name: str | None = None,
) -> tuple[pd.DataFrame, LoadInfo]:
    selected_sheet = sheet_name or find_content_sheet(path)
    df = read_excel_file(path, sheet_name=selected_sheet)
    df = strip_columns(df, drop_unnamed=True)
    info = LoadInfo(
        sheet_name=selected_sheet,
        rows=len(df),
        columns=len(df.columns),
        warnings=[],
    )
    return df, info


def clean_title(title: Any) -> Any:
    if is_blank(title):
        return title
    title_text = str(title)
    title_text = re.sub(r"\([^)]*\)", "", title_text)
    gender_terms = [
        r"\bwomen's\b",
        r"\bwomens\b",
        r"\bwoman\b",
        r"\bwomen\b",
        r"\bmen's\b",
        r"\bmens\b",
        r"\bman\b",
        r"\bmen\b",
        r"\bfor women\b",
        r"\bfor womens\b",
        r"\bfor men\b",
        r"\bfor mens\b",
        r"\bfor men & women\b",
        r"\bgirls\b",
        r"\bgirl\b",
        r"\bboys\b",
        r"\bboy\b",
        r"\bof\b",
        r"\bboys & girls\b",
        r"\|",
    ]
    for term in gender_terms:
        title_text = re.sub(term, "", title_text, flags=re.IGNORECASE)
    title_text = re.sub(r"\bSatchel\b", "Satchel Bag", title_text, flags=re.IGNORECASE)
    title_text = re.sub(r"\bTote\b", "Tote Bag", title_text, flags=re.IGNORECASE)
    title_text = re.sub(r"\bSling\b", "Sling Bag", title_text, flags=re.IGNORECASE)
    title_text = title_text.replace("-", "")
    title_text = " ".join(title_text.split())
    return title_text.title().strip()


def standardize_color(color_value: Any) -> Any:
    if is_blank(color_value):
        return color_value
    color_text = str(color_value).strip()
    if color_text.lower() == "p blue":
        return "Pale Blue"
    return color_text.title()


def numbered_column_sort_key(column: str) -> tuple[str, int]:
    match = re.search(r"(\d+)", str(column))
    number = int(match.group(1)) if match else 999
    return (normalize_name(column), number)


def build_bullet_points_html(row: pd.Series, bullet_cols: list[str]) -> str:
    parts = []
    for col in bullet_cols:
        value = row.get(col)
        if not is_blank(value):
            parts.append(f"<li>{str(value).strip()}</li>")
    return "".join(parts)


def build_special_features_html(row: pd.Series, special_cols: list[str]) -> str:
    features = []
    for col in special_cols:
        value = row.get(col)
        if not is_blank(value):
            features.append(str(value).strip().rstrip("."))
    if features:
        return f"<li><b>Special Features : </b>{', '.join(features)}</li>"
    return ""


def transform_content_master(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    df = strip_columns(df, drop_unnamed=True)
    info: dict[str, Any] = {
        "original_rows": len(df),
        "original_cols": len(df.columns),
        "product_title_col": None,
        "bullet_cols": [],
        "special_cols": [],
        "color_standardized": False,
        "warnings": [],
    }

    colour_col = resolve_column(df.columns, ["Colour", "Color"])
    if colour_col is not None:
        df[colour_col] = df[colour_col].apply(standardize_color)
        info["color_standardized"] = True

    product_title_col = resolve_column(df.columns, "Product Title")
    if product_title_col is None:
        product_title_col = resolve_column(df.columns, "Title")
    if product_title_col is None:
        raise ValueError(
            "No Product Title or Title column found in Content Master. "
            f"Available columns: {list(df.columns)}"
        )
    info["product_title_col"] = product_title_col

    bullet_cols = sorted(
        [col for col in df.columns if normalize_name(col).startswith("bullet point")],
        key=numbered_column_sort_key,
    )
    special_cols = sorted(
        [col for col in df.columns if normalize_name(col).startswith("special feature")],
        key=numbered_column_sort_key,
    )
    info["bullet_cols"] = bullet_cols
    info["special_cols"] = special_cols

    title_data = df[product_title_col].apply(clean_title)
    bullet_html_data = df.apply(lambda row: build_bullet_points_html(row, bullet_cols), axis=1)
    special_html_data = df.apply(lambda row: build_special_features_html(row, special_cols), axis=1)
    html_content_data = "<ul>" + bullet_html_data + special_html_data + "</ul>"

    base_cols = [
        col
        for col in df.columns
        if col not in CONTENT_GENERATED_COLUMNS or col == product_title_col
    ]
    base_df = df[base_cols].copy()
    base_df["Title"] = title_data
    base_df["Bullet Points HTML"] = bullet_html_data
    base_df["HTML content"] = html_content_data
    base_df["All Special Features HTML"] = special_html_data

    first_bullet = bullet_cols[0] if bullet_cols else None
    first_special = special_cols[0] if special_cols else None
    final_cols: list[str] = []
    for col in base_cols:
        if col == first_bullet and "Bullet Points HTML" not in final_cols:
            final_cols.append("Bullet Points HTML")
        if col == first_special:
            final_cols.extend(["HTML content", "All Special Features HTML"])
        final_cols.append(col)
        if col == product_title_col and product_title_col != "Title":
            final_cols.append("Title")

    if "Bullet Points HTML" not in final_cols:
        final_cols.append("Bullet Points HTML")
    if "HTML content" not in final_cols:
        final_cols.extend(["HTML content", "All Special Features HTML"])

    final_cols = [col for idx, col in enumerate(final_cols) if col not in final_cols[:idx]]
    result = base_df[final_cols]
    info["final_cols"] = len(result.columns)
    return result, info


def score_sheet_columns(columns: Iterable[Any]) -> int:
    cols = [normalize_name(col) for col in columns]
    score = 0
    score += 5 if "article" in cols else 0
    score += 3 if "ean/upc" in cols else 0
    score += 3 if "new mrp" in cols else 0
    score += 2 if "size" in cols else 0
    score += 2 if "country" in cols else 0
    score += 1 if "family" in cols else 0
    score += 1 if "colour" in cols or "color" in cols else 0
    # A single leading blank column is normal in the GS1 master; only punish wider gaps.
    unnamed = sum(1 for col in cols if col.startswith("unnamed") or col in {"", "nan", "none"})
    score -= max(0, unnamed - 2)
    return score


def score_sheet_name(sheet: str) -> int:
    name = normalize_name(sheet)
    if name in {"master", "master sheet", "gs1 master", "gs master"}:
        return 6
    if any(word in name for word in ("discontinued", "blocked", "map ", "kitting", "non merchandise")):
        return -6
    if "master" in name:
        return 2
    return 0


def detect_gs1_sheet_and_header(
    path: str | Path,
    requested_sheet: str | None = None,
) -> tuple[str, int]:
    xls = get_excel_file(path)
    candidate_sheets = [requested_sheet] if requested_sheet else list(xls.sheet_names)

    best_sheet = candidate_sheets[0]
    best_header = 0
    best_score = -999
    for sheet in candidate_sheets:
        try:
            # Read each sheet once and score the first rows as possible header rows.
            sample = read_excel_file(path, sheet_name=sheet, header=None, nrows=6)
        except Exception:
            continue
        for header_row in range(min(4, len(sample))):
            score = score_sheet_columns(sample.iloc[header_row].tolist())
            if score <= 0:
                continue
            score += score_sheet_name(sheet)
            if score > best_score:
                best_sheet = sheet
                best_header = header_row
                best_score = score
    return best_sheet, best_header


def load_gs1(
    path: str | Path,
    sheet_name: str | None = None,
) -> tuple[pd.DataFrame, LoadInfo]:
    selected_sheet, header_row = detect_gs1_sheet_and_header(path, sheet_name)
    df = read_excel_file(path, sheet_name=selected_sheet, header=header_row)
    df = strip_columns(df, drop_unnamed=True)
    info = LoadInfo(
        sheet_name=selected_sheet,
        header_row=header_row,
        rows=len(df),
        columns=len(df.columns),
        warnings=[],
    )
    if resolve_column(df.columns, "Article") is None:
        info.warnings.append("GS file does not contain an Article column after auto-detection.")
    if resolve_column(df.columns, ["EAN/UPC", "EAN Code", "EAN"]) is None:
        info.warnings.append("GS file does not contain an EAN/UPC column; barcodes will be blank.")
    return df, info


def format_size(size_value: Any) -> Any:
    size = normalize_size(size_value)
    return size or None


def format_dimension(dim_value: Any) -> Any:
    if is_blank(dim_value):
        return None
    dim_text = str(dim_value).strip()
    if ";" in dim_text:
        dim_text = dim_text.split(";", 1)[1].strip()
    match = re.search(r"\((.*?)\)cm", dim_text, re.IGNORECASE)
    if match:
        return f"{match.group(1).strip()} cm"
    if dim_text.lower().endswith("cm") and not dim_text.lower().endswith(" cm"):
        return f"{dim_text[:-2]} cm"
    return dim_text


def get_manufacturer_details(country_value: Any) -> Any:
    if is_blank(country_value):
        return None
    country = str(country_value).strip().title()
    address = (
        "Bagzone Lifestyles Private Limited 401, Ackruti Star, Oppo. Ackruti Centre "
        "Point, Central Road, MIDC, Andheri East, Mumbai-400093."
    )
    if country == "India":
        return f"<p><strong>Manufactured by: </strong>{address}</p>"
    if country == "China":
        return f"<p><strong>Imported & Marketed By: </strong>{address}</p>"
    return None


def create_tags(color: Any, size: Any) -> str:
    tags: list[str] = []
    if not is_blank(color):
        tags.append(str(color).strip().lower())
    tags.extend(["nano", "new-launch"])
    if not is_blank(size):
        tags.append(str(size).strip().lower())
    return ", ".join(tags)


def remove_vendor_from_title(title: Any, vendor: Any = None) -> Any:
    if is_blank(title):
        return title
    title_text = str(title).strip()
    candidates = [
        "Lavie Signature",
        "Lavie Sport",
        "Lavie Luxe",
        "Lavie World",
        "Lavie",
    ]
    if not is_blank(vendor):
        candidates.insert(0, str(vendor).strip())

    for candidate in sorted(set(candidates), key=len, reverse=True):
        if not candidate:
            continue
        pattern = rf"^\s*{re.escape(candidate)}\b[\s\-:|]*"
        title_text = re.sub(pattern, "", title_text, flags=re.IGNORECASE)
    return " ".join(title_text.split()).strip()


def format_identifier(value: Any) -> Any:
    """Return barcodes / EANs as plain digits: no '.0', no scientific notation, no apostrophe."""
    if is_blank(value):
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return format(value, "f").rstrip("0").rstrip(".")

    text = str(value).strip().lstrip("'").replace(",", "").replace(" ", "")
    if re.fullmatch(r"\d+", text):
        return text
    if re.fullmatch(r"\d+\.0+", text):
        return text.split(".", 1)[0]
    try:
        decimal_value = Decimal(text)
    except InvalidOperation:
        return text
    if decimal_value == decimal_value.to_integral_value():
        return format(decimal_value.quantize(Decimal(1)), "f")
    return format(decimal_value.normalize(), "f")


def format_price(value: Any) -> Any:
    if is_blank(value):
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip().replace(",", "")
    if re.fullmatch(r"\d+\.0+", text):
        return int(text.split(".", 1)[0])
    if re.fullmatch(r"\d+", text):
        return int(text)
    return value


def slugify_part(value: Any) -> str:
    if is_blank(value):
        return ""
    text = str(value).strip().lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def make_handle(vendor: Any, title: Any, size: Any = None, color: Any = None) -> str:
    """vendor-title, adding size / colour only when the title does not already contain them."""
    title_slug = slugify_part(title)
    parts = [slugify_part(vendor), title_slug]
    for extra in (size, color):
        extra_slug = slugify_part(extra)
        if extra_slug and f"-{extra_slug}-" not in f"-{title_slug}-":
            parts.append(extra_slug)
    handle = "-".join(part for part in parts if part)
    return handle or "product"


def unique_handle(base_handle: str, handle_counts: dict[str, int]) -> str:
    count = handle_counts.get(base_handle, 0)
    handle_counts[base_handle] = count + 1
    if count == 0:
        return base_handle
    return f"{base_handle}-{count}"


def seo_title(title: Any, store_name: str = "Lavie World") -> Any:
    if is_blank(title):
        return None
    return f"Buy {str(title).strip()} Online - {store_name}"


def image_alt_text(title: Any) -> Any:
    """Image alt text is exactly the product title (both stores)."""
    if is_blank(title):
        return None
    return str(title).strip()


def build_lookup(df: pd.DataFrame, key_alias: str | Iterable[str]) -> dict[str, pd.Series]:
    key_col = resolve_column(df.columns, key_alias)
    if key_col is None:
        return {}
    lookup: dict[str, pd.Series] = {}
    for _, row in df.iterrows():
        key = normalize_sku(row.get(key_col)).upper()
        if key and key not in lookup:
            lookup[key] = row
    return lookup


BARCODE_COLUMNS = ["Variant Barcode", "Variant Barcodes"]
MPN_COLUMN = "Google Shopping / MPN"
BADGE_COLUMN = "Badges (product.metafields.custom.badges)"
BADGE_VALUE = "New"

METAFIELD_MAPPINGS = {
    "Closure Type (product.metafields.custom.closure_type)": "Closure type",
    "Compartments (product.metafields.custom.compartments)": "No. of Compartments",
    "External Pockets (product.metafields.custom.external_pockets)": "External Pockets",
    "Internal Pockets (product.metafields.custom.internal_pockets)": "Internal Pockets",
    "Laptop Compartment (product.metafields.custom.laptop_compartment)": "Laptop Compartment",
    "Lining Type (product.metafields.custom.lining_type)": "Lining Type",
    "Material (product.metafields.custom.material)": "Material",
    "Occasion (product.metafields.custom.occasion)": "Occasion",
    "Pattern (product.metafields.custom.pattern)": "Pattern",
    "Strap Type (product.metafields.custom.strap_type)": "Strap Type",
    "Water Resistant (product.metafields.custom.water_resistant)": "Water Resistant",
    "Care Instruction (product.metafields.my_fields.care_instruction)": "Care Instruction",
    "product deacription (product.metafields.my_fields.product_description)": "Product Description",
}


def set_first_present(
    df: pd.DataFrame,
    index: Any,
    targets: list[str],
    value: Any,
    warnings: set[str],
) -> None:
    for target in targets:
        col = resolve_column(df.columns, target)
        if col is not None:
            if not is_blank(value):
                df.at[index, col] = value
            return
    warnings.add(f"Target column missing in Product Listing: {' / '.join(targets)}")


def build_row_context(
    sku: str,
    cm_data: pd.Series | None,
    gs1_data: pd.Series | None,
    store: StoreConfig,
) -> dict[str, Any]:
    def cm(aliases: str | Iterable[str]) -> Any:
        return get_value(cm_data, aliases) if cm_data is not None else None

    def gs1(aliases: str | Iterable[str]) -> Any:
        return get_value(gs1_data, aliases) if gs1_data is not None else None

    size = normalize_size(gs1("Size")) or normalize_size(cm("Size"))
    gs_color_raw = gs1(["Colour", "Color"])
    cm_color_raw = cm(["Colour", "Color"])
    color = normalize_color(gs_color_raw) or normalize_color(cm_color_raw)
    raw_subcategory = clean_text(cm("Subcategory"))
    product_type, type_method = normalize_product_type(raw_subcategory, cm("Product Category"))

    brand = clean_text(cm("Brand Name")) or clean_text(gs1(["BRAND", "Brand"]))
    if store.vendor_override:
        vendor = store.vendor_override
    else:
        vendor = brand.title() if brand else ""

    country = clean_text(gs1(["Country", "Country of Origin"]))
    return {
        "sku": sku,
        "variant_sku": clean_text(cm("BZ CODE")) or sku,
        "brand": brand,
        "vendor": vendor,
        "size": size,
        "color": color,
        "color_source": "GS" if normalize_color(gs_color_raw) else ("Content Master" if color else ""),
        "raw_color": clean_text(gs_color_raw) or clean_text(cm_color_raw),
        "product_type": product_type,
        "type_method": type_method,
        "raw_subcategory": raw_subcategory,
        "product_title": clean_text(cm("Product Title")) or clean_text(cm(["Final Product Title", "Title"])),
        "bz_description": clean_text(cm("BZ Description")) or clean_text(gs1("Article description in SAP")),
        "style_name": clean_text(cm("Product Style Name")),
        "family": clean_text(gs1("Family")) or clean_text(gs1("New Family")),
        "country": country.title() if country else "",
        "barcode": format_identifier(gs1(["EAN/UPC", "EAN Code", "EAN"])),
        "price": format_price(gs1("NEW MRP")),
        "dimension": format_dimension(gs1(["System Dimension", "Dimension"])),
        "cm": cm,
    }


def fallback_name(ctx: dict[str, Any]) -> str:
    return derive_style_name(
        product_title=ctx["product_title"],
        bz_description=ctx["bz_description"],
        style_name=ctx["style_name"] or ctx["family"],
        brand=ctx["brand"],
        color=ctx["color"],
        size=ctx["size"],
        product_type=ctx["product_type"],
        raw_color=ctx["raw_color"],
    )


def generate_titles(
    contexts: list[dict[str, Any]],
    store: StoreConfig,
    title_generator: Callable[[list[dict[str, Any]]], dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Fill ctx['name'], ctx['title'] and ctx['title_source'] for every context."""
    info: dict[str, Any] = {
        "strategy": store.title_strategy,
        "ai_enabled": bool(title_generator) and store.title_strategy == "ai",
        "styles": 0,
        "ai_titles": 0,
        "rule_titles": 0,
        "family_titles": 0,
        "model": None,
        "errors": [],
        "warnings": [],
    }
    if not contexts:
        return info

    if store.title_strategy == "family":
        for ctx in contexts:
            family = smart_title(ctx["family"])
            if family:
                ctx["name"], ctx["title_source"] = family, "Family (GS)"
                info["family_titles"] += 1
            else:
                ctx["name"], ctx["title_source"] = fallback_name(ctx), "Rule (no Family in GS)"
                info["rule_titles"] += 1
            ctx["title"] = compose_title(ctx["name"], ctx["size"], ctx["color"], ctx["product_type"])
        missing = [ctx["sku"] for ctx in contexts if ctx["title_source"].startswith("Rule")]
        if missing:
            info["warnings"].append(
                f"{len(missing)} SKU(s) have no Family in the GS sheet; their title name was built from the Content Master."
            )
        return info

    # LAVIE: one AI request item per style, so every colour of a style gets the same name.
    groups: dict[str, list[dict[str, Any]]] = {}
    for ctx in contexts:
        key = style_group_key(ctx["product_title"], ctx["bz_description"], ctx["style_name"], ctx["product_type"])
        groups.setdefault(key, []).append(ctx)
    info["styles"] = len(groups)

    items: list[dict[str, Any]] = []
    ids: dict[str, list[dict[str, Any]]] = {}
    for index, members in enumerate(groups.values(), start=1):
        first = members[0]
        style_id = f"s{index}"
        ids[style_id] = members
        items.append(
            {
                "id": style_id,
                "product_title": first["product_title"],
                "sap_description": first["bz_description"],
                "style_name": first["style_name"],
                "family": first["family"],
                "brand": first["brand"],
                "type": first["product_type"],
                "sizes": sorted({m["size"] for m in members if m["size"]}),
                "colours": sorted({m["color"] for m in members if m["color"]}),
            }
        )

    ai_names: dict[str, str] = {}
    if info["ai_enabled"]:
        try:
            ai_names = title_generator(items) or {}
        except Exception as exc:  # noqa: BLE001 - fall back to rule-based names
            info["errors"].append(str(exc))
        describe = getattr(title_generator, "describe", None)
        if callable(describe):
            details = describe()
            info["model"] = details.get("model")
            info["errors"].extend(details.get("errors", []))

    for style_id, members in ids.items():
        ai_name = clean_ai_name(ai_names.get(style_id), members[0]["brand"])
        for ctx in members:
            if ai_name:
                ctx["name"], ctx["title_source"] = ai_name, "AI"
                info["ai_titles"] += 1
            else:
                ctx["name"], ctx["title_source"] = fallback_name(ctx), "Rule"
                info["rule_titles"] += 1
            ctx["title"] = compose_title(ctx["name"], ctx["size"], ctx["color"], ctx["product_type"])

    if info["ai_enabled"] and info["rule_titles"]:
        info["warnings"].append(
            f"AI could not title {info['rule_titles']} SKU(s); rule-based Name + Size + Colour + Type titles were used for them."
        )
    if not info["ai_enabled"] and store.title_strategy == "ai":
        info["warnings"].append(
            "AI titles are off (no GEMINI_API_KEY or switched off), so rule-based Name + Size + Colour + Type titles were used."
        )
    info["errors"] = list(dict.fromkeys(info["errors"]))
    return info


def fill_product_data(
    content_master_df: pd.DataFrame,
    gs1_df: pd.DataFrame,
    product_list_df: pd.DataFrame,
    store: str | StoreConfig = "lavie",
    title_generator: Callable[[list[dict[str, Any]]], dict[str, str]] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    store_cfg = store if isinstance(store, StoreConfig) else get_store(store)
    cm_lookup = build_lookup(content_master_df, "BZ CODE")
    gs1_lookup = build_lookup(gs1_df, "Article")
    result_df = strip_columns(product_list_df).astype(object)
    sku_col = resolve_column(result_df.columns, "SKU")
    warnings: set[str] = set()
    handle_counts: dict[str, int] = {}

    stats: dict[str, Any] = {
        "store": store_cfg.key,
        "total_rows": len(result_df),
        "rows_with_sku": 0,
        "cm_matches": 0,
        "gs1_matches": 0,
        "no_matches": 0,
        "missing_cm_skus": [],
        "missing_gs1_skus": [],
        "missing_barcode_skus": [],
        "warnings": [],
    }

    if sku_col is None:
        raise ValueError("Product Listing must contain a SKU column.")
    if not cm_lookup:
        warnings.add("Content Master lookup is empty or missing BZ CODE.")
    if not gs1_lookup:
        warnings.add("GS lookup is empty or missing Article.")

    contexts: list[tuple[Any, dict[str, Any]]] = []
    for idx, row in result_df.iterrows():
        sku = normalize_sku(row.get(sku_col))
        if not sku:
            continue

        stats["rows_with_sku"] += 1
        cm_data = cm_lookup.get(sku.upper())
        gs1_data = gs1_lookup.get(sku.upper())

        if cm_data is not None:
            stats["cm_matches"] += 1
        else:
            stats["missing_cm_skus"].append(sku)
        if gs1_data is not None:
            stats["gs1_matches"] += 1
        else:
            stats["missing_gs1_skus"].append(sku)
        if cm_data is None and gs1_data is None:
            stats["no_matches"] += 1

        contexts.append((idx, build_row_context(sku, cm_data, gs1_data, store_cfg)))

    title_info = generate_titles([ctx for _, ctx in contexts], store_cfg, title_generator)

    type_rows: dict[tuple[str, str, str], int] = {}
    title_rows: list[dict[str, Any]] = []
    for idx, ctx in contexts:
        cm = ctx["cm"]
        title = ctx["title"]
        size = ctx["size"] or None
        color = ctx["color"] or None
        product_type = ctx["product_type"] or None
        barcode = ctx["barcode"]
        if not barcode:
            stats["missing_barcode_skus"].append(ctx["sku"])

        handle_prefix = store_cfg.handle_prefix or ctx["vendor"]
        handle_value = unique_handle(make_handle(handle_prefix, title, size, color), handle_counts)

        set_if_present(result_df, idx, "Title", title, warnings)
        set_if_present(result_df, idx, "Handle", handle_value, warnings)
        set_if_present(result_df, idx, "Body (HTML)", cm(["HTML Content", "HTML content"]), warnings)
        set_if_present(result_df, idx, "Vendor", ctx["vendor"] or None, warnings)
        set_if_present(result_df, idx, "Product Category", cm("Product Category"), warnings)
        set_if_present(result_df, idx, "Type", product_type, warnings)
        set_if_present(result_df, idx, "Tags", create_tags(color, size), warnings, allow_blank=True)
        set_if_present(result_df, idx, "Published", "TRUE", warnings, allow_blank=True)
        set_if_present(result_df, idx, "Option1 Name", "Color", warnings, allow_blank=True)
        set_if_present(result_df, idx, "Option1 Value", color, warnings)
        set_if_present(result_df, idx, "Option2 Name", "Size", warnings, allow_blank=True)
        set_if_present(result_df, idx, "Option2 Value", size, warnings)
        set_if_present(result_df, idx, "Variant SKU", ctx["variant_sku"], warnings)
        set_if_present(result_df, idx, "Variant Grams", 400, warnings, allow_blank=True)
        set_if_present(result_df, idx, "Variant Inventory Tracker", "shopify", warnings, allow_blank=True)
        set_if_present(result_df, idx, "Variant Inventory Policy", "deny", warnings, allow_blank=True)
        set_if_present(result_df, idx, "Variant Fulfillment Service", "manual", warnings, allow_blank=True)
        set_if_present(result_df, idx, "Variant Price", ctx["price"], warnings)
        set_if_present(result_df, idx, "Variant Compare At Price", ctx["price"], warnings)
        set_if_present(result_df, idx, "Variant Requires Shipping", "TRUE", warnings, allow_blank=True)
        set_if_present(result_df, idx, "Variant Taxable", "FALSE", warnings, allow_blank=True)
        set_first_present(result_df, idx, BARCODE_COLUMNS, barcode, warnings)
        set_if_present(result_df, idx, "Gift Card", "FALSE", warnings, allow_blank=True)
        set_if_present(result_df, idx, "SEO Title", seo_title(title, store_cfg.seo_suffix), warnings)
        set_if_present(result_df, idx, "SEO Description", cm("Product Description"), warnings)
        set_if_present(result_df, idx, "Google Shopping / Gender", cm("Target Gender"), warnings)
        set_if_present(result_df, idx, MPN_COLUMN, barcode, warnings)
        set_if_present(result_df, idx, "Image Alt Text", image_alt_text(title), warnings)
        set_if_present(result_df, idx, BADGE_COLUMN, BADGE_VALUE, warnings, allow_blank=True)

        for target_col, source_col in METAFIELD_MAPPINGS.items():
            set_if_present(result_df, idx, target_col, cm(source_col), warnings)
        set_if_present(
            result_df,
            idx,
            "product category (product.metafields.custom.product_category)",
            product_type,
            warnings,
        )

        set_if_present(result_df, idx, "Quantity (product.metafields.custom.product_qty)", "1N", warnings, allow_blank=True)
        set_if_present(result_df, idx, "Size (product.metafields.custom.size)", size, warnings)
        set_if_present(
            result_df,
            idx,
            "Country of origin (product.metafields.my_fields.country_of_origin)",
            ctx["country"] or None,
            warnings,
        )
        set_if_present(
            result_df,
            idx,
            "Manufacturer Details (product.metafields.my_fields.manufacturer_details)",
            get_manufacturer_details(ctx["country"]),
            warnings,
        )
        set_if_present(
            result_df,
            idx,
            "Dimensions (product.metafields.my_fields.specifications)",
            ctx["dimension"],
            warnings,
        )
        for column_name in UNMAPPED_FINAL_COLUMNS:
            clear_if_present(result_df, idx, column_name)
        set_if_present(result_df, idx, "Variant Weight Unit", "kg", warnings, allow_blank=True)
        set_if_present(result_df, idx, "Status", "draft", warnings, allow_blank=True)

        type_key = (ctx["raw_subcategory"] or "(blank)", ctx["product_type"] or "(blank)", ctx["type_method"])
        type_rows[type_key] = type_rows.get(type_key, 0) + 1
        title_rows.append(
            {
                "SKU": ctx["sku"],
                "Title": title,
                "Title Source": ctx["title_source"],
                "Name Part": ctx["name"],
                "Size (GS)": ctx["size"],
                "Colour": ctx["color"],
                "Colour Source": ctx["color_source"],
                "Type": ctx["product_type"],
                "Content Subcategory": ctx["raw_subcategory"],
                "Barcode (EAN/UPC)": barcode or "",
                "Content Product Title": ctx["product_title"],
            }
        )

    unmapped = sorted({raw for (raw, _, method) in type_rows if method == "unmapped"})
    if unmapped:
        warnings.add(
            "Subcategory value(s) not in the Type map, used as written: " + ", ".join(unmapped[:8])
        )
    missing_type = [ctx["sku"] for _, ctx in contexts if not ctx["product_type"]]
    if missing_type:
        warnings.add(f"{len(missing_type)} SKU(s) have no Subcategory in the Content Master, so Type is blank.")
    if stats["missing_barcode_skus"]:
        warnings.add(f"{len(stats['missing_barcode_skus'])} SKU(s) have no EAN/UPC in the GS sheet.")

    stats["missing_cm_skus"] = unique_preserve(stats["missing_cm_skus"])
    stats["missing_gs1_skus"] = unique_preserve(stats["missing_gs1_skus"])
    stats["missing_barcode_skus"] = unique_preserve(stats["missing_barcode_skus"])
    stats["title_info"] = title_info
    stats["title_rows"] = title_rows
    stats["type_rows"] = [
        {"Content Subcategory": raw, "Shopify Type": mapped, "Match": method, "SKUs": count}
        for (raw, mapped, method), count in sorted(type_rows.items())
    ]
    warnings.update(title_info.get("warnings", []))
    stats["warnings"] = sorted(warnings)
    return result_df, stats


def normalize_dropbox_url(url: Any) -> str:
    text = str(url).strip()
    if not text:
        return text
    parsed = urlparse(text)
    host = parsed.netloc.lower()
    if "dropbox.com" not in host:
        return text
    if host.startswith("dl.dropboxusercontent.com"):
        return text

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["dl"] = "1"
    parsed = parsed._replace(netloc="dl.dropboxusercontent.com", query=urlencode(query))
    return urlunparse(parsed)


def read_dropbox_sheet(path: str | Path, sheet_name: str) -> pd.DataFrame:
    if Path(path).suffix.lower() == ".csv":
        return pd.read_csv(path, header=None, dtype=str, encoding="utf-8-sig")
    return read_excel_file(path, sheet_name=sheet_name, header=None)


def build_dropbox_map(dropbox_df: pd.DataFrame) -> dict[str, list[str]]:
    sku_map: dict[str, list[str]] = {}
    for _, row in dropbox_df.iterrows():
        sku = normalize_sku(row.iloc[0] if len(row) else "")
        if not sku or normalize_name(sku) in {"sku", "article", "bz code"}:
            continue
        images = [
            normalize_dropbox_url(value)
            for value in row.iloc[1:].tolist()
            if not is_blank(value)
        ]
        if images:
            sku_map[sku.upper()] = images
    return sku_map


def product_skus(product_df: pd.DataFrame) -> list[str]:
    sku_col = resolve_column(product_df.columns, "SKU")
    if sku_col is None:
        return []
    return unique_preserve(product_df[sku_col].tolist())


def select_dropbox_sheet(
    path: str | Path,
    product_df: pd.DataFrame,
    requested_sheet: str | None = None,
) -> tuple[str, pd.DataFrame, dict[str, list[str]], dict[str, int]]:
    if Path(path).suffix.lower() == ".csv":
        candidate_sheets = ["CSV"]
    else:
        xls = get_excel_file(path)
        candidate_sheets = [requested_sheet] if requested_sheet else xls.sheet_names
    product_sku_set = {sku.upper() for sku in product_skus(product_df)}

    best_sheet = candidate_sheets[0]
    best_df = read_dropbox_sheet(path, best_sheet)
    best_map = build_dropbox_map(best_df)
    best_overlap = len(product_sku_set.intersection(best_map))
    scores = {best_sheet: best_overlap}

    for sheet in candidate_sheets[1:]:
        df = read_dropbox_sheet(path, sheet)
        sku_map = build_dropbox_map(df)
        overlap = len(product_sku_set.intersection(sku_map))
        scores[sheet] = overlap
        if overlap > best_overlap:
            best_sheet = sheet
            best_df = df
            best_map = sku_map
            best_overlap = overlap
    return best_sheet, best_df, best_map, scores


def map_product_images(
    product_df: pd.DataFrame,
    dropbox_path: str | Path | None,
    requested_sheet: str | None = None,
    minimum_image_slots: int = 0,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    product_df = strip_columns(product_df).astype(object)
    sku_col = resolve_column(product_df.columns, "SKU")
    if sku_col is None:
        raise ValueError("Product Listing must contain a SKU column before image mapping.")

    if dropbox_path is None:
        # Dropbox sheet is optional: keep rows as they are, no image links are added.
        return product_df, {
            "skipped": True,
            "selected_sheet": None,
            "sheet_scores": {},
            "dropbox_rows": 0,
            "dropbox_skus": 0,
            "original_rows": len(product_df),
            "result_rows": len(product_df),
            "blank_sku_rows_preserved": int(product_df[sku_col].map(lambda v: not normalize_sku(v)).sum()),
            "skus_with_images": 0,
            "image_urls_written": 0,
            "missing_image_skus": [],
            "warnings": [],
        }

    selected_sheet, dropbox_df, sku_map, sheet_scores = select_dropbox_sheet(
        dropbox_path,
        product_df,
        requested_sheet,
    )
    all_columns = product_df.columns.tolist()
    handle_col = resolve_column(all_columns, "Handle")
    image_alt_col = resolve_column(all_columns, "Image Alt Text")
    image_src_col = resolve_column(all_columns, "Image Src")
    image_pos_col = resolve_column(all_columns, "Image Position")
    variant_image_col = resolve_column(all_columns, "Variant Image")

    warnings: list[str] = []
    if image_src_col is None:
        warnings.append("Product Listing does not contain Image Src; URLs could not be written.")
    if image_pos_col is None:
        warnings.append("Product Listing does not contain Image Position; positions could not be written.")

    result_rows: list[dict[str, Any]] = []
    missing_image_skus: list[str] = []
    blank_sku_rows = 0
    skus_with_images = 0
    image_urls_written = 0

    for _, row in product_df.iterrows():
        original_row = row.to_dict()
        sku_value = normalize_sku(original_row.get(sku_col))
        if not sku_value:
            blank_sku_rows += 1
            result_rows.append(original_row)
            continue

        handle_value = original_row.get(handle_col, "") if handle_col else ""
        image_alt_value = original_row.get(image_alt_col, "") if image_alt_col else ""
        images = sku_map.get(sku_value.upper(), [])

        if images:
            skus_with_images += 1
            if image_src_col:
                original_row[image_src_col] = images[0]
            if variant_image_col:
                original_row[variant_image_col] = images[0]
            if image_pos_col:
                original_row[image_pos_col] = "1"
            image_urls_written += 1
            result_rows.append(original_row)

            for image_position, image_url in enumerate(images[1:], start=2):
                new_row = {col: "" for col in all_columns}
                new_row[sku_col] = sku_value
                if handle_col:
                    new_row[handle_col] = handle_value
                if image_alt_col:
                    new_row[image_alt_col] = image_alt_value
                if image_src_col:
                    new_row[image_src_col] = image_url
                if variant_image_col:
                    new_row[variant_image_col] = ""
                if image_pos_col:
                    new_row[image_pos_col] = str(image_position)
                image_urls_written += 1
                result_rows.append(new_row)

            blank_rows_count = max(0, minimum_image_slots - len(images))
        else:
            missing_image_skus.append(sku_value)
            if image_src_col:
                original_row[image_src_col] = ""
            if variant_image_col:
                original_row[variant_image_col] = ""
            if image_pos_col:
                original_row[image_pos_col] = ""
            result_rows.append(original_row)
            blank_rows_count = minimum_image_slots

        for _ in range(blank_rows_count):
            blank_row = {col: "" for col in all_columns}
            blank_row[sku_col] = sku_value
            if handle_col:
                blank_row[handle_col] = handle_value
            if image_alt_col:
                blank_row[image_alt_col] = image_alt_value
            if variant_image_col:
                blank_row[variant_image_col] = ""
            result_rows.append(blank_row)

    result_df = pd.DataFrame(result_rows, columns=all_columns, dtype=object)
    stats = {
        "skipped": False,
        "selected_sheet": selected_sheet,
        "sheet_scores": sheet_scores,
        "dropbox_rows": len(dropbox_df),
        "dropbox_skus": len(sku_map),
        "original_rows": len(product_df),
        "result_rows": len(result_df),
        "blank_sku_rows_preserved": blank_sku_rows,
        "skus_with_images": skus_with_images,
        "image_urls_written": image_urls_written,
        "missing_image_skus": unique_preserve(missing_image_skus),
        "warnings": warnings,
    }
    return result_df, stats


def safe_excel_df(df: pd.DataFrame) -> pd.DataFrame:
    def trim(value: Any) -> Any:
        if isinstance(value, str) and len(value) > 32767:
            return value[:32767]
        return value

    return df.map(trim)


def numeric_identifier_columns(df: pd.DataFrame) -> list[str]:
    columns = []
    for target in [*BARCODE_COLUMNS, MPN_COLUMN]:
        col = resolve_column(df.columns, target)
        if col is not None and col not in columns:
            columns.append(col)
    return columns


def write_shopify_excel(df: pd.DataFrame, path: str | Path) -> None:
    """Excel backup with EAN/UPC and MPN stored as real numbers shown in full (no 8.9E+12)."""
    excel_df = safe_excel_df(df)
    id_columns = numeric_identifier_columns(excel_df)

    def to_number(value: Any) -> Any:
        text = format_identifier(value)
        if text and re.fullmatch(r"\d{1,15}", text):
            return int(text)
        return value

    for col in id_columns:
        excel_df[col] = excel_df[col].map(to_number).astype(object)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        excel_df.to_excel(writer, index=False, sheet_name="Shopify Import")
        if excel_df.empty:
            return
        sheet = writer.sheets["Shopify Import"]
        for col in id_columns:
            col_idx = excel_df.columns.get_loc(col) + 1
            for (cell,) in sheet.iter_rows(min_row=2, min_col=col_idx, max_col=col_idx):
                if isinstance(cell.value, int):
                    cell.number_format = "0"


def finalize_identifiers(df: pd.DataFrame) -> pd.DataFrame:
    """Make sure barcode / MPN cells are plain digit strings in the CSV."""
    df = df.copy()
    for col in numeric_identifier_columns(df):
        df[col] = df[col].map(lambda value: format_identifier(value) if not is_blank(value) else value)
    return df


def build_report_frames(
    content_info: dict[str, Any],
    gs1_info: dict[str, Any],
    product_info: dict[str, Any],
    content_transform_info: dict[str, Any],
    fill_stats: dict[str, Any],
    image_stats: dict[str, Any],
) -> dict[str, pd.DataFrame]:
    warnings = []
    warnings.extend(content_info.get("warnings", []))
    warnings.extend(gs1_info.get("warnings", []))
    warnings.extend(product_info.get("warnings", []))
    warnings.extend(content_transform_info.get("warnings", []))
    warnings.extend(fill_stats.get("warnings", []))
    warnings.extend(image_stats.get("warnings", []))

    title_info = fill_stats.get("title_info", {})
    summary_rows = [
        ("Store", fill_stats.get("store")),
        ("Content sheet", content_info.get("sheet_name")),
        ("GS sheet", gs1_info.get("sheet_name")),
        ("GS header row", gs1_info.get("header_row")),
        ("Dropbox sheet", image_stats.get("selected_sheet") or "Not uploaded"),
        ("Content rows", content_info.get("rows")),
        ("Product rows uploaded", fill_stats.get("total_rows")),
        ("Rows with SKU", fill_stats.get("rows_with_sku")),
        ("Content Master matches", fill_stats.get("cm_matches")),
        ("GS matches", fill_stats.get("gs1_matches")),
        ("No CM/GS match", fill_stats.get("no_matches")),
        ("SKUs without EAN/UPC", len(fill_stats.get("missing_barcode_skus", []))),
        ("AI titles", title_info.get("ai_titles", 0)),
        ("Family titles", title_info.get("family_titles", 0)),
        ("Rule-based titles", title_info.get("rule_titles", 0)),
        ("AI model", title_info.get("model") or "-"),
        ("SKUs with images", image_stats.get("skus_with_images")),
        ("Missing image SKUs", len(image_stats.get("missing_image_skus", []))),
        ("Final rows", image_stats.get("result_rows")),
        ("Image URLs written", image_stats.get("image_urls_written")),
    ]
    checklist = [
        ("EAN / UPC barcodes", "Variant Barcode and Google Shopping / MPN are the GS EAN/UPC as plain numbers."),
        ("Titles", "Lavie: AI Name + Size + Colour + Type. Akiki: Family + Size + Colour + Type."),
        ("Types", "Type comes from the Content Master Subcategory, cleaned to the store's Type list."),
        ("Tags", "colour, nano, new-launch, size. Add store-specific tags if needed."),
        ("Image alt text", "Same as the title on every image row."),
        ("Badges", "Set to New for every new listing."),
    ]
    frames = {
        "Summary": pd.DataFrame(summary_rows, columns=["Metric", "Value"]),
        "Cross Check": pd.DataFrame(checklist, columns=["Please cross-check", "What the tool did"]),
        "Titles": pd.DataFrame(fill_stats.get("title_rows", [])),
        "Type Mapping": pd.DataFrame(fill_stats.get("type_rows", [])),
        "Missing Content Master": pd.DataFrame({"SKU": fill_stats.get("missing_cm_skus", [])}),
        "Missing GS": pd.DataFrame({"SKU": fill_stats.get("missing_gs1_skus", [])}),
        "Missing EAN": pd.DataFrame({"SKU": fill_stats.get("missing_barcode_skus", [])}),
        "Missing Images": pd.DataFrame({"SKU": image_stats.get("missing_image_skus", [])}),
        "Warnings": pd.DataFrame({"Warning": unique_preserve(warnings + title_info.get("errors", []))}),
        "Dropbox Sheet Scores": pd.DataFrame(
            [
                {"Sheet": sheet, "SKU Overlap": score}
                for sheet, score in image_stats.get("sheet_scores", {}).items()
            ]
        ),
    }
    return frames


def write_report(path: str | Path, frames: dict[str, pd.DataFrame]) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for sheet_name, frame in frames.items():
            frame.to_excel(writer, index=False, sheet_name=sheet_name[:31])


def run_pipeline(
    content_master_path: str | Path,
    gs1_path: str | Path,
    dropbox_path: str | Path | None,
    product_list_path: str | Path,
    output_dir: str | Path,
    content_sheet: str | None = None,
    gs1_sheet: str | None = None,
    dropbox_sheet: str | None = None,
    store: str = "lavie",
    title_generator: Callable[[list[dict[str, Any]]], dict[str, str]] | None = None,
) -> dict[str, Any]:
    store_cfg = get_store(store)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    content_df, content_load_info = load_content_master(content_master_path, content_sheet)
    transformed_content_df, content_transform_info = transform_content_master(content_df)
    gs1_df, gs1_load_info = load_gs1(gs1_path, gs1_sheet)
    product_df, product_load_info = load_product_list(product_list_path)
    filled_df, fill_stats = fill_product_data(
        transformed_content_df,
        gs1_df,
        product_df,
        store=store_cfg,
        title_generator=title_generator,
    )
    final_df, image_stats = map_product_images(filled_df, dropbox_path, dropbox_sheet)
    final_df = finalize_identifiers(final_df)

    prefix = f"{store_cfg.key}_"
    csv_path = output_dir / f"{prefix}shopify_import_ready.csv"
    excel_path = output_dir / f"{prefix}shopify_import_ready.xlsx"
    report_path = output_dir / f"{prefix}processing_report.xlsx"

    final_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    write_shopify_excel(final_df, excel_path)

    report_frames = build_report_frames(
        content_load_info.as_dict(),
        gs1_load_info.as_dict(),
        product_load_info.as_dict(),
        content_transform_info,
        fill_stats,
        image_stats,
    )
    write_report(report_path, report_frames)

    return {
        "store": store_cfg,
        "final_df": final_df,
        "content_info": content_load_info.as_dict(),
        "gs1_info": gs1_load_info.as_dict(),
        "product_info": product_load_info.as_dict(),
        "content_transform_info": content_transform_info,
        "fill_stats": fill_stats,
        "image_stats": image_stats,
        "report_frames": report_frames,
        "downloads": {
            "csv": csv_path.name,
            "excel": excel_path.name,
            "report": report_path.name,
        },
    }
