"""Gemini powered title names for LAVIE listings.

The AI writes only the *name* part of a title (for example "Whitney 2 Compartment").
The pipeline then assembles the final title deterministically as

    {name} {Size} {Colour} {Type}

so every title follows the store pattern and never repeats size, colour or type,
even when the model makes a small mistake. Any style the model fails on falls
back to the rule-based name from catalog_rules.derive_style_name.

Configuration (environment variables):
    GEMINI_API_KEY   required to enable AI titles (GOOGLE_API_KEY also accepted)
    GEMINI_MODEL     optional, comma separated model ids to try first
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib import error, request


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODELS = ("gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-2.5-flash")

SYSTEM_PROMPT = """You are a senior e-commerce catalogue editor for LAVIE, an Indian handbag, wallet and luggage brand selling on Shopify. Sub-brands: Lavie, Lavie Luxe, Lavie Sport, Lavie Signature, Lavie Paris.

Store title pattern (assembled by code, not by you):
    {name} {Size} {Colour} {Type}
Example: "Whitney 2 Compartment" + "Medium" + "Off White" + "Satchel Bag" -> "Whitney 2 Compartment Medium Off White Satchel Bag"

YOUR TASK: write only the {name} part for each input item. Titles must be clean, human, SEO friendly and free of redundancy.

How to build {name}:
1. Find the style / model name. Prefer product_title (marketing title), then sap_description (upper-case SAP short text full of abbreviations), then style_name and family.
2. Keep the model name plus the design descriptors that distinguish styles, for example: 2 Compartment, 3 Compartment, Flap, Vertical, Horizontal, Zip Around, Trifold, Bifold, Card, Crossbody, Mobile, Combo Set, Dual Handle, Quilted, Emboss, Box, Pro, Duo, Soft, Mono, Mono LL, Monogram, Anti-Theft, capacity such as 24L.
3. Remove anything the code adds or that does not belong in a name:
   - brand / sub-brand words: Lavie, Lavie Luxe, Lavie Sport, Lavie Signature, Lavie Paris, Lavie World, LS, LX, Akiki
   - audience words: Women's, Woman's, Men's, Ladies, Girls, Boys, Unisex, "For Men & Women"
   - size words: Small, Medium, Large, Extra Large, Mini, SM, MD, LG, XL, and misspellings such as Meidum
   - colour words, including every colour listed for the item
   - product type words: Bag, Handbag, Satchel, Tote, Sling, Slingbag, Backpack, Wallet, Clutch, Frame, Hobo, Duffle, Potli, Purse
   - season or year codes glued to or following the name: Glare27 -> Glare, Canva26 -> Canva, "Vogue 27" -> Vogue
   - marketing tails after "|" or "with": "with Trolley Sleeve", "17 Inch", "Laptop Compatible", "School Bag for Girls"
   - brackets, pipes and stray punctuation
4. Expand abbreviations: 2C / 2c / 2-Compartment -> 2 Compartment, 3C -> 3 Compartment, HZ -> Horizontal, VT -> Vertical, ZA -> Zip Around, MOB -> Mobile, DH -> Dual Handle.
5. Use Title Case, keep acronyms such as LL in capitals, 1 to 5 words, never repeat a word, never invent words that are not supported by the input.
6. Each item is one style; all its colours and sizes share the name you write. Never mention a colour, size or type.
7. If nothing distinctive remains, return the style_name or family in Title Case.

