# Claude Code guidance

## Project layout

- `main.py` — entrypoint: runs the Scrapy spider via `CrawlerProcess`
- `tech_events/` — Scrapy project
  - `spiders/events.py` — fetches every URL in `events_list.yaml`
  - `pipelines.py` — `ClaudeExtractionPipeline` (HTML → structured JSON via Claude) + `YamlWriterPipeline` (upserts into `event/<year>.yaml`)
  - `items.py` — `PageItem` (event_name, url, html)
  - `settings.py` — Scrapy settings (delays, headers, pipeline order)
- `app.py` — Flask + Frozen-Flask app; groups events by `series` and passes to template
- `templates/index.html` — Jinja2 template with search/filter UI; SVG icons inlined via macros
- `event/<year>.yaml` — event data (both managed and manually added entries)
- `events_list.yaml` — source of truth for which events to track (url + name)
- `extractors.yaml` — auto-generated; XPath selectors per series for local extraction (skip Claude on re-runs)
- `pyproject.toml` / `uv.lock` — Python dependencies managed by uv
- `mise.toml` — installs uv via mise

## Key design decisions

**`events_list.yaml` is the source of truth for scope.** Only events listed there are ever written or updated. Manually added entries (entries whose `series` doesn't match any managed event) are preserved across re-runs via `_upsert()` in `pipelines.py`. Currently tracks ~100 events across India-specific and international categories (cloud/DevOps, security, AI/ML, language communities, hardware, networking, etc.).

**Three-stage pipeline:** Scrapy handles HTTP (retries, delays, robots.txt), `ClaudeExtractionPipeline` handles extraction in three escalating stages:
1. **`[local]`** — stored XPath selectors from `extractors.yaml` applied via `parsel`. Valid when at least one entry has a date; this is the "does the cached path still match and yield values" check. Whether the *values changed* is handled downstream by `_upsert()`.
2. **`[claude]`** — when local extraction misses, Claude re-extracts from the HTML Scrapy already fetched and returns both events and fresh XPath selectors in one call (`{"events": [...], "selectors": {...}}`). Because these XPaths are learned from the raw fetched HTML, they match future runs and re-enable the `[local]` path.
3. **`[web-search]`** — when stage 2 still finds no `{YEAR}` events (page 404/JS-only/relocated), `_web_search_extract()` calls Claude with the `web_search_20260209` + `web_fetch_20260209` server tools to locate the official page on the web, extract events, and report a `canonical_url`. Server-tool loops that return `stop_reason="pause_turn"` are re-sent (capped at `_MAX_TURN_CONTINUATIONS`).

`YamlWriterPipeline` upserts on `close_spider`.

**`extractors.yaml`:** auto-generated file storing per-series **XPath** expressions (parsel/lxml syntax). Has `container` (repeating-block XPath or null; relative field XPaths start with `.//`) and `fields` mapping each `TechEvent` field to an XPath (`/text()`, `/@href`) or null (for interpretation fields like `mode`, `scope`). A stale CSS-format file simply matches nothing and falls through to Claude, which rewrites it as XPath — self-healing, no migration needed.

**Web-search self-heal + URL auto-correction:** when stage 3's `canonical_url` differs from the fetched URL, the pipeline queues an `events_list.yaml` correction applied once in `close_spider` (`_apply_url_updates()`). `_retemplatize()` re-inserts a `{year}` template if the original entry used one, so the entry keeps working next year. Web searches are billed per query (separate from tokens) and their count is reported at the end of the run.

**`from_crawler()` pattern:** Both pipelines use `from_crawler()` to store the crawler and access `self.crawler.spider` — the `spider` argument on pipeline methods is deprecated in the current Scrapy version.

**Upsert dedup keys:** `(series.lower(), date)` pair AND `event_website` URL. An entry is skipped if either key has been seen, preventing duplicates when multiple events share a URL.

**`series` field:** stamped in `ClaudeExtractionPipeline` via `e["series"] = name` using the `events_list.yaml` name, keeping dedup stable regardless of how Claude phrases the event name.

**`{year}` URL templates:** URLs in `events_list.yaml` may contain `{year}` anywhere — path segment, subdomain, query param. The spider replaces it with the current year at crawl time (e.g. `https://{year}.pyconfhyd.org/` → `https://2026.pyconfhyd.org/`). Use this for events whose URL changes each year.

**Upsert across runs:** existing managed entries whose fresh scrape is skipped (e.g. 404, same-URL dedup) are preserved in `_upsert()` rather than dropped. Only entries explicitly returned by Claude as new/changed are updated.

**Date handling:** entries with missing or unparseable dates are treated as current-year (future) events by `_is_current_year()`.

**Model:** `claude-sonnet-4-6` at $3.00/$15.00 per 1M tokens (input/output).

## Running things

```bash
export ANTHROPIC_API_KEY=sk-ant-...
uv run python main.py            # fetch events → event/<year>.yaml
uv run scrapy crawl events       # equivalent, via Scrapy CLI
uv run python app.py             # dev server on :5000
uv run python app.py freeze      # build static site → build/
```

## Git commits

Never add a `Co-Authored-By: Claude` trailer to commit messages.

## Colour palette

- Danube `#5992c6`
- Cocoa Brown `#31241f`
- Shilo `#e9b8c9`
- Torea Bay `#0a2a92`