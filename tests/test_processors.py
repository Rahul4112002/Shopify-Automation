from pathlib import Path

import pandas as pd

from processors import (
    fill_product_data,
    load_gs1,
    map_product_images,
    normalize_dropbox_url,
    transform_content_master,
)


def test_content_transform_inserts_generated_columns():
    df = pd.DataFrame(
        {
            "BZ CODE": ["SKU1"],
            "Product Title": ["Lavie Women's Sling - Black"],
            "Colour": ["p blue"],
            "Bullet Point 1": ["Roomy"],
            "Bullet Point 2": ["Light"],
            "Special Feature 1 ": ["Zip closure."],
            "Brand Name": ["lavie"],
        }
    )

    result, info = transform_content_master(df)

    assert result.loc[0, "Title"] == "Lavie Sling Bag Black"
    assert result.loc[0, "Colour"] == "Pale Blue"
    assert result.loc[0, "Bullet Points HTML"] == "<li>Roomy</li><li>Light</li>"
    assert result.loc[0, "HTML content"] == (
        "<ul><li>Roomy</li><li>Light</li>"
        "<li><b>Special Features : </b>Zip closure</li></ul>"
    )
    assert info["product_title_col"] == "Product Title"


def test_load_gs1_detects_shifted_header(tmp_path: Path):
    path = tmp_path / "gs1.xlsx"
    raw = pd.DataFrame(
        [
            ["ignore", "ignore", "ignore"],
            ["Article", "EAN/UPC", "NEW MRP"],
            ["SKU1", "123456", 999],
        ]
    )
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        raw.to_excel(writer, index=False, header=False, sheet_name="Master")

    df, info = load_gs1(path)

    assert info.header_row == 1
    assert info.sheet_name == "Master"
    assert list(df.columns) == ["Article", "EAN/UPC", "NEW MRP"]
    assert df.loc[0, "Article"] == "SKU1"


def test_product_fill_maps_content_and_gs1_fields():
    content = pd.DataFrame(
        {
            "BZ CODE": ["SKU1", "SKU2"],
            "Title": ["Lavie Sport Clean Bag", "Lavie Sport Clean Bag"],
            "HTML content": ["<ul><li>One</li></ul>", "<ul><li>Two</li></ul>"],
            "Brand Name": ["lavie sport", "lavie sport"],
            "Colour": ["Black", "Black"],
            "Product Category": ["Luggage", "Luggage"],
            "Subcategory": ["Backpack", "Backpack"],
            "Product Description": ["Good bag", "Good bag 2"],
            "Target Gender": ["Female", "Female"],
            "Closure type": ["Zip", "Zip"],
        }
    )
    gs1 = pd.DataFrame(
        {
            "Article": ["SKU1", "SKU2"],
            "Size": ["x-large", "x-large"],
            "Country": ["India", "India"],
            "NEW MRP": [1999, 1999],
            "OLD MRP": [2499, 2499],
            "EAN/UPC": ["8.900000000000E+12", "8900000000001.0"],
            "System Dimension": ["LARGE;(40L X 9W X 26H)cm", "LARGE;(40L X 9W X 26H)cm"],
        }
    )
    product = pd.DataFrame(
        {
            "SKU": ["SKU1", "SKU2"],
            "Handle": ["", ""],
            "Title": ["", ""],
            "Body (HTML)": ["", ""],
            "Vendor": ["", ""],
            "Tags": ["", ""],
            "Option2 Value": ["", ""],
            "Variant SKU": ["", ""],
            "Variant Price": ["", ""],
            "Variant Compare At Price": ["", ""],
            "Variant Barcode": ["", ""],
            "Google Shopping / MPN": ["", ""],
            "SEO Title": ["", ""],
            "Image Alt Text": ["", ""],
            "Status": ["", ""],
            "Color (product.metafields.shopify.color-pattern)": ["Should Clear", "Should Clear"],
            "Size (product.metafields.shopify.size)": ["Should Clear", "Should Clear"],
            "Target gender (product.metafields.shopify.target-gender)": ["Should Clear", "Should Clear"],
            "Dimensions (product.metafields.my_fields.specifications)": ["", ""],
            "Manufacturer Details (product.metafields.my_fields.manufacturer_details)": ["", ""],
        }
    )

    result, stats = fill_product_data(content, gs1, product)

    assert stats["cm_matches"] == 2
    assert stats["gs1_matches"] == 2
    assert result.loc[0, "Title"] == "Clean Bag"
    assert result.loc[1, "Title"] == "Clean Bag"
    assert result.loc[0, "Handle"] == "lavie-sport-clean-bag-extra-large-black"
    assert result.loc[1, "Handle"] == "lavie-sport-clean-bag-extra-large-black-1"
    assert result.loc[0, "Body (HTML)"] == "<ul><li>One</li></ul>"
    assert result.loc[0, "Vendor"] == "Lavie Sport"
    assert result.loc[0, "Tags"] == "black, nano, new-launch, extra large"
    assert result.loc[0, "Option2 Value"] == "Extra Large"
    assert result.loc[0, "Variant SKU"] == "SKU1"
    assert result.loc[0, "Variant Price"] == 1999
    assert result.loc[0, "Variant Compare At Price"] == 1999
    assert result.loc[0, "Variant Barcode"] == "8900000000000"
    assert result.loc[1, "Variant Barcode"] == "8900000000001"
    assert result.loc[0, "Google Shopping / MPN"] == "8900000000000"
    assert result.loc[0, "SEO Title"] == "Buy Clean Bag Online - Lavie World"
    assert result.loc[0, "Image Alt Text"] == "Clean Bag Black"
    assert result.loc[0, "Color (product.metafields.shopify.color-pattern)"] == ""
    assert result.loc[0, "Size (product.metafields.shopify.size)"] == ""
    assert result.loc[0, "Target gender (product.metafields.shopify.target-gender)"] == ""
    assert result.loc[0, "Status"] == "draft"
    assert result.loc[0, "Dimensions (product.metafields.my_fields.specifications)"] == "40L X 9W X 26H cm"
    assert "Manufactured by" in result.loc[0, "Manufacturer Details (product.metafields.my_fields.manufacturer_details)"]


