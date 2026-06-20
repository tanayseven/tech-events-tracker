# Claude Code guidance

## Project layout

- `main.py` — AI scraper: calls Claude with `web_search` tool, writes `event/<year>.yaml`
- `app.py` — Flask + Frozen-Flask app; groups events by `series` and passes to template
- `templates/index.html` — Jinja2 template with search/filter UI
- `event/<year>.yaml` — event data (both managed and manually added entries)
- `pyproject.toml` / `uv.lock` — Python dependencies managed by uv
- `mise.toml` — installs uv via mise

## Key design decisions

**`CONFERENCE_LIST` is the source of truth for scope.** Only conferences listed there are ever written or updated by `main.py`. Manually added events (entries whose name doesn't match any managed conference) are preserved across re-runs via the `upsert()` function.

**Upsert strategy:** `upsert()` seeds the deduplication `seen` set only from *unmanaged* entries, so managed conference events are always fully replaced by the latest fetch. Dedup key is `(name.lower(), date)`.

**`series` field:** stamped by `main.py` via `e.setdefault("series", name)` using the `CONFERENCE_LIST` value. Recurring meetups (e.g. Expert Talks) return multiple objects from the same search, all sharing the same `series`.

**Web search:** uses `web_search_20250305` with `max_uses: 3` to cap token consumption. Model is `claude-haiku-4-5` (cheapest tier).

**Date handling:** entries with missing or unparseable dates are treated as current-year (future) events by `is_current_year()`.

## Running things

```bash
export ANTHROPIC_API_KEY=sk-ant-...
uv run python main.py        # refresh event data
uv run python app.py         # dev server on :5000
uv run python app.py freeze  # build static site → build/
```

## Colour palette

- Danube `#5992c6`
- Cocoa Brown `#31241f`
- Shilo `#e9b8c9`
- Torea Bay `#0a2a92`