"""Push a parsed recipe into a Mealie instance.

Mealie's create flow is two steps:
  1. POST /api/recipes {"name": ...}          -> returns the new recipe slug
  2. PATCH /api/recipes/{slug} {full fields}   -> populate everything else

Ingredients are sent as free-text "notes" with disableAmount=true. That is the
documented reliable path for non-English recipes: the structured quantity/unit/
food fields require pre-existing food & unit IDs (and would otherwise silently
drop or pollute Mealie's food database). Notes preserve the Hebrew text exactly
as parsed; users can later run Mealie's "parse" action to structure them.

Tags are resolved/created best-effort against /api/organizers/tags so they show
up as real Mealie tags; failures there never abort the recipe push.
"""
from __future__ import annotations

import logging
import uuid
from typing import List, Optional
from urllib.parse import urlparse

import httpx

from .config import settings
from .models import Ingredient, Recipe

log = logging.getLogger("recipe-parser.mealie")


class MealieError(RuntimeError):
    pass


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.mealie_token}"}


def ingredient_note(ing: Ingredient) -> str:
    """Combine amount + unit + name into one display string, e.g. '2 כוסות קמח'."""
    return " ".join(p for p in (ing.amount.strip(), ing.unit.strip(), ing.name.strip()) if p)


def to_quantity(amount: str) -> float:
    """Best-effort numeric quantity (kept for future scaling; not displayed)."""
    a = amount.strip().replace(",", ".")
    try:
        return float(a)
    except ValueError:
        if "/" in a:
            try:
                num, den = a.split("/", 1)
                return float(num) / float(den)
            except (ValueError, ZeroDivisionError):
                pass
        return 1.0


def _is_section_header(line: str) -> bool:
    """A short standalone line ending with ':' and no digits, e.g. 'לרוטב:'."""
    s = line.strip()
    return bool(s) and s.endswith(":") and len(s) <= 30 and not any(c.isdigit() for c in s)


def _sectioned_notes(recipe: Recipe, raw_ingredients: Optional[List[str]]):
    """Return ordered (note, section) pairs.

    From verbatim JSON-LD lines we detect inline header lines ('לרוטב:'); from
    the model's ingredients we use each ingredient's own `section`.
    """
    pairs = []
    if raw_ingredients:
        current = ""
        for line in raw_ingredients:
            t = (line or "").strip()
            if not t:
                continue
            if _is_section_header(t):
                current = t.rstrip(":").strip()
                continue
            pairs.append((t, current))
    else:
        for ing in recipe.ingredients:
            note = ingredient_note(ing)
            if note:
                pairs.append((note, (ing.section or "").strip()))
    return pairs


def build_payload(recipe: Recipe, name: str, source_url: str, tags: List[dict],
                  raw_ingredients: Optional[List[str]] = None) -> dict:
    # Prefer the article's verbatim ingredient lines (exact Hebrew). Fall back to
    # the model's parsed ingredients only when the page had no structured lines.
    pairs = _sectioned_notes(recipe, raw_ingredients)

    # Each ingredient gets a real UUID referenceId. Mealie REQUIRES referenceIds
    # to be valid UUIDs: sending a non-UUID string makes a failed PATCH leave the
    # recipe half-written and unopenable (see Mealie issue #7072), so we never
    # use arbitrary strings or null here. A component header is written as the
    # `title` on the first ingredient of each section (Mealie's section header).
    ingredients = []
    prev_section = None
    for note, section in pairs:
        title = section if section and section != prev_section else None
        prev_section = section
        ingredients.append({
            "referenceId": str(uuid.uuid4()),
            "quantity": 1,
            "unit": None,
            "food": None,
            "note": note,
            "isFood": False,
            "disableAmount": True,
            "title": title,
            "originalText": note,
        })

    # recipeInstructions items MUST carry ingredientReferences (even if empty),
    # otherwise Mealie 3.x raises:
    #   TypeError: RecipeInstruction.__init__() missing ... 'ingredient_references'
    # A step's section becomes its `title` (a step-group header in Mealie).
    instructions = []
    prev_section = None
    for stage in recipe.stages:
        text = stage.instruction.strip()
        if not text:
            continue
        section = (stage.section or "").strip()
        title = section if section and section != prev_section else ""
        prev_section = section
        instructions.append({
            "id": str(uuid.uuid4()),
            "title": title,
            "summary": "",
            "text": text,
            "ingredientReferences": [],
        })

    return {
        "name": name,
        "description": recipe.description or "",
        "recipeYield": recipe.servings or "",
        "prepTime": recipe.prep_time or None,
        "performTime": recipe.cook_time or None,
        "totalTime": recipe.total_time or None,
        "orgURL": source_url,
        "recipeIngredient": ingredients,
        "recipeInstructions": instructions,
        "tags": tags,
        "settings": {"disableAmount": True},
    }


