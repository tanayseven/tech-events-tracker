# How scraping works

The scraper runs in two stages: **Scrapy fetches**, **Claude extracts**.

## 1. Spider (`tech_events/spiders/events.py`) — HTTP layer

```
events_list.yaml  →  EventsSpider.start()  →  one Request per URL
```

- Reads every entry from `events_list.yaml` at startup.
- Replaces `{year}` in URLs with the current year (e.g. `ep{year}.europython.eu` → `ep2026.europython.eu`).
- Fires a `scrapy.Request` for each URL with `dont_verify_ssl: True` (handles cert mismatches like PyConF Hyderabad).
- On success → `parse()` wraps the raw HTML in a `PageItem(event_name, url, html)` and yields it downstream.
- On failure → `on_error()` logs the error; that event is silently skipped.

Scrapy handles all HTTP concerns: retries, rate limiting (`DOWNLOAD_DELAY = 1s`), redirects, robots.txt bypass.

## 2. `ClaudeExtractionPipeline` — extraction layer

Each `PageItem` flows into `process_item()`, which tries two paths in order:

### Path A — local CSS selectors (fast, free)

If `extractors.yaml` has an entry for this series, `_apply_extractor()` runs the
stored CSS selectors directly against the raw HTML using `parsel` (Scrapy's built-in
selector library). If the result contains at least one entry with a `date`, the item
is accepted and Claude is not called.

```
extractors.yaml  →  parsel CSS selectors  →  validated entries  →  done
```

### Path B — Claude fallback (when selectors are missing or stale)

Triggered when no extractor exists yet, or when local extraction returns no dates.

1. **Clean HTML** — strips `<script>`, `<style>`, `<noscript>`, `<head>` via lxml, leaving readable text. Truncated to 80,000 chars.
2. **Prompt Claude** (`claude-sonnet-4-6`) — asks for a JSON object with two keys:
   - `"events"` — array of event objects for the current year
   - `"selectors"` — CSS selector per field so future scrapes can skip Claude
3. **Store selectors** — `extractors.yaml` is updated with the new selectors for this series (written once after all items finish).
4. **Parse events** — JSON parsed, validated against the `TechEvent` Pydantic model, filtered to current year.
5. **Stamp `series`** — sets `e["series"] = name` from `events_list.yaml` so dedup is stable regardless of how Claude phrases the event name.

```
HTML  →  Claude (events + selectors)  →  update extractors.yaml  →  validated entries
```

The console label `[local]` or `[claude]` shows which path was taken per event.

### `extractors.yaml` format

```yaml
"PyCon India":
  container: null           # CSS selector for repeating event block; null = whole page
  fields:
    date: "#event-date::text"
    name: "h1.title::text"
    venue: ".venue span::text"
    event_website: "a.register::attr(href)"
    mode: null              # requires interpretation — always extracted by Claude
    scope: null
    ...
```

Fields set to `null` (like `mode`, `scope`, `description`) can't be reliably
CSS-selected, so they remain `null` on local runs. On the next Claude call they
will be populated again.

## 3. `YamlWriterPipeline` — upsert layer

Runs once after all items are processed (`close_spider`):

```
fresh_by_event  +  existing event/2026.yaml  →  _upsert()  →  event/2026.yaml
```

`_upsert()` logic:
- Splits existing entries into **managed** (series matches `events_list.yaml`) and **untouched** (manually added).
- Deduplicates fresh entries by `(series.lower(), date)` key AND `event_website` URL — both must be unseen.
- URL dedup is seeded only from `untouched` entries, so existing managed entries don't block their own refresh.
- Tracks which existing managed entries were matched by a fresh scrape (`refreshed_keys`); unmatched ones are re-appended unchanged (preserves data when a site 404s on re-run).
- Writes the merged list back to `event/<year>.yaml`.

## Data flow summary

```
events_list.yaml
      │
      ▼
EventsSpider.start()              — one Request per URL, {year} substituted
      │
      ▼  (Scrapy HTTP)
EventsSpider.parse()              — wraps HTML in PageItem
      │
      ▼  (ITEM_PIPELINES order 200)
ClaudeExtractionPipeline
  ├─ extractors.yaml hit?
  │     yes → parsel CSS selectors → date present? → done  [local]
  │     no  ↓
  └─ Claude (events + selectors) → update extractors.yaml  [claude]
      │
      ▼  (ITEM_PIPELINES order 300, on close_spider)
YamlWriterPipeline._upsert()
      │
      ▼
event/<year>.yaml
```