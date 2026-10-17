"""Store configuration and catalog normalisation rules.

Everything that decides *how a value should look in Shopify* lives here:
store settings, Type (Subcategory) normalisation, colour and size clean-up,
and the deterministic title builders used for Akiki and as the Lavie fallback
when AI titles are unavailable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any, Iterable

import pandas as pd


# ---------------------------------------------------------------------------
# Store configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreConfig:
    key: str
    name: str
    tagline: str
    seo_suffix: str
    title_strategy: str  # "ai" (Lavie) or "family" (Akiki)
    title_pattern: str
    vendor_override: str | None = None
    handle_prefix: str | None = None


STORES: dict[str, StoreConfig] = {
    "lavie": StoreConfig(
        key="lavie",
        name="LAVIE",
        tagline="Lavie, Lavie Luxe, Lavie Sport, Lavie Signature and Lavie Paris listings.",
        seo_suffix="Lavie World",
        title_strategy="ai",
        title_pattern="Name + Size + Colour + Type (AI written, SEO friendly)",
    ),
    "akiki": StoreConfig(
        key="akiki",
        name="AKIKI LONDON",
        tagline="Akiki London handbags, clutches, totes and slings.",
        seo_suffix="Akiki London",
        title_strategy="family",
        title_pattern="Family + Size + Colour + Type",
        vendor_override="Akiki",
        handle_prefix="akiki-london",
    ),
}


def get_store(key: str | None) -> StoreConfig:
    store = STORES.get((key or "").strip().lower())
    if store is None:
        raise ValueError(f"Unknown store '{key}'. Choose one of: {', '.join(STORES)}")
    return store


# ---------------------------------------------------------------------------
# Small text helpers
# ---------------------------------------------------------------------------


def clean_text(value: Any) -> str:
    """Return a stripped string, or '' for blank / NaN / NA style values."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = re.sub(r"\s+", " ", str(value)).strip()
    if text.upper() in {"NA", "N/A", "NONE", "NAN", "NULL", "-"}:
        return ""
    return text


KEEP_UPPER = {"LL", "XL", "XS", "XXL", "EDP", "EDT", "USB", "RFID", "UK", "USA", "II", "III"}


def smart_title(text: str) -> str:
    """Title-case words while keeping known acronyms and dotted/hyphenated parts tidy."""

    def cap_piece(piece: str) -> str:
        if not piece:
            return piece
        if piece.upper() in KEEP_UPPER:
            return piece.upper()
        return piece[0].upper() + piece[1:].lower()

    def cap_word(word: str) -> str:
        if word.upper() in KEEP_UPPER:
            return word.upper()
        # Capitalise every alphabetic run that follows a separator: "mt.pink" -> "Mt.Pink"
        return re.sub(r"[A-Za-z][A-Za-z']*", lambda m: cap_piece(m.group(0)), word)

    return " ".join(cap_word(word) for word in clean_text(text).split(" ") if word)


def dedupe_words(text: str) -> str:
    """Drop immediately repeated words: 'Tote Tote Bag' -> 'Tote Bag'."""
    words = text.split()
    result: list[str] = []
    for word in words:
        if result and result[-1].lower() == word.lower():
            continue
        result.append(word)
    return " ".join(result)


def remove_phrase(text: str, phrase: str) -> str:
    phrase = clean_text(phrase)
    if not phrase:
        return text
    pattern = r"(?<![A-Za-z0-9])" + re.escape(phrase) + r"(?![A-Za-z0-9])"
    return re.sub(pattern, " ", text, flags=re.IGNORECASE)


# ---------------------------------------------------------------------------
# Type (Shopify "Type" <- Content Master "Subcategory")
# ---------------------------------------------------------------------------