async def _resolve_tags(client: httpx.AsyncClient, names: List[str]) -> List[dict]:
    """Match existing tags by name (case-insensitive); create any that are missing."""
    names = [n.strip() for n in names if n and n.strip()]
    if not names:
        return []
    base = settings.mealie_url
    existing: dict = {}
    try:
        resp = await client.get(
            f"{base}/api/organizers/tags",
            params={"perPage": -1},
            headers=_headers(),
        )
        if resp.status_code < 400:
            for tag in resp.json().get("items", []):
                existing[tag["name"].strip().lower()] = tag
    except httpx.HTTPError:
        return []

    out: List[dict] = []
    seen = set()
    for name in names:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        if key in existing:
            out.append(existing[key])
            continue
        try:
            cr = await client.post(
                f"{base}/api/organizers/tags", json={"name": name}, headers=_headers()
            )
            if cr.status_code < 400:
                tag = cr.json()
                existing[key] = tag
                out.append(tag)
        except httpx.HTTPError:
            continue
    return out


async def push_to_mealie(recipe: Recipe, source_url: str, image_url: str = "",
                         raw_ingredients: Optional[List[str]] = None) -> dict:
    if not settings.mealie_token:
        raise MealieError(
            "Mealie is not configured. Set MEALIE_API_TOKEN "
            "(create a token in Mealie under Settings -> API Tokens)."
        )

    base = settings.mealie_url
    name = (recipe.title or "").strip() or "מתכון ללא שם"

    async with httpx.AsyncClient(timeout=settings.mealie_timeout) as client:
        # 1) create the empty recipe -> slug
        try:
            cr = await client.post(
                f"{base}/api/recipes", json={"name": name}, headers=_headers()
            )
        except httpx.HTTPError as exc:
            raise MealieError(
                f"Could not reach Mealie at {base}. Is it running and reachable? ({exc})"
            ) from exc

        if cr.status_code in (401, 403):
            raise MealieError("Mealie rejected the API token (check MEALIE_API_TOKEN).")
        if cr.status_code >= 400:
            raise MealieError(
                f"Creating the recipe failed (HTTP {cr.status_code}): {cr.text[:200]}"
            )

        slug = cr.json()
        if isinstance(slug, dict):  # some versions return an object
            slug = slug.get("slug") or slug.get("name")
        if not slug:
            raise MealieError("Mealie did not return a recipe slug.")

        # 2) populate it
        tags = await _resolve_tags(client, recipe.tags)
        payload = build_payload(recipe, name, source_url, tags, raw_ingredients)

        pr = await client.patch(
            f"{base}/api/recipes/{slug}", json=payload, headers=_headers()
        )
        if pr.status_code >= 400:
            raise MealieError(
                f"Saving recipe details failed (HTTP {pr.status_code}): {pr.text[:300]}"
            )

        # 3) image (best-effort). We download the bytes ourselves and upload
        #    them; letting Mealie fetch the URL is unreliable across CDNs.
        image_set, image_error = False, ""
        if image_url:
            image_set, image_error = await _set_image(client, slug, image_url, source_url)
            log.info("image for %s: set=%s err=%s url=%s",
                     slug, image_set, image_error, image_url)
        else:
            log.info("image for %s: no image URL was found on the page", slug)

    public = settings.mealie_public_url or base
    return {
        "slug": slug,
        "name": name,
        "recipe_url": f"{public}/g/{settings.mealie_group}/r/{slug}",
        "tags_attached": len(tags),
        "ingredients": len(payload["recipeIngredient"]),
        "stages": len(payload["recipeInstructions"]),
        "image_found": bool(image_url),
        "image_set": image_set,
        "image_error": image_error,
    }