def test_dropbox_mapping_is_headerless_and_keeps_more_than_ten_images(tmp_path: Path):
    dropbox_path = tmp_path / "dropbox.xlsx"
    many_images = [f"https://www.dropbox.com/scl/fi/file/SKU1_{idx}.jpg?dl=0" for idx in range(1, 12)]
    sport = pd.DataFrame([["SKU1", *many_images]])
    other = pd.DataFrame([["OTHER", "https://example.com/other.jpg"]])
    with pd.ExcelWriter(dropbox_path, engine="openpyxl") as writer:
        other.to_excel(writer, index=False, header=False, sheet_name="Other")
        sport.to_excel(writer, index=False, header=False, sheet_name="Sport")

    product = pd.DataFrame(
        {
            "SKU": ["SKU1", "", "SKU2"],
            "Handle": ["h1", "", "h2"],
            "Image Src": ["", "", ""],
            "Image Position": ["", "", ""],
            "Variant Image": ["", "", ""],
            "Image Alt Text": ["Alt 1", "", "Alt 2"],
        }
    )

    result, stats = map_product_images(product, dropbox_path)

    assert stats["selected_sheet"] == "Sport"
    assert stats["skus_with_images"] == 1
    assert stats["blank_sku_rows_preserved"] == 1
    assert stats["missing_image_skus"] == ["SKU2"]
    assert result.iloc[0]["Image Position"] == "1"
    assert "dl.dropboxusercontent.com" in result.iloc[0]["Image Src"]
    assert result.iloc[0]["Variant Image"] == result.iloc[0]["Image Src"]
    assert result.iloc[1]["Variant Image"] == ""
    assert result[result["SKU"] == "SKU1"].shape[0] == 11
    assert result[result["SKU"] == "SKU2"].shape[0] == 1
    assert result[result["SKU"] == ""].shape[0] == 1
    image_rows = result[(result["SKU"] == "SKU1") & (result["Image Src"] != "")]
    assert image_rows["Handle"].eq("h1").all()
    assert image_rows["Image Alt Text"].eq("Alt 1").all()


def test_direct_dropbox_urls_are_preserved():
    url = "https://dl.dropboxusercontent.com/scl/fi/file/image.jpg?dl=0"
    assert normalize_dropbox_url(url) == url
