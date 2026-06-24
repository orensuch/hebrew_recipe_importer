"""Turn scraped page text into a structured recipe via a local Ollama model.

Uses Ollama's structured-output feature: we pass our JSON schema in the
`format` field so the model is grammar-constrained to emit valid JSON matching
the Recipe shape. Temperature 0 for determinism.
"""
from __future__ import annotations

import json

import httpx

from .config import settings
from .models import RECIPE_JSON_SCHEMA, Recipe
from .scraper import ScrapedPage

SYSTEM_PROMPT = """\
You extract structured data from a cooking recipe web page.

CRITICAL RULES:
- The recipe is in Hebrew. Copy ALL Hebrew text EXACTLY as it appears. Do NOT \
translate, transliterate, normalise, correct spelling, or reword anything. \
Never convert text to or from English.
- Do NOT invent information. If a field is unknown, return an empty string \
(or an empty list). Never guess quantities.
- For every ingredient, only SPLIT the original line into three parts without \
changing the words:
    amount = the leading numeric quantity as written (e.g. "2", "1/2", "2-3", ""),
    unit   = the measurement unit in Hebrew if one is clearly present (e.g. \
"כוסות", "גרם", "כפית", "כף", "מ\"ל"); otherwise "",
    name   = the rest of the line, verbatim, in Hebrew.
  Concatenating amount + unit + name must reproduce the original line. If you \
are unsure how to split, put the whole original line in name and leave amount \
and unit empty.
- A recipe often has several components / sub-recipes (e.g. a salad and a \
dressing, a cake and a frosting). Component headers in the source are short \
standalone lines, usually ending with a colon — e.g. "לרוטב:", "לסלט:", \
"לבצק:", "לציפוי:", "למילוי:", "לקרם:". For EVERY ingredient set "section" to \
the header it falls under, in Hebrew and WITHOUT the trailing colon (e.g. \
"לרוטב"); use "" for ingredients listed before any header. Apply the same \
"section" value to the stages that belong to that component.
- Capture EVERY ingredient from EVERY component. Do not stop after the first \
group — the sauce/dressing/topping ingredients that follow a header must all be \
included.
- "stages" are the preparation steps in their original order, copied verbatim. \
Number them starting at 1 in the "step" field.
- "tags" are short Hebrew keywords/categories for the dish (e.g. "קינוח", \
"טבעוני", "ללא גלוטן"). Use [] if none are present.
- Ignore navigation, ads, comments, and unrelated page text.
- Respond ONLY with JSON that matches the required schema. No prose, no markdown.\
"""


def _build_user_prompt(page: ScrapedPage) -> str:
    parts = [f"Page title: {page.title or '(none)'}", f"Source URL: {page.url}"]
    if page.json_ld:
        parts.append(
            "Structured recipe data found embedded in the page "
            "(schema.org/Recipe). Prefer it when it is clearly correct:\n"
            + page.json_ld
        )
    if page.site_tags:
        parts.append(
            "Tags/categories already declared on the page. Include the relevant "
            "ones in 'tags', keeping them in Hebrew exactly as written:\n"
            + ", ".join(page.site_tags)
        )
    parts.append("Full page text:\n" + page.text)
    return "\n\n".join(parts)


def merge_site_tags(model_tags: list[str], site_tags: list[str]) -> list[str]:
    """Union of the model's tags and the page's own tags, de-duplicated.

    Site tags come first (they are the article's real labels) and casing/
    duplicates are collapsed case-insensitively.
    """
    out: list[str] = []
    seen: set[str] = set()
    for tag in list(site_tags) + list(model_tags):
        t = (tag or "").strip()
        key = t.lower()
        if t and key not in seen:
            seen.add(key)
            out.append(t)
    return out


def apply_site_meta(recipe, site_meta: dict) -> None:
    """Prefer times/yield declared by the article over the model's reading."""
    for field_name in ("prep_time", "cook_time", "total_time", "servings"):
        value = (site_meta or {}).get(field_name)
        if value:
            setattr(recipe, field_name, value)


class OllamaError(RuntimeError):
    pass


async def parse_recipe(page: ScrapedPage, model: str | None = None) -> Recipe:
    model = model or settings.ollama_model
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(page)},
        ],
        "format": RECIPE_JSON_SCHEMA,
        "stream": False,
        "options": {"temperature": 0, "num_ctx": settings.ollama_num_ctx},
    }

    url = f"{settings.ollama_host}/api/chat"
    try:
        async with httpx.AsyncClient(timeout=settings.ollama_timeout) as client:
            resp = await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        raise OllamaError(
            f"Could not reach Ollama at {settings.ollama_host}. "
            f"Is it running and reachable? ({exc})"
        ) from exc

    if resp.status_code == 404:
        raise OllamaError(
            f"Model '{model}' was not found in Ollama. "
            f"Pull it first: `ollama pull {model}`."
        )
    if resp.status_code >= 400:
        raise OllamaError(f"Ollama returned HTTP {resp.status_code}: {resp.text[:300]}")

    body = resp.json()
    content = (body.get("message") or {}).get("content", "")
    if not content:
        raise OllamaError("Ollama returned an empty response.")

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise OllamaError(f"Model did not return valid JSON: {exc}") from exc

    # Validate / normalise; tolerate missing keys via model defaults.
    recipe = Recipe.model_validate(data)

    # Re-number stages defensively in case the model didn't.
    for i, stage in enumerate(recipe.stages, start=1):
        if stage.step != i:
            stage.step = i
    return recipe


async def check_ollama() -> dict:
    """Health probe: confirm Ollama is up and whether the model is present."""
    url = f"{settings.ollama_host}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            tags = [m.get("name") for m in resp.json().get("models", [])]
    except httpx.HTTPError as exc:
        return {
            "ollama_reachable": False,
            "host": settings.ollama_host,
            "error": str(exc),
        }
    return {
        "ollama_reachable": True,
        "host": settings.ollama_host,
        "model": settings.ollama_model,
        "model_available": settings.ollama_model in tags,
        "available_models": tags,
    }
