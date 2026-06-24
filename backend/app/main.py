"""FastAPI service: scrape a Hebrew recipe URL, parse it, optionally push to Mealie.

Two entry points:
  POST /api/parse         -> single JSON response (good for scripting / curl)
  POST /api/parse/stream  -> NDJSON stream of live status events, then the result
                             (used by the web UI for verbose progress + timing)
"""
from __future__ import annotations

import json
import logging
import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse

from .config import settings
from .mealie import MealieError, check_mealie, download_image, push_to_mealie
from .models import ParseRequest, RecipeResult
from .parser import (
    OllamaError, apply_site_meta, check_ollama, merge_site_tags, parse_recipe,
)
from .scraper import ScrapeError, scrape

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("recipe-parser")

log.info(
    "Connection targets -> OLLAMA_HOST=%s | MEALIE_URL=%s (token %s)",
    settings.ollama_host,
    settings.mealie_url,
    "set" if settings.mealie_token else "EMPTY",
)

app = FastAPI(title="Hebrew Recipe Parser", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _ms(since: float) -> int:
    return int((time.perf_counter() - since) * 1000)


@app.get("/api/health")
async def health() -> dict:
    ollama = await check_ollama()
    mealie = await check_mealie()
    return {"status": "ok", **ollama, **mealie}


@app.get("/api/image")
async def image_proxy(url: str = Query(...), referer: str = Query("")):
    """Fetch an image inside the backend and stream it back.

    The UI previews images through this endpoint, so what you see is exactly what
    the container could download — independent of the browser's own network.
    """
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Enter a valid http(s) image URL.")
    content, ctype, err = await download_image(url, referer)
    if content is None:
        raise HTTPException(status_code=502, detail=err or "could not fetch image")
    media = (ctype or "image/jpeg").split(";")[0].strip() or "image/jpeg"
    return Response(content=content, media_type=media,
                    headers={"Cache-Control": "no-store"})


def _validate_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Enter a valid http(s) URL.")
    return url


@app.post("/api/parse", response_model=RecipeResult)
async def parse(req: ParseRequest) -> RecipeResult:
    url = _validate_url(req.url)

    try:
        page = await scrape(url, settings.fetch_timeout, settings.max_content_chars)
    except ScrapeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        recipe = await parse_recipe(page, req.model)
    except OllamaError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    recipe.tags = merge_site_tags(recipe.tags, page.site_tags)
    apply_site_meta(recipe, page.site_meta)
    result = RecipeResult(
        **recipe.model_dump(), source_url=url,
        model=req.model or settings.ollama_model, image_url=page.image_url or "",
    )

    if req.send_to_mealie:
        try:
            result.mealie = {
                "ok": True,
                **await push_to_mealie(recipe, url, page.image_url or "", page.raw_ingredients),
            }
        except MealieError as exc:
            result.mealie = {"ok": False, "error": str(exc)}

    log.info("Parsed %s -> %d ingredients, %d stages",
             url, len(recipe.ingredients), len(recipe.stages))
    return result


async def _pipeline(url: str, send_to_mealie: bool, model: str | None = None):
    """Yield NDJSON status events as the work progresses, then a final result."""
    def ev(**kw) -> str:
        return json.dumps(kw, ensure_ascii=False) + "\n"

    t_total = time.perf_counter()

    # --- fetch & scrape ---
    yield ev(event="status", step="fetch", state="running")
    t = time.perf_counter()
    try:
        page = await scrape(url, settings.fetch_timeout, settings.max_content_chars)
    except ScrapeError as exc:
        yield ev(event="status", step="fetch", state="error", message=str(exc))
        yield ev(event="error", detail=str(exc))
        return
    note = "נמצאו נתוני schema.org מובנים" if page.json_ld else "התוכן חולץ מהעמוד"
    yield ev(event="status", step="fetch", state="done", ms=_ms(t), note=note)

    # --- parse with the model ---
    yield ev(event="status", step="parse", state="running")
    t = time.perf_counter()
    try:
        recipe = await parse_recipe(page, model)
    except OllamaError as exc:
        yield ev(event="status", step="parse", state="error", message=str(exc))
        yield ev(event="error", detail=str(exc))
        return
    yield ev(event="status", step="parse", state="done", ms=_ms(t),
             ingredients=len(recipe.ingredients), stages=len(recipe.stages))

    recipe.tags = merge_site_tags(recipe.tags, page.site_tags)
    apply_site_meta(recipe, page.site_meta)
    result = RecipeResult(
        **recipe.model_dump(), source_url=url,
        model=model or settings.ollama_model, image_url=page.image_url or "",
    )

    # --- push to Mealie (optional) ---
    if send_to_mealie:
        yield ev(event="status", step="mealie", state="running")
        t = time.perf_counter()
        try:
            info = await push_to_mealie(recipe, url, page.image_url or "", page.raw_ingredients)
        except MealieError as exc:
            yield ev(event="status", step="mealie", state="error", message=str(exc))
            result.mealie = {"ok": False, "error": str(exc)}
        else:
            yield ev(event="status", step="mealie", state="done", ms=_ms(t),
                     slug=info.get("slug"), image_found=info.get("image_found"),
                     image_set=info.get("image_set"),
                     image_error=info.get("image_error"), tags=info.get("tags_attached"))
            result.mealie = {"ok": True, **info}

    yield ev(event="result", recipe=result.model_dump(), total_ms=_ms(t_total))


@app.post("/api/parse/stream")
async def parse_stream(req: ParseRequest):
    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        async def bad():
            yield json.dumps(
                {"event": "error", "detail": "Enter a valid http(s) URL."},
                ensure_ascii=False,
            ) + "\n"
        return StreamingResponse(bad(), media_type="application/x-ndjson")

    return StreamingResponse(
        _pipeline(url, req.send_to_mealie, req.model),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )
