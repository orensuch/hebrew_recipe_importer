"""Fetch a recipe page and reduce it to clean text + metadata for the model.

Strategy:
1. Download the HTML with a browser-like User-Agent.
2. Pull the page <title>, any schema.org/Recipe JSON-LD block, the lead image
   (og:image / JSON-LD image / twitter:image) and the article's own tags
   (JSON-LD keywords/category/cuisine, article:tag, meta keywords).
3. Extract the main article text with trafilatura (boilerplate removed),
   falling back to a BeautifulSoup text dump.

Nothing here is Hebrew-specific; encoding is handled by httpx/trafilatura.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import urljoin

import httpx
import trafilatura
from bs4 import BeautifulSoup

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "he-IL,he;q=0.9,en;q=0.6",
}

# Tags longer than this are almost certainly SEO sentences, not real tags.
_MAX_TAG_LEN = 40
_MAX_TAGS = 15


class ScrapeError(RuntimeError):
    """Raised when the page cannot be fetched or yields no usable text."""


@dataclass
class ScrapedPage:
    url: str
    title: str
    text: str
    json_ld: Optional[str]            # compact JSON string of the Recipe block
    image_url: Optional[str] = None   # absolute URL of the lead image
    site_tags: List[str] = field(default_factory=list)  # tags found on the page
    raw_ingredients: List[str] = field(default_factory=list)  # verbatim lines
    site_meta: Dict[str, str] = field(default_factory=dict)   # times/yield from page


async def fetch_html(url: str, timeout: int) -> str:
    try:
        async with httpx.AsyncClient(
            headers=_HEADERS, follow_redirects=True, timeout=timeout
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text
    except httpx.HTTPStatusError as exc:
        raise ScrapeError(
            f"The page returned HTTP {exc.response.status_code}."
        ) from exc
    except httpx.HTTPError as exc:
        raise ScrapeError(f"Could not fetch the page: {exc}") from exc


def _looks_like_recipe(obj: object) -> bool:
    if isinstance(obj, dict):
        t = obj.get("@type")
        return t == "Recipe" or (isinstance(t, list) and "Recipe" in t)
    return False


def find_recipe_node(soup: BeautifulSoup) -> Optional[dict]:
    """Return the raw schema.org Recipe dict from any JSON-LD block, if present."""
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            candidates = data.get("@graph", [data]) if "@graph" in data else [data]
        else:
            continue
        for node in candidates:
            if _looks_like_recipe(node):
                return node
    return None


def slim_recipe_ld(node: dict) -> str:
    """Compact JSON of the recipe node, keeping only fields useful to the model."""
    keep = {
        "name", "description", "recipeYield", "prepTime", "cookTime",
        "totalTime", "recipeCategory", "recipeCuisine", "keywords",
        "recipeIngredient", "recipeInstructions",
    }
    slim = {k: v for k, v in node.items() if k in keep}
    return json.dumps(slim, ensure_ascii=False)


def _as_list(value) -> List[str]:
    """Normalise keywords/category/cuisine (str | list | comma-string) to a list."""
    out: List[str] = []
    if isinstance(value, str):
        out = value.split(",")
    elif isinstance(value, list):
        for v in value:
            if isinstance(v, str):
                out.append(v)
            elif isinstance(v, dict) and "name" in v:
                out.append(str(v["name"]))
    return out


def _clean_tags(raw: List[str]) -> List[str]:
    seen, out = set(), []
    for t in raw:
        t = (t or "").strip().strip("#").strip()
        if not t or len(t) > _MAX_TAG_LEN:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(out) >= _MAX_TAGS:
            break
    return out


def extract_site_tags(soup: BeautifulSoup, node: Optional[dict]) -> List[str]:
    """Collect tags the article itself declares (vegan, easy, meat, ...)."""
    raw: List[str] = []
    if node:
        for key in ("keywords", "recipeCategory", "recipeCuisine"):
            raw += _as_list(node.get(key))
    # <meta property="article:tag" content="טבעוני">
    for meta in soup.find_all("meta", attrs={"property": "article:tag"}):
        if meta.get("content"):
            raw.append(meta["content"])
    # <meta name="keywords" content="a, b, c">
    kw = soup.find("meta", attrs={"name": "keywords"})
    if kw and kw.get("content"):
        raw += kw["content"].split(",")
    return _clean_tags(raw)


def _image_from_node(node: dict):
    img = node.get("image") or node.get("thumbnailUrl")
    if isinstance(img, str):
        return img
    if isinstance(img, dict):
        return img.get("url") or img.get("contentUrl")
    if isinstance(img, list) and img:
        for item in img:
            if isinstance(item, str) and item.strip():
                return item
            if isinstance(item, dict) and (item.get("url") or item.get("contentUrl")):
                return item.get("url") or item.get("contentUrl")
    return None


def _largest_article_image(soup: BeautifulSoup) -> Optional[str]:
    """Heuristic fallback: the biggest <img> on the page, honouring lazy attrs."""
    best, best_score = None, 0
    for img in soup.find_all("img"):
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("data-original")
        )
        if not src and img.get("srcset"):
            # take the last (usually largest) candidate in srcset
            src = img["srcset"].split(",")[-1].strip().split(" ")[0]
        if not src or src.startswith("data:"):
            continue
        try:
            w = int(img.get("width") or 0)
            h = int(img.get("height") or 0)
        except ValueError:
            w = h = 0
        score = (w * h) or len(src)  # prefer sized images, else longer URLs
        if score > best_score:
            best, best_score = src, score
    return best


def extract_image(soup: BeautifulSoup, node: Optional[dict], base_url: str) -> Optional[str]:
    """Find the lead image and return it as an absolute URL."""
    candidate = None
    if node:
        candidate = _image_from_node(node)
    if not candidate:
        for attrs in (
            {"property": "og:image"},
            {"property": "og:image:url"},
            {"property": "og:image:secure_url"},
            {"name": "twitter:image"},
            {"name": "twitter:image:src"},
            {"itemprop": "image"},
        ):
            meta = soup.find("meta", attrs=attrs)
            if meta and meta.get("content"):
                candidate = meta["content"]
                break
    if not candidate:
        link = soup.find("link", rel="image_src")
        if link and link.get("href"):
            candidate = link["href"]
    if not candidate:
        candidate = _largest_article_image(soup)
    if not candidate:
        return None
    return urljoin(base_url, candidate.strip())


def extract_raw_ingredients(node: Optional[dict]) -> List[str]:
    """Verbatim ingredient lines from JSON-LD (exact source Hebrew, no rewriting)."""
    if not node:
        return []
    items = node.get("recipeIngredient") or node.get("ingredients")
    out: List[str] = []
    if isinstance(items, list):
        for it in items:
            if isinstance(it, str) and it.strip():
                out.append(it.strip())
    return out


_ISO_DUR = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def iso_duration_to_he(value: str) -> str:
    """'PT1H30M' -> 'שעה ו־30 דקות'. Non-ISO strings are returned unchanged."""
    s = (value or "").strip()
    if not s.startswith("P"):
        return s
    m = _ISO_DUR.fullmatch(s)
    if not m:
        return s
    days, hours, mins, _ = (int(x) if x else 0 for x in m.groups())
    hours += days * 24
    parts = []
    if hours == 1:
        parts.append("שעה")
    elif hours == 2:
        parts.append("שעתיים")
    elif hours > 2:
        parts.append(f"{hours} שעות")
    if mins:
        parts.append(f"{mins} דקות")
    return " ו־".join(parts) if parts else s


def _yield_to_str(value) -> str:
    if isinstance(value, (int, float)):
        return str(int(value))
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and value:
        return str(value[0]).strip()
    return ""


def extract_site_meta(node: Optional[dict]) -> Dict[str, str]:
    """Times and yield declared by the article (preferred over model guesses)."""
    meta: Dict[str, str] = {}
    if not node:
        return meta
    mapping = {
        "prep_time": "prepTime",
        "cook_time": "cookTime",
        "total_time": "totalTime",
    }
    for field_name, ld_key in mapping.items():
        val = node.get(ld_key)
        if isinstance(val, str) and val.strip():
            meta[field_name] = iso_duration_to_he(val)
    y = _yield_to_str(node.get("recipeYield"))
    if y:
        meta["servings"] = y
    return meta


def extract_text(html: str, url: str) -> str:
    extracted = trafilatura.extract(
        html, url=url, include_comments=False, include_tables=True, favor_recall=True,
    )
    if extracted and extracted.strip():
        return extracted.strip()
    soup = BeautifulSoup(html, "lxml")
    for bad in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        bad.decompose()
    return soup.get_text("\n", strip=True)


async def scrape(url: str, timeout: int, max_chars: int) -> ScrapedPage:
    html = await fetch_html(url, timeout)
    soup = BeautifulSoup(html, "lxml")

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    node = find_recipe_node(soup)
    json_ld = slim_recipe_ld(node) if node else None
    image_url = extract_image(soup, node, url)
    site_tags = extract_site_tags(soup, node)
    raw_ingredients = extract_raw_ingredients(node)
    site_meta = extract_site_meta(node)
    text = extract_text(html, url)

    if not text and not json_ld:
        raise ScrapeError("No readable content was found on the page.")

    if len(text) > max_chars:
        text = text[:max_chars]

    return ScrapedPage(
        url=url, title=title, text=text, json_ld=json_ld,
        image_url=image_url, site_tags=site_tags,
        raw_ingredients=raw_ingredients, site_meta=site_meta,
    )