# Canonical types follow what the live LAVIE / AKIKI stores already use.
# Keys are matched after lower-casing, punctuation clean-up and singularising,
# so "SLING BAGS", "Sling bag " and "sling" all hit the same entry.
_TYPE_ALIAS_SOURCE: dict[str, Iterable[str]] = {
    "Satchel Bag": [
        "satchel", "satchel bag", "satchel handbag", "sathcel", "sathcel bag", "satchal",
        "satchal bag", "flap satchel", "dome sat", "dome satchel", "dome satchel bag",
        "box bag", "box satchel", "medium satchel", "top handle", "top handle bag", "dome",
    ],
    "Sling Bag": [
        "sling", "sling bag", "slingbag", "party sling", "box sling", "casual sling",
        "saddle sling", "flap sling", "dome sling", "horizontal sling", "crossbody",
        "cross body", "crossbody bag", "crossbody sling", "crossbody sling bag",
        "messenger sling", "messenger sling bag", "messenger bag", "messenger",
        "unisex sling bag", "unisex sling", "top zip", "travel sling", "mobile sling",
    ],
    "Tote Bag": ["tote", "tote bag", "tote handbag", "tote/laptop bag", "toe handbag", "toehandbag", "shopper"],
    "Laptop Handbag": [
        "laptop handbag", "laptop hangbag", "laptop hand bag", "laptop tote", "laptop tote bag",
        "laptop bag", "office bag", "laptop office bag",
    ],
    "Laptop Bag": ["laptop briefcase", "bags & sleeves", "laptop sleeve", "sleeve"],
    "Briefcase": ["briefcase", "briefcase bag"],
    "Backpack": [
        "backpack", "school backpack", "school bag", "school bag laptop", "school daypack",
        "school/casual backpack", "casual backpack", "casual daypack", "daypack",
        "rucksack", "rucksacks & trekking backpack", "trekking backpack",
    ],
    "Laptop Backpack": ["laptop backpack", "overnighter laptop backpack", "laptop bp"],
    "Fashion Backpack": ["fashion backpack", "ladies backpack", "women backpack"],
    "Wallet": [
        "wallet", "unisex wallet", "men wallet", "mens wallet", "women wallet", "womens wallet",
        "ladies wallet", "purse", "card holder", "card wallet",
    ],
    "Clutch": [
        "clutch", "framed clutch", "frame clutch", "foldover clutch", "fold over clutch",
        "flap over clutch", "flapover clutch", "party clutch", "envelope clutch",
    ],
    "Frame Bag": ["frame", "frame bag", "framed bag", "framed", "quare frame bag", "frame handbag"],
    "Hobo Bag": ["hobo", "hobo bag", "hobo handbag"],
    "Handbag": [
        "handbag", "hand bag", "syn handbag", "synthetic handbag", "combo handbag",
        "synthetic bag", "ladies handbag", "women handbag",
    ],
    "Wheel Duffle": [
        "wheel duffle", "travel wheel duffle", "travel duffle", "overnighter trolley",
        "wheel duffel", "trolley duffle", "trolley",
    ],
    "Duffle": [
        "duffle", "duffel", "duffle bag", "non wheel duffle", "duffle casual", "gym bag",
        "travel overnighter duffle", "laptop overnighter", "overnighter",
    ],
    "Potli": ["potli", "potli bag"],
    "Perfume": ["perfume", "perfume edp", "edp", "fragrance"],
    "Bucket Bag": ["bucket", "bucket bag"],
    "Trunk Bag": ["trunk", "trunk bag"],
    "Vanity Bag": ["vanity", "vanity bag", "vanity pouch"],
    "Gift Set": [
        "gift set", "giftset", "men giftset", "women giftset", "men's gift box", "mens gift box",
        "women's gift box", "womens gift box", "unisex gift box", "gift box",
    ],
    "Belt": ["belt"],
    "Charm": ["charm", "bag charm"],
    "Eye Mask": ["eyemask", "eye mask"],
    "Baguette Bag": ["baguette", "baguette bag"],
    "Mobile Pouch": ["mobile pouch", "phone pouch"],
    "Passport Cover": ["passport cover", "passport holder"],
    "Horizontal Bag": ["horizontal bag"],
    "Sling Strap": ["sling strap", "bag strap"],
}

