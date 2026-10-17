# Listing Studio: Shopify product listing automation

A Flask app that turns the Content Master, GS1 Master, an optional Dropbox image sheet and a Shopify product template into an import-ready Shopify CSV for **LAVIE** or **AKIKI LONDON**.

## How to use

1. Open the app and choose a store: LAVIE or AKIKI LONDON.
2. Upload the files on the store page:

| Upload | Required | What the tool reads |
| --- | --- | --- |
| Content Master sheet | Yes | BZ CODE, Product Title, BZ Description, Brand Name, Subcategory, Bullet Points, Special Features, metafield columns |
| GS sheet (GS1 Master) | Yes | Article, EAN/UPC, Colour, Size, NEW MRP, Family, Country, System Dimension |
| Dropbox image links | No | Column A = SKU, columns B onward = image links. Skip it and no image links are added. |
| Product list (SKUs) | Yes | Shopify product template with the SKU column filled |

3. Download the Shopify CSV, the Excel backup and the QA report.
4. Cross-check EAN codes, tags, titles and types before importing. The QA report has "Titles" and "Type Mapping" sheets for this.

## Key mappings

| Shopify column | Source |
| --- | --- |
| Title (LAVIE) | AI name + Size + Colour + Type. Rule-based backup when AI is off or fails. |
| Title (AKIKI) | GS Family + Size + Colour + Type |
| Type | Content Master Subcategory, cleaned to the store Type list (see `catalog_rules.py`) |
| Option1 Value | GS Colour, abbreviations expanded (LT PINK -> Light Pink, OFF WHITE/TAN -> Off White & Tan) |
| Option2 Value | GS Size |
| Variant Barcode / Variant Barcodes, Google Shopping / MPN | GS EAN/UPC as plain digits; stored as real numbers in the Excel backup |
| Badges (product.metafields.custom.badges) | New |
| Image Alt Text | Same as Title |
| SEO Title | Buy <title> Online - Lavie World / Akiki London |

Type spellings, colour codes and store settings (vendor, handle prefix, SEO suffix) live in `catalog_rules.py`, so they can be adjusted in one place.

## Gemini API key for AI titles (LAVIE)

1. Go to https://aistudio.google.com/apikey and sign in with a Google account.
2. Click **Create API key**, pick or create a Google Cloud project, and copy the key.
3. Local runs: copy `.env.example` to `.env` and set `GEMINI_API_KEY=<your key>`.
4. Vercel: Project Settings > Environment Variables > add `GEMINI_API_KEY` for Production (and Preview if wanted), then redeploy.
5. Optional: set `GEMINI_MODEL` to choose a model. The default tries `gemini-3.5-flash-lite`, then `gemini-3.8-flash`, then `gemini-2.5-flash`.

Never commit the key; `.env` is already in `.gitignore`. The LAVIE page shows "AI titles on" when the key is detected.

## Run locally

```bash
pip install -r requirements-dev.txt
flask --app app run
python -m pytest
```