Examples (input -> name):
- product_title "Lavie Signature Whitney 2c Women's Satchel Bag (Offwhite)", sap "LS WHITNEY 2C SAT - LADIES HANDBAG", type "Satchel Bag" -> "Whitney 2 Compartment"
- product_title "Lavie Signature Marina Women's Flap Satchel Bag (Tan)", type "Satchel Bag" -> "Marina Flap"
- product_title "Lavie Luxe Women's Glare27 Small Frame (Wine)", type "Frame Bag" -> "Glare"
- product_title "Lavie Paris Women's Soft Alfie Large Zip Around Wallet (OffWhite)", type "Wallet" -> "Soft Alfie Zip Around"
- product_title "Lavie Paris Women's Soft Karen Mobile 2-Compartment Sling Bag (Tan)", type "Sling Bag" -> "Soft Karen Mobile 2 Compartment"
- product_title "Lavie Paris Women's Soft Steph Vertical Tote (Wine)", type "Satchel Bag" -> "Soft Steph Vertical"
- product_title "Lavie Sport Legend26 Laptop backpack with Trolley sleeve For Men & Women (BLACK)", type "Laptop Backpack" -> "Legend"
- product_title "Lavie Signature Canva26 Combo Set (Wine)", type "Tote Bag" -> "Canva Combo Set"
- product_title "Lavie Luxe Women's Business Elite Backpack (Beige)", type "Backpack" -> "Business Elite"
- product_title "Lavie Paris Women's Mono LL Mark 3C Tote (Pearl)", type "Laptop Handbag" -> "Mono LL Mark 3 Compartment"
- product_title "Lavie Luxe Duo Quad Large Satchel (Tan)", type "Satchel Bag" -> "Duo Quad"
- product_title "Lavie Luxe Women's Lucie Emboss Bifold Zip-Around (Tan)", type "Wallet" -> "Lucie Emboss Bifold Zip Around"
- sap "SOFT NEOMEE MD TRIFOLD- WALLET", style_name "AMIEE", type "Wallet" -> "Soft Neomee Trifold"