# Ordered keyword rules used when no alias matches (first match wins).
_TYPE_KEYWORD_RULES: list[tuple[tuple[str, ...], str]] = [
    (("laptop", "backpack"), "Laptop Backpack"),
    (("fashion", "backpack"), "Fashion Backpack"),
    (("backpack",), "Backpack"),
    (("daypack",), "Backpack"),
    (("rucksack",), "Backpack"),
    (("school",), "Backpack"),
    (("wheel",), "Wheel Duffle"),
    (("trolley",), "Wheel Duffle"),
    (("duffle",), "Duffle"),
    (("duffel",), "Duffle"),
    (("clutch",), "Clutch"),
    (("laptop",), "Laptop Handbag"),
    (("sling",), "Sling Bag"),
    (("crossbody",), "Sling Bag"),
    (("messenger",), "Sling Bag"),
    (("tote",), "Tote Bag"),
    (("satchel",), "Satchel Bag"),
    (("sathcel",), "Satchel Bag"),
    (("hobo",), "Hobo Bag"),
    (("frame",), "Frame Bag"),
    (("bucket",), "Bucket Bag"),
    (("trunk",), "Trunk Bag"),
    (("vanity",), "Vanity Bag"),
    (("potli",), "Potli"),
    (("wallet",), "Wallet"),
    (("perfume",), "Perfume"),
    (("gift",), "Gift Set"),
    (("briefcase",), "Briefcase"),
    (("handbag",), "Handbag"),
    (("hangbag",), "Handbag"),
]

_TYPE_FILLER_WORDS = {
    "women", "woman", "womens", "men", "man", "mens", "ladies", "lady", "unisex", "for",
    "girls", "boys", "kids", "small", "medium", "large", "mini", "big", "new", "and", "the",
}


def _singular(word: str) -> str:
    if len(word) <= 3 or word.endswith("ss"):
        return word
    if word.endswith(("ches", "shes", "xes")):
        return word[:-2]
    if word.endswith("s"):
        return word[:-1]
    return word


def _type_key(text: str) -> str:
    text = clean_text(text).lower().replace("’", "'")
    text = re.sub(r"[^a-z0-9/&' ]+", " ", text)
    text = text.replace("'s ", " ").replace("' ", " ")
    words = [_singular(word.strip("'")) for word in text.split()]
    return " ".join(word for word in words if word)


TYPE_ALIASES: dict[str, str] = {}
for _canonical, _aliases in _TYPE_ALIAS_SOURCE.items():
    TYPE_ALIASES[_type_key(_canonical)] = _canonical
    for _alias in _aliases:
        TYPE_ALIASES[_type_key(_alias)] = _canonical
CANONICAL_TYPES = sorted(_TYPE_ALIAS_SOURCE)


def _match_type(text: str) -> tuple[str, str] | None:
    key = _type_key(text)
    if not key:
        return None
    if key in TYPE_ALIASES:
        return TYPE_ALIASES[key], "exact"

    stripped = " ".join(word for word in key.split() if word not in _TYPE_FILLER_WORDS)
    if stripped and stripped in TYPE_ALIASES:
        return TYPE_ALIASES[stripped], "exact"

    candidate = stripped or key
    close = get_close_matches(candidate, list(TYPE_ALIASES), n=1, cutoff=0.84)
    if close:
        return TYPE_ALIASES[close[0]], "fuzzy"

    compact = candidate.replace(" ", "")
    tokens = set(candidate.split())
    for keywords, canonical in _TYPE_KEYWORD_RULES:
        if all(keyword in tokens or keyword in compact for keyword in keywords):
            return canonical, "keyword"
    if "sat" in tokens:
        return "Satchel Bag", "keyword"
    return None


def normalize_product_type(subcategory: Any, product_category: Any = None) -> tuple[str, str]:
    """Map a messy Content Master Subcategory to a clean Shopify Type.

    Returns (type, method). method is one of: exact, fuzzy, keyword,
    category (matched from Product Category), unmapped, missing.
    """
    sub = clean_text(subcategory)
    category = clean_text(product_category)

    # Long sentences are data-entry mistakes (descriptions pasted into the column).
    if sub and len(sub.split()) <= 6:
        match = _match_type(sub)
        if match:
            return match
    if category and len(category.split()) <= 6:
        match = _match_type(category)
        if match:
            return match[0], "category"
    if sub and len(sub.split()) > 6:
        match = _match_type(sub)
        if match:
            return match[0], "keyword"
    if sub and len(sub.split()) <= 6:
        return smart_title(sub), "unmapped"
    return "", "missing"


