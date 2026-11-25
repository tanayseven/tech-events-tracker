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
## Checking `events_list.yaml` URLs (`verify_urls.py`)

A separate, report-only tool that catches stale URLs the scraper wouldn't notice
(e.g. an old page that still loads but is no longer the official site). It needs
no API key: searches go through DuckDuckGo via the `ddgs` package.

```
events_list.yaml name  →  ddgs.text('"<name>" <year> conference')  →  compare hosts  →  url_report.yaml
```

- **OK** — the stored URL's host (or a parent/child host, e.g. `2026.pyconfhyd.org` ↔ `pyconfhyd.org`)
  appears in the top `--results` hits (default 5). Hosts are compared exactly, so
  `us.pycon.org` does not confirm `in.pycon.org`.
- **CHANGED** — it doesn't, and a hit that isn't a ticketing/listing/social site
  (see `AGGREGATORS`) has a domain resembling the event name (`_resembles()`: a
  distinctive name word, the acronym, or 2+ name words in the host). That hit is
  suggested, keeping `{year}` templates via `_retemplatize()`.
- **UNSURE** — it doesn't, but no hit looks like the event's own site (only articles
  or listings). Nothing is suggested or applied; it's listed in the report/PR body.
- **NOT FOUND** / **ERROR** — nothing usable, or the search failed after retries.

ddgs scrapes search engines, so it can be throttled; the script waits `--delay`
seconds between queries (default 2) and retries with backoff. Throttling sometimes
surfaces as "No results found.", so re-run ERROR entries with `--only` before trusting them.
Suggestions come from search ranking — always verify before editing `events_list.yaml`.
Matching is host-level, so on shared hosts (e.g. `events.linuxfoundation.org`) a hit for a
sibling event also counts as OK.

### Daily PR (`.github/workflows/verify-urls.yml`)

Runs every day at 03:30 UTC (and on demand via *Run workflow*):

```
verify_urls.py --apply --summary pr-body.md
   → events_list.yaml changed?  no → done (no PR)
                                yes → peter-evans/create-pull-request
                                      branch bot/verify-urls, body = pr-body.md
```

- `--apply` only writes a CHANGED suggestion when its URL is **not already in
  `events_list.yaml`**, so the PR contains genuinely new URLs only.
- The same branch is force-updated each run, so there is at most one open PR.
- `verify_urls_ignore.yaml` is the reviewer's feedback loop: `skip` an event whose
  search results are unreliable, or `reject` a specific suggested site.
- Circuit breaker: after 5 consecutive failed searches the run stops searching;
  changes found so far are still proposed. If every search fails the job fails, so
  a blocked runner IP is visible instead of silently producing no PRs.

Requires the repo setting *Settings → Actions → General → Allow GitHub Actions to
create and approve pull requests*.