_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_CT_TO_EXT = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
    "image/webp": "webp", "image/gif": "gif", "image/avif": "avif",
}


def _guess_ext(content_type: str, url: str):
    """Return (extension, mime) for Mealie's upload, from content-type then URL."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _CT_TO_EXT:
        ext = _CT_TO_EXT[ct]
        return ext, ct
    path = urlparse(url).path.lower()
    for ext in ("jpeg", "jpg", "png", "webp", "gif", "avif"):
        if path.endswith("." + ext):
            norm = "jpg" if ext == "jpeg" else ext
            return norm, f"image/{'jpeg' if norm == 'jpg' else norm}"
    return "jpg", "image/jpeg"


async def download_image(url: str, referer: str = ""):
    """Fetch image bytes with a browser UA (+Referer for hotlink protection).

    Returns (content, content_type, error_message); content is None on failure.
    Shared by the Mealie upload and the UI image proxy so what you preview is
    exactly what the container fetched.
    """
    headers = {"User-Agent": _BROWSER_UA, "Accept": "image/*,*/*;q=0.8"}
    if referer:
        headers["Referer"] = referer
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=20, headers=headers
        ) as dl:
            r = await dl.get(url)
    except httpx.HTTPError as exc:
        return None, "", f"download failed: {exc}"
    if r.status_code >= 400:
        return None, "", f"download HTTP {r.status_code}"
    if not r.content:
        return None, "", "downloaded 0 bytes"
    return r.content, r.headers.get("content-type", ""), ""


async def _set_image(client: httpx.AsyncClient, slug: str, image_url: str, referer: str = ""):
    """Attach an image to the recipe. Returns (ok, error_message); never raises.

    We download the bytes ourselves and upload them via PUT /api/recipes/{slug}/
    image (multipart: image + extension). Letting Mealie fetch the URL itself is
    unreliable — many CDNs/WAFs block its request and Mealie can still report
    success without an image (Mealie issue #7578). If our own download fails, we
    fall back to asking Mealie to fetch the URL.
    """
    content, ctype, dl_err = await download_image(image_url, referer)

    # Preferred path: upload the raw bytes we fetched.
    if content:
        ext, mime = _guess_ext(ctype, image_url)
        try:
            resp = await client.put(
                f"{settings.mealie_url}/api/recipes/{slug}/image",
                files={"image": (f"image.{ext}", content, mime)},
                data={"extension": ext},
                headers=_headers(),
            )
        except httpx.HTTPError as exc:
            return False, f"upload failed: {exc}"
        if resp.status_code < 400:
            return True, ""
        return False, f"upload HTTP {resp.status_code}: {resp.text[:160]}"

    # Fallback: let Mealie fetch the URL server-side.
    try:
        resp = await client.post(
            f"{settings.mealie_url}/api/recipes/{slug}/image",
            json={"url": image_url, "includeTags": False},
            headers=_headers(),
        )
    except httpx.HTTPError as exc:
        return False, f"{dl_err}; server-fetch failed: {exc}"
    if resp.status_code < 400:
        return True, ""
    return False, f"{dl_err}; server-fetch HTTP {resp.status_code}"


async def check_mealie() -> dict:
    """Report whether Mealie is configured and reachable (for the UI pill)."""
    if not settings.mealie_token:
        return {"mealie_configured": False, "mealie_reachable": None}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(f"{settings.mealie_url}/api/app/about")
            reachable = resp.status_code < 400
            error = "" if reachable else f"HTTP {resp.status_code}"
    except httpx.HTTPError as exc:
        reachable, error = False, str(exc)
    return {
        "mealie_configured": True,
        "mealie_reachable": reachable,
        "mealie_url": settings.mealie_public_url,
        "mealie_target": settings.mealie_url,
        "mealie_error": error,
    }