# ---------------------------------------------------------------------------
# Colour (Option1 Value <- GS "Colour")
# ---------------------------------------------------------------------------

_COLOR_PREFIXES = {
    "lt": "Light", "l": "Light", "dk": "Dark", "d": "Dark", "p": "Pale", "mt": "Metallic",
    "c": "Charcoal", "b": "Bright", "bt": "Bright", "r": "Royal",
}
_COLOR_WORD_FIXES = {
    "offwhite": "Off White",
    "off-white": "Off White",
    "ocher": "Ochre",
    "orcher": "Ochre",
    "biege": "Beige",
    "fushia": "Fuchsia",
    "fuschia": "Fuchsia",
    "mettalic": "Metallic",
    "metalic": "Metallic",
    "charcol": "Charcoal",
    "gray": "Grey",
    "multicolor": "Multicolour",
    "multicolour": "Multicolour",
    "pblue": "Pale Blue",
    "dpink": "Dark Pink",
    "lpink": "Light Pink",
}
# Whole-part abbreviations used in multi-pack codes such as "BK+NY+TL" or "NY+MR+OFWH".
_COLOR_PART_CODES = {
    "bk": "Black", "blk": "Black", "ny": "Navy", "tl": "Teal", "mr": "Maroon", "ofwh": "Off White",
    "ow": "Off White", "mt": "Mint", "gy": "Grey", "rd": "Red", "tn": "Tan", "pk": "Pink",
    "wh": "White", "br": "Brown", "bl": "Blue", "rybl": "Royal Blue",
}


def _normalize_color_part(part: str) -> str:
    text = clean_text(part).strip(" .-")
    if not text:
        return ""
    lower = text.lower()
    if lower in _COLOR_PART_CODES:
        return _COLOR_PART_CODES[lower]
    # Prefix abbreviations such as "LT PINK", "DK.GREY", "D.TAN", "MT. PINK", "P BLUE", "DK P BLUE".
    prefixes: list[str] = []
    while True:
        match = re.match(r"^(lt|dk|mt|bt|d|l|p|c|b|r)(?:\.\s*|\s+)([a-z].*)$", lower)
        if not match or len(match.group(2).replace(" ", "")) < 3:
            break
        prefixes.append(_COLOR_PREFIXES[match.group(1)])
        lower = match.group(2)
    if prefixes:
        lower = " ".join(prefixes + [lower])
    words = []
    for word in lower.split():
        fixed = _COLOR_WORD_FIXES.get(word)
        words.append(fixed if fixed else word)
    return smart_title(" ".join(words))


def normalize_color(value: Any) -> str:
    """'OFF WHITE/TAN' -> 'Off White & Tan', 'BLACK/BLACK' -> 'Black', 'LT PINK' -> 'Light Pink'."""
    text = clean_text(value)
    if not text:
        return ""
    parts = re.split(r"\s*(?:/|\+|&|,|\band\b)\s*", text, flags=re.IGNORECASE)
    cleaned: list[str] = []
    for part in parts:
        normalized = _normalize_color_part(part)
        if normalized and normalized.lower() not in {item.lower() for item in cleaned}:
            cleaned.append(normalized)
    return " & ".join(cleaned)


# ---------------------------------------------------------------------------
# Size (Option2 Value <- GS "Size")
# ---------------------------------------------------------------------------

_SIZE_MAP = {
    "x-large": "Extra Large",
    "x large": "Extra Large",
    "xlarge": "Extra Large",
    "extra large": "Extra Large",
    "xl": "Extra Large",
    "xx-large": "XXL",
    "xxl": "XXL",
    "x-small": "Extra Small",
    "x small": "Extra Small",
    "xsmall": "Extra Small",
    "extra small": "Extra Small",
    "xs": "Extra Small",
    "small": "Small",
    "sm": "Small",
    "s": "Small",
    "medium": "Medium",
    "md": "Medium",
    "med": "Medium",
    "m": "Medium",
    "large": "Large",
    "lg": "Large",
    "l": "Large",
    "one size": "One Size",
    "free size": "Free Size",
}


