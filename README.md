# מנתח מתכונים · Hebrew Recipe Parser

A small web service that scrapes a Hebrew recipe page, sends the cleaned text to
a model running in your **local Ollama**, returns the recipe as structured JSON
(title, description, tags, servings/times, **ingredients name+amount+unit in
Hebrew**, ordered **stages**), and optionally **saves it straight into Mealie**.
The web UI shows live, step-by-step status with elapsed and total timing.

```
┌──────────┐    /api     ┌──────────┐   scrape + prompt   ┌──────────┐
│ Web UI   │ ──────────▶ │ FastAPI  │ ──────────────────▶ │  Ollama  │
│ (nginx)  │  NDJSON ◀── │ backend  │ ◀──── JSON ──────── │ (host)   │
└──────────┘  status     └────┬─────┘                     └──────────┘
                              │ create + PATCH
                              ▼
                         ┌──────────┐
                         │  Mealie  │ (host :1189)
                         └──────────┘
```

## Requirements

- Docker + Docker Compose
- **Ollama** running on your host with a Hebrew-capable model pulled:
  ```bash
  ollama pull gemma2:9b        # or aya:8b / qwen2.5:14b / mistral-nemo
  ```
- *(optional)* **Mealie** running on the same host (e.g. port `1189`) plus an
  API token from **Mealie → Settings → API Tokens**.

## Run

```bash
cp .env.example .env          # set MEALIE_API_TOKEN if you want Mealie saving
docker compose up --build
```

Open **http://localhost:8080**, paste a recipe URL, press **פענוח**. The header
pills show whether Ollama and Mealie are reachable; the **שמירה ב‑Mealie** toggle
appears (and auto-enables) once Mealie is configured and reachable.

### Enabling Mealie

1. In Mealie go to **Settings → API Tokens**, create a token, copy it.
2. Put it in `.env` as `MEALIE_API_TOKEN=...` and set `MEALIE_URL` /
   `MEALIE_PUBLIC_URL` if your port isn't `1189`.
3. `docker compose up -d --build`. Leave the token empty to disable sending.

## How it works

1. **Scrape** (`backend/app/scraper.py`) — fetches the page with a browser-like
   User-Agent, extracts the main article text with `trafilatura` (boilerplate
   removed, BeautifulSoup fallback), and additionally pulls any embedded
   `schema.org/Recipe` JSON-LD as a strong hint for the model.
2. **Parse** (`backend/app/parser.py`) — calls Ollama's chat API with the JSON
   schema in the `format` field, so the model is grammar-constrained to emit
   valid JSON. A system prompt forces it to keep all text in Hebrew, split each
   ingredient into amount / unit / name, and order the stages. Temperature 0.
3. **Save to Mealie** (`backend/app/mealie.py`, optional) — `POST /api/recipes`
   with the name to create the recipe and get its slug, then `PATCH
   /api/recipes/{slug}` with the full data. Tags are resolved/created against
   `/api/organizers/tags` (best-effort; failures there don't abort the save).
4. **Return** — validated against Pydantic models and sent back to the UI.

The UI calls **`POST /api/parse/stream`**, which streams NDJSON status events
(`fetch` → `parse` → `mealie`) so each step shows live with its own duration,
a ticking elapsed timer, and a final total time.

### How fields map to Mealie

| Parsed field | Mealie field |
|---|---|
| `title` | `name` |
| `description` | `description` |
| `servings` | `recipeYield` |
| `prep_time` / `cook_time` / `total_time` | `prepTime` / `performTime` / `totalTime` |
| `ingredients` | `recipeIngredient` (each as a `note`, `disableAmount: true`) |
| `stages` | `recipeInstructions` (`text`) |
| `tags` | `tags` (matched/created in `/api/organizers/tags`) |
| `source_url` | `orgURL` |

Ingredients go in the **note** field on purpose: Mealie's structured
quantity/unit/food fields need pre-existing food & unit IDs, and sending them by
name either fails or pollutes the food database. Notes preserve the exact Hebrew
text. To get scalable structured amounts later, open the recipe in Mealie and
run its **Parse** action.

## API

```bash
# parse only
curl -s http://localhost:8080/api/parse \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/some-hebrew-recipe"}' | jq

# parse and save to Mealie
curl -s http://localhost:8080/api/parse \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com/recipe", "send_to_mealie": true}' | jq
```

There is also `POST /api/parse/stream` (same body) which returns an NDJSON
stream of `{"event":"status",...}` lines followed by a final
`{"event":"result","recipe":{...},"total_ms":...}`.

Response shape:

```json
{
  "title": "עוגת שוקולד",
  "description": "...",
  "tags": ["קינוח", "אפייה"],
  "servings": "8",
  "prep_time": "20 דקות",
  "cook_time": "40 דקות",
  "total_time": "שעה",
  "ingredients": [
    { "name": "קמח לבן", "amount": "2", "unit": "כוסות" },
    { "name": "ביצים",   "amount": "3", "unit": "" }
  ],
  "stages": [
    { "step": 1, "instruction": "לחמם תנור ל‑180 מעלות." }
  ],
  "source_url": "https://...",
  "model": "gemma2:9b"
}
```

`GET /api/health` reports Ollama reachability and whether the model is pulled.

## Configuration (.env)

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_HOST` | `http://host.docker.internal:11434` | Ollama base URL |
| `OLLAMA_MODEL` | `gemma2:9b` | Model tag (must be pulled) |
| `OLLAMA_NUM_CTX` | `8192` | Context window |
| `OLLAMA_TIMEOUT` | `300` | Model timeout (s) |
| `MAX_CONTENT_CHARS` | `14000` | Cap on scraped text sent to the model |
| `MEALIE_URL` | `http://host.docker.internal:1189` | Mealie URL the backend calls |
| `MEALIE_PUBLIC_URL` | `http://localhost:1189` | Mealie URL for browser links |
| `MEALIE_API_TOKEN` | _(empty)_ | Mealie API token; empty disables saving |
| `MEALIE_GROUP` | `home` | Group slug used to build recipe links |
| `WEB_PORT` | `8080` | Host port for the web UI |

## Notes & tuning

- **Model choice matters most.** Hebrew quality varies a lot between models. If
  ingredients come back translated or garbled, try a stronger/Hebrew-tuned model
  and re-run.
- To run **Ollama inside compose** instead of on the host, uncomment the
  `ollama` service in `docker-compose.yml` and set
  `OLLAMA_HOST=http://ollama:11434`.
- Some sites block scraping or render content with JavaScript; those return a
  422 with an explanation. JSON-LD extraction helps a lot when present.