Respond with JSON only: {"items": [{"id": "<id from input>", "name": "<name>"}]} containing exactly one entry for every input id."""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "STRING"},
                    "name": {"type": "STRING"},
                },
                "required": ["id", "name"],
            },
        }
    },
    "required": ["items"],
}


def gemini_api_key() -> str | None:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    key = (key or "").strip()
    return key or None


def configured_models() -> list[str]:
    raw = os.environ.get("GEMINI_MODEL", "")
    models = [model.strip() for model in raw.split(",") if model.strip()]
    for model in DEFAULT_MODELS:
        if model not in models:
            models.append(model)
    return models


class GeminiError(RuntimeError):
    pass


def build_user_prompt(items: list[dict[str, Any]]) -> str:
    lines = [
        "Write the {name} part for each item below.",
        "Each line is one JSON object. Return one result per id.",
        "",
    ]
    for item in items:
        lines.append(json.dumps(item, ensure_ascii=False))
    return "\n".join(lines)


def extract_response_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        feedback = payload.get("promptFeedback") or {}
        raise GeminiError(f"Gemini returned no candidates {feedback}".strip())
    parts = (candidates[0].get("content") or {}).get("parts") or []
    texts = [part.get("text", "") for part in parts if isinstance(part, dict) and not part.get("thought")]
    text = "".join(texts).strip()
    if not text:
        reason = candidates[0].get("finishReason", "unknown")
        raise GeminiError(f"Gemini returned an empty response (finish reason: {reason}).")
    return text


def parse_names(text: str) -> dict[str, str]:
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    data = json.loads(cleaned)
    entries = data.get("items", []) if isinstance(data, dict) else data
    names: dict[str, str] = {}
    if not isinstance(entries, list):
        return names
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        item_id = str(entry.get("id", "")).strip()
        name = str(entry.get("name", "")).strip()
        if item_id and name:
            names[item_id] = name
    return names


class GeminiTitleGenerator:
    """Callable: list of style dicts (each with an "id") -> {id: name}."""

    def __init__(
        self,
        api_key: str,
        models: list[str] | None = None,
        batch_size: int = 20,
        max_workers: int = 4,
        timeout: int = 60,
        max_retries: int = 2,
    ) -> None:
        if not api_key:
            raise ValueError("A Gemini API key is required.")
        self.api_key = api_key
        self.models = models or configured_models()
        self.batch_size = max(1, batch_size)
        self.max_workers = max(1, max_workers)
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self._lock = threading.Lock()
        self._unavailable: set[str] = set()
        self._schema_unsupported: set[str] = set()
        self.model_used: str | None = None
        self.errors: list[str] = []
        self.requests_made = 0

    # -- public -----------------------------------------------------------
    def __call__(self, items: list[dict[str, Any]]) -> dict[str, str]:
        if not items:
            return {}
        batches = [items[i : i + self.batch_size] for i in range(0, len(items), self.batch_size)]
        results: dict[str, str] = {}
        workers = min(self.max_workers, len(batches))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for batch_result in pool.map(self._run_batch, batches):
                results.update(batch_result)
        return results

    def describe(self) -> dict[str, Any]:
        return {
            "model": self.model_used,
            "requests": self.requests_made,
            "errors": list(dict.fromkeys(self.errors)),
        }

    # -- internals --------------------------------------------------------
    def _record_error(self, message: str) -> None:
        with self._lock:
            self.errors.append(message)

    def _run_batch(self, batch: list[dict[str, Any]]) -> dict[str, str]:
        try:
            text = self._generate(build_user_prompt(batch))
            names = parse_names(text)
            wanted = {str(item["id"]) for item in batch}
            return {key: value for key, value in names.items() if key in wanted}
        except Exception as exc:  # noqa: BLE001 - any failure falls back to rule-based names
            self._record_error(str(exc))
            return {}

    def _model_order(self) -> list[str]:
        with self._lock:
            preferred = [self.model_used] if self.model_used else []
            others = [m for m in self.models if m not in preferred and m not in self._unavailable]
        return preferred + others

    def _generate(self, prompt: str) -> str:
        last_error: Exception | None = None
        for model in self._model_order():
            attempt = 0
            while True:
                try:
                    text = self._post(model, prompt)
                    with self._lock:
                        self.model_used = self.model_used or model
                    return text
                except error.HTTPError as exc:
                    detail = _http_error_detail(exc)
                    last_error = GeminiError(f"{model}: HTTP {exc.code} {detail}".strip())
                    if exc.code in (401, 403) or (exc.code == 400 and "api key" in detail.lower()):
                        raise GeminiError(
                            f"Gemini rejected the API key (HTTP {exc.code}). Check GEMINI_API_KEY."
                        ) from exc
                    if exc.code == 404:
                        with self._lock:
                            self._unavailable.add(model)
                        break
                    if exc.code == 400 and model not in self._schema_unsupported:
                        # Retry once without the JSON schema (older / newer models may reject it).
                        with self._lock:
                            self._schema_unsupported.add(model)
                        continue
                    if exc.code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                        attempt += 1
                        time.sleep(2 * attempt)
                        continue
                    break
                except (error.URLError, TimeoutError, OSError, ValueError, GeminiError) as exc:
                    last_error = exc
                    if attempt < self.max_retries:
                        attempt += 1
                        time.sleep(1.5 * attempt)
                        continue
                    break
        raise GeminiError(str(last_error) if last_error else "No Gemini model is available.")

    def _post(self, model: str, prompt: str) -> str:
        generation_config: dict[str, Any] = {"responseMimeType": "application/json"}
        if model not in self._schema_unsupported:
            generation_config["responseSchema"] = RESPONSE_SCHEMA
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }
        req = request.Request(
            GEMINI_URL.format(model=model),
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
            method="POST",
        )
        with self._lock:
            self.requests_made += 1
        with request.urlopen(req, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return extract_response_text(payload)


def _http_error_detail(exc: error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
        return str((payload.get("error") or {}).get("message", ""))[:300]
    except Exception:  # noqa: BLE001
        return ""


def build_title_generator() -> GeminiTitleGenerator | None:
    key = gemini_api_key()
    if not key:
        return None
    return GeminiTitleGenerator(key)