def normalize_size(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    lower = text.lower()
    if lower in _SIZE_MAP:
        return _SIZE_MAP[lower]
    ml = re.fullmatch(r"(\d+(?:\.\d+)?)\s*ml", lower)
    if ml:
        return f"{ml.group(1)} ml"
    cms = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(cm|cms)", lower)
    if cms:
        return f"{cms.group(1)} cms"
    return smart_title(text)


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------

BRAND_PREFIXES = [
    "lavie signature", "lavie luxury", "lavie luxe", "lavie sport", "lavie paris",
    "lavie world", "lavie (csd)", "lavie", "akiki london", "akiki", "ls", "lx",
]
_GENDER_PATTERNS = [
    r"\bfor\s+men\s*(?:&|and)\s*women\b",
    r"\bfor\s+women\s*(?:&|and)\s*men\b",
    r"\bfor\s+(?:women|woman|ladies|girls|men|man|boys|kids)\b",
    r"\b(?:women|woman|men|man|girl|boy|lady|kid)'?s\b",
    r"\b(?:women|woman|ladies|lady|girls|girl|boys|boy|unisex)\b",
]
_ABBREVIATIONS = {
    r"\bhz\b": "Horizontal",
    r"\bvt\b": "Vertical",
    r"\bza\b": "Zip Around",
    r"\bmob\b": "Mobile",
    r"\bdh\b": "Dual Handle",
    r"\btpzp\b": "Top Zip",
    r"\bdm\b": "Dome",
    r"\b(\d)\s*way\b": r"\1 Way",
}
_SIZE_WORDS = r"\b(?:extra\s+large|extra\s+small|x-large|x-small|small|medium|large|mini|sm|md|lg|xl|xs)\b"
_TYPE_NOUNS = {
    "bag", "bags", "handbag", "handbags", "hangbag", "satchel", "sathcel", "sat", "tote", "totes",
    "sling", "slings", "slingbag", "backpack", "backpacks", "bp", "wallet", "wallets", "clutch",
    "clutches", "frame", "framed", "frm", "hobo", "duffle", "duffel", "potli", "purse",
}


def _strip_brand(text: str, extra_brand: str = "") -> str:
    candidates = list(BRAND_PREFIXES)
    brand = clean_text(extra_brand).lower()
    if brand:
        candidates.insert(0, brand)
    changed = True
    while changed:
        changed = False
        for candidate in sorted(set(candidates), key=len, reverse=True):
            pattern = rf"^\s*{re.escape(candidate)}(?:'s)?(?![A-Za-z0-9])[\s\-:|]*"
            new_text = re.sub(pattern, "", text, flags=re.IGNORECASE)
            if new_text != text:
                text = new_text
                changed = True
    return text


def derive_style_name(
    product_title: Any = None,
    bz_description: Any = None,
    style_name: Any = None,
    brand: Any = None,
    color: Any = None,
    size: Any = None,
    product_type: Any = None,
    raw_color: Any = None,
) -> str:
    """Rule-based clean-up of a product name (the part before Size/Colour/Type)."""
    attrs = dict(brand=brand, color=color, size=size, product_type=product_type, raw_color=raw_color)
    title_name = _name_from_source(clean_text(product_title), from_sap=False, **attrs)
    sap_name = _name_from_source(clean_text(bz_description), from_sap=True, **attrs)
    # Content Master titles are sometimes generic ("Soft lg Tote"); the SAP text then says more.
    if title_name and (len(title_name.split()) > 1 or len(sap_name.split()) <= 1):
        name = title_name
    else:
        name = sap_name or title_name
    if not name:
        style = clean_text(style_name)
        name = _drop_type_words(style, product_type) or smart_title(style)
    return name


def _name_from_source(
    source: str,
    from_sap: bool,
    brand: Any = None,
    color: Any = None,
    size: Any = None,
    product_type: Any = None,
    raw_color: Any = None,
) -> str:
    if not source:
        return ""
    text = source.split("|", 1)[0]
    text = re.sub(r"\([^)]*\)", " ", text)
    if from_sap:
        # SAP short descriptions end with a category tail: "LS WHITNEY 2C SAT - LADIES HANDBAG".
        text = re.sub(r"\s*-\s*[A-Za-z ]*$", " ", text)
    text = _strip_brand(text, clean_text(brand))
    for pattern in _GENDER_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bwith\b.*$", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"(\d)\s*-\s*(?=[A-Za-z])", r"\1 ", text)
    text = re.sub(r"\b(\d)\s*c\b", r"\1 Compartment", text, flags=re.IGNORECASE)
    for pattern, replacement in _ABBREVIATIONS.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    # Season / year codes such as Glare27, Canva26, "Vogue 27".
    text = re.sub(r"(?<=[A-Za-z])2\d\b", " ", text)
    text = re.sub(r"\b2\d\b", " ", text)
    text = re.sub(_SIZE_WORDS, " ", text, flags=re.IGNORECASE)
    for phrase in [size, color, raw_color, product_type]:
        text = remove_phrase(text, clean_text(phrase))
    for part in re.split(r"\s*(?:&|/|\+)\s*", clean_text(color)):
        text = remove_phrase(text, part)
    return _drop_type_words(text, product_type)


def _drop_type_words(text: str, product_type: Any = None) -> str:
    type_tokens = {token.lower() for token in clean_text(product_type).split()}
    words = []
    for word in re.split(r"[\s:|,/]+", text):
        cleaned = re.sub(r"[^A-Za-z0-9&+.'\-]", "", word).strip(" .'\"-")
        token = cleaned.lower()
        if not token or token in _TYPE_NOUNS or token in type_tokens:
            continue
        words.append(cleaned)
    return dedupe_words(smart_title(" ".join(words)))


def compose_title(name: Any, size: Any, color: Any, product_type: Any) -> str:
    """Name + Size + Colour + Type with no repeated words."""
    name_text = clean_text(name)
    size_text = clean_text(size)
    color_text = clean_text(color)
    type_text = clean_text(product_type)

    trimmed = name_text
    for phrase in [size_text, color_text, type_text]:
        trimmed = remove_phrase(trimmed, phrase)
    trimmed = re.sub(r"\s+", " ", trimmed).strip(" -,")
    if trimmed:
        name_text = trimmed

    title = " ".join(part for part in [name_text, size_text, color_text, type_text] if part)
    return dedupe_words(re.sub(r"\s+", " ", title).strip())


def akiki_family_name(family: Any, style_name: Any = None, **fallback_kwargs: Any) -> str:
    name = smart_title(clean_text(family))
    if name:
        return name
    name = smart_title(clean_text(style_name))
    if name:
        return name
    return derive_style_name(style_name=style_name, **fallback_kwargs)


def style_group_key(product_title: Any, bz_description: Any, style_name: Any, product_type: Any) -> str:
    """Key shared by all colour variants of one style, so they get the same AI name."""
    base = clean_text(product_title) or clean_text(bz_description) or clean_text(style_name)
    base = re.sub(r"\([^)]*\)", " ", base)
    base = base.split("|", 1)[0]
    base = re.sub(r"[^a-z0-9]+", " ", base.lower()).strip()
    return f"{base}::{clean_text(product_type).lower()}"


def clean_ai_name(name: Any, brand: Any = None) -> str:
    """Sanity-check a name returned by the AI. Returns '' when it is unusable."""
    text = clean_text(name).strip("\"'` .,-")
    if not text:
        return ""
    text = re.sub(r"\([^)]*\)", " ", text).split("|", 1)[0]
    text = _strip_brand(text, clean_text(brand))
    for pattern in _GENDER_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    text = re.sub(_SIZE_WORDS, " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[^\w\s&'+.-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -.,")
    if not text or len(text) > 70 or len(text.split()) > 8:
        return ""
    result = " ".join(text.split())
    # Keep the model's casing when it is sensible, otherwise title-case it.
    if result.isupper() or result.islower():
        result = smart_title(result)
    return dedupe_words(result)
