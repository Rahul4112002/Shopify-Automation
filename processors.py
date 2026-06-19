from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import pandas as pd


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
    score += 2 if "size" in cols or "size " in cols else 0
    score += 2 if "country" in cols else 0
    score -= sum(1 for col in cols if col.startswith("unnamed"))
    return score


def detect_gs1_sheet_and_header(
    path: str | Path,
    requested_sheet: str | None = None,
) -> tuple[str, int]:
    xls = get_excel_file(path)
    if requested_sheet:
        candidate_sheets = [requested_sheet]
    else:
        preferred = [
            sheet
            for sheet in xls.sheet_names
            if normalize_name(sheet) in {"master", "master sheet"}
        ]
        contains_master = [
            sheet
            for sheet in xls.sheet_names
            if "master" in normalize_name(sheet) and sheet not in preferred
        ]
        others = [
            sheet
            for sheet in xls.sheet_names
            if sheet not in preferred and sheet not in contains_master
        ]
        candidate_sheets = preferred + contains_master + others

    best_sheet = candidate_sheets[0]
    best_header = 0
    best_score = -999
    for sheet in candidate_sheets:
        for header_row in range(4):
            try:
                sample = read_excel_file(path, sheet_name=sheet, header=header_row, nrows=5)
                score = score_sheet_columns(sample.columns)
                if score > best_score:
                    best_sheet = sheet
                    best_header = header_row
                    best_score = score
            except Exception:
                continue
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
        info.warnings.append("GS1 file does not contain an Article column after auto-detection.")
    return df, info


def format_size(size_value: Any) -> Any:
    if is_blank(size_value):
        return None
    size_text = str(size_value).strip().title()
    if size_text.lower() == "x-large":
        return "Extra Large"
    return size_text


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
    if is_blank(value):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return format(value, "f").rstrip("0").rstrip(".")

    text = str(value).strip().replace(",", "")
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


def slugify_part(value: Any) -> str:
    if is_blank(value):
        return ""
    text = str(value).strip().lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def make_handle(vendor: Any, title: Any, size: Any, color: Any) -> str:
    parts = [slugify_part(part) for part in [vendor, title, size, color]]
    handle = "-".join(part for part in parts if part)
    return handle or "product"


def unique_handle(base_handle: str, handle_counts: dict[str, int]) -> str:
    count = handle_counts.get(base_handle, 0)
    handle_counts[base_handle] = count + 1
    if count == 0:
        return base_handle
    return f"{base_handle}-{count}"


def seo_title(title: Any) -> Any:
    if is_blank(title):
        return None
    return f"Buy {str(title).strip()} Online - Lavie World"


def image_alt_text(title: Any, color: Any) -> Any:
    if is_blank(title):
        return None
    if is_blank(color):
        return str(title).strip()
    return f"{str(title).strip()} {str(color).strip().title()}"


def build_lookup(df: pd.DataFrame, key_alias: str) -> dict[str, pd.Series]:
    key_col = resolve_column(df.columns, key_alias)
    if key_col is None:
        return {}
    lookup: dict[str, pd.Series] = {}
    for _, row in df.iterrows():
        key = normalize_sku(row.get(key_col))
        if key and key not in lookup:
            lookup[key] = row
    return lookup


def fill_product_data(
    content_master_df: pd.DataFrame,
    gs1_df: pd.DataFrame,
    product_list_df: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    cm_lookup = build_lookup(content_master_df, "BZ CODE")
    gs1_lookup = build_lookup(gs1_df, "Article")
    result_df = strip_columns(product_list_df).astype(object)
    sku_col = resolve_column(result_df.columns, "SKU")
    warnings: set[str] = set()
    handle_counts: dict[str, int] = {}

    stats: dict[str, Any] = {
        "total_rows": len(result_df),
        "rows_with_sku": 0,
        "cm_matches": 0,
        "gs1_matches": 0,
        "no_matches": 0,
        "missing_cm_skus": [],
        "missing_gs1_skus": [],
        "warnings": [],
    }

    if sku_col is None:
        raise ValueError("Product Listing must contain a SKU column.")
    if not cm_lookup:
        warnings.add("Content Master lookup is empty or missing BZ CODE.")
    if not gs1_lookup:
        warnings.add("GS1 lookup is empty or missing Article.")

    for idx, row in result_df.iterrows():
        sku = normalize_sku(row.get(sku_col))
        if not sku:
            continue

        stats["rows_with_sku"] += 1
        cm_data = cm_lookup.get(sku)
        gs1_data = gs1_lookup.get(sku)

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

        def cm(aliases: str | Iterable[str]) -> Any:
            return get_value(cm_data, aliases) if cm_data is not None else None

        def gs1(aliases: str | Iterable[str]) -> Any:
            return get_value(gs1_data, aliases) if gs1_data is not None else None

        gs1_size = format_size(gs1("Size"))
        cm_size = format_size(cm("Size"))
        final_size = gs1_size or cm_size

        cm_color = cm(["Colour", "Color"])
        if not is_blank(cm_color):
            cm_color = str(cm_color).strip()

        gs1_country = gs1("Country")
        if not is_blank(gs1_country):
            gs1_country = str(gs1_country).strip().title()

        vendor_val = cm("Brand Name")
        if not is_blank(vendor_val):
            vendor_val = str(vendor_val).strip().title()

        clean_final_title = remove_vendor_from_title(
            cm(["Final Product Title", "Title"]),
            vendor_val,
        )
        barcode_value = format_identifier(gs1(["EAN/UPC", "EAN Code"]))
        handle_value = unique_handle(
            make_handle(vendor_val, clean_final_title, final_size, cm_color),
            handle_counts,
        )

        set_if_present(result_df, idx, "Title", clean_final_title, warnings)
        set_if_present(result_df, idx, "Handle", handle_value, warnings)
        set_if_present(result_df, idx, "Body (HTML)", cm(["HTML Content", "HTML content"]), warnings)
        set_if_present(result_df, idx, "Vendor", vendor_val, warnings)
        set_if_present(result_df, idx, "Product Category", cm("Product Category"), warnings)
        set_if_present(result_df, idx, "Type", cm("Subcategory"), warnings)
        set_if_present(result_df, idx, "Tags", create_tags(cm_color, final_size), warnings, allow_blank=True)
        set_if_present(result_df, idx, "Published", "TRUE", warnings, allow_blank=True)
        set_if_present(result_df, idx, "Option1 Name", "Color", warnings, allow_blank=True)
        set_if_present(
            result_df,
            idx,
            "Option1 Value",
            str(cm_color).title() if not is_blank(cm_color) else None,
            warnings,
        )
        set_if_present(result_df, idx, "Option2 Name", "Size", warnings, allow_blank=True)
        set_if_present(result_df, idx, "Option2 Value", final_size, warnings)
        set_if_present(result_df, idx, "Variant SKU", cm("BZ CODE") or sku, warnings)
        set_if_present(result_df, idx, "Variant Grams", 400, warnings, allow_blank=True)
        set_if_present(result_df, idx, "Variant Inventory Tracker", "shopify", warnings, allow_blank=True)
        set_if_present(result_df, idx, "Variant Inventory Policy", "deny", warnings, allow_blank=True)
        set_if_present(result_df, idx, "Variant Fulfillment Service", "manual", warnings, allow_blank=True)
        new_mrp = gs1("NEW MRP")
        set_if_present(result_df, idx, "Variant Price", new_mrp, warnings)
        set_if_present(result_df, idx, "Variant Compare At Price", new_mrp, warnings)
        set_if_present(result_df, idx, "Variant Requires Shipping", "TRUE", warnings, allow_blank=True)
        set_if_present(result_df, idx, "Variant Taxable", "FALSE", warnings, allow_blank=True)
        set_if_present(result_df, idx, "Variant Barcode", barcode_value, warnings)
        set_if_present(result_df, idx, "Gift Card", "FALSE", warnings, allow_blank=True)
        set_if_present(result_df, idx, "SEO Title", seo_title(clean_final_title), warnings)
        set_if_present(result_df, idx, "SEO Description", cm("Product Description"), warnings)
        set_if_present(result_df, idx, "Google Shopping / Gender", cm("Target Gender"), warnings)
        set_if_present(result_df, idx, "Google Shopping / MPN", barcode_value, warnings)
        set_if_present(result_df, idx, "Image Alt Text", image_alt_text(clean_final_title, cm_color), warnings)

        metafield_mappings = {
            "Closure Type (product.metafields.custom.closure_type)": "Closure type",
            "Compartments (product.metafields.custom.compartments)": "No. of Compartments",
            "External Pockets (product.metafields.custom.external_pockets)": "External Pockets",
            "Internal Pockets (product.metafields.custom.internal_pockets)": "Internal Pockets",
            "Laptop Compartment (product.metafields.custom.laptop_compartment)": "Laptop Compartment",
            "Lining Type (product.metafields.custom.lining_type)": "Lining Type",
            "Material (product.metafields.custom.material)": "Material",
            "Occasion (product.metafields.custom.occasion)": "Occasion",
            "Pattern (product.metafields.custom.pattern)": "Pattern",
            "product category (product.metafields.custom.product_category)": "Subcategory",
            "Strap Type (product.metafields.custom.strap_type)": "Strap Type",
            "Water Resistant (product.metafields.custom.water_resistant)": "Water Resistant",
            "Care Instruction (product.metafields.my_fields.care_instruction)": "Care Instruction",
            "product deacription (product.metafields.my_fields.product_description)": "Product Description",
        }
        for target_col, source_col in metafield_mappings.items():
            set_if_present(result_df, idx, target_col, cm(source_col), warnings)

        set_if_present(result_df, idx, "Quantity (product.metafields.custom.product_qty)", "1N", warnings, allow_blank=True)
        set_if_present(result_df, idx, "Size (product.metafields.custom.size)", final_size, warnings)
        set_if_present(result_df, idx, "Country of origin (product.metafields.my_fields.country_of_origin)", gs1_country, warnings)
        set_if_present(
            result_df,
            idx,
            "Manufacturer Details (product.metafields.my_fields.manufacturer_details)",
            get_manufacturer_details(gs1_country),
            warnings,
        )
        set_if_present(
            result_df,
            idx,
            "Dimensions (product.metafields.my_fields.specifications)",
            format_dimension(gs1(["Dimension", "System Dimension"])),
            warnings,
        )
        for column_name in UNMAPPED_FINAL_COLUMNS:
            clear_if_present(result_df, idx, column_name)
        set_if_present(result_df, idx, "Variant Weight Unit", "kg", warnings, allow_blank=True)
        set_if_present(result_df, idx, "Status", "draft", warnings, allow_blank=True)

    stats["missing_cm_skus"] = unique_preserve(stats["missing_cm_skus"])
    stats["missing_gs1_skus"] = unique_preserve(stats["missing_gs1_skus"])
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
            sku_map[sku] = images
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
    xls = get_excel_file(path)
    candidate_sheets = [requested_sheet] if requested_sheet else xls.sheet_names
    product_sku_set = set(product_skus(product_df))

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
    dropbox_path: str | Path,
    requested_sheet: str | None = None,
    minimum_image_slots: int = 0,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    product_df = strip_columns(product_df).astype(object)
    sku_col = resolve_column(product_df.columns, "SKU")
    if sku_col is None:
        raise ValueError("Product Listing must contain a SKU column before image mapping.")

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
        images = sku_map.get(sku_value, [])

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

    result_df = pd.DataFrame(result_rows, columns=all_columns)
    stats = {
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

    summary_rows = [
        ("Content sheet", content_info.get("sheet_name")),
        ("GS1 sheet", gs1_info.get("sheet_name")),
        ("GS1 header row", gs1_info.get("header_row")),
        ("Dropbox sheet", image_stats.get("selected_sheet")),
        ("Content rows", content_info.get("rows")),
        ("Product rows uploaded", fill_stats.get("total_rows")),
        ("Rows with SKU", fill_stats.get("rows_with_sku")),
        ("Content Master matches", fill_stats.get("cm_matches")),
        ("GS1 matches", fill_stats.get("gs1_matches")),
        ("No CM/GS1 match", fill_stats.get("no_matches")),
        ("SKUs with images", image_stats.get("skus_with_images")),
        ("Missing image SKUs", len(image_stats.get("missing_image_skus", []))),
        ("Final rows", image_stats.get("result_rows")),
        ("Image URLs written", image_stats.get("image_urls_written")),
    ]
    frames = {
        "Summary": pd.DataFrame(summary_rows, columns=["Metric", "Value"]),
        "Missing Content Master": pd.DataFrame(
            {"SKU": fill_stats.get("missing_cm_skus", [])}
        ),
        "Missing GS1": pd.DataFrame({"SKU": fill_stats.get("missing_gs1_skus", [])}),
        "Missing Images": pd.DataFrame({"SKU": image_stats.get("missing_image_skus", [])}),
        "Warnings": pd.DataFrame({"Warning": unique_preserve(warnings)}),
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
    dropbox_path: str | Path,
    product_list_path: str | Path,
    output_dir: str | Path,
    content_sheet: str | None = None,
    gs1_sheet: str | None = None,
    dropbox_sheet: str | None = None,
) -> dict[str, Any]:
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
    )
    final_df, image_stats = map_product_images(filled_df, dropbox_path, dropbox_sheet)

    csv_path = output_dir / "shopify_import_ready.csv"
    excel_path = output_dir / "shopify_import_ready.xlsx"
    report_path = output_dir / "processing_report.xlsx"

    final_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    safe_excel_df(final_df).to_excel(excel_path, index=False, engine="openpyxl")

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
