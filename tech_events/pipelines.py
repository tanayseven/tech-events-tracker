"""
Scrapy item pipelines.

ClaudeExtractionPipeline  — tries stored CSS selectors first; falls back to
                             Claude when selectors are missing or stale, and
                             updates extractors.yaml with new selectors.
YamlWriterPipeline        — after all items are processed, upserts results into
                             event/<year>.yaml.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Literal

import yaml
from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

YEAR = dt.datetime.now().year
OUTPUT_FILE = Path(f"event/{YEAR}.yaml")
EXTRACTORS_FILE = Path("extractors.yaml")
MAX_HTML_CHARS = 80_000

MODEL = "claude-sonnet-4-6"
_COST_PER_M_INPUT  = 3.00
_COST_PER_M_OUTPUT = 15.00


# ── Schema ────────────────────────────────────────────────────────────────────

class TechEvent(BaseModel):
    name: str | None = None
    series: str | None = None
    mode: Literal["online", "in-person", "hybrid"] | None = None
    venue: str | None = None
    country: str | None = None
    topic: str | None = None
    description: str | None = None
    scope: Literal["local meetup", "national conference", "international conference"] | None = None
    date: str | None = None
    event_website: str | None = None
    ticket_booking_link: str | None = None
    ticket_status: Literal["available", "sold_out", "not_opened"] | None = None
    cfp_submission_link: str | None = None
    cfp_deadline: str | None = None
    source_url: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean_html(html: str) -> str:
    """Strip scripts/styles from HTML, returning cleaned HTML with DOM structure intact."""
    try:
        from lxml import html as lhtml
        doc = lhtml.fromstring(html)
        for tag in doc.iter("script", "style", "noscript", "head"):
            parent = tag.getparent()
            if parent is not None:
                parent.remove(tag)
        return lhtml.tostring(doc, encoding="unicode", method="html")
    except Exception:
        text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
        return text


def _extract_json(text: str) -> str | None:
    """Extract the first JSON object or array from text, handling fenced blocks."""
    fence = re.search(r"```(?:json)?\s*([{\[].*?[}\]])\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)
    first_brace   = text.find("{")
    first_bracket = text.find("[")
    if first_brace == -1 and first_bracket == -1:
        return None
    if first_brace == -1:
        start = first_bracket
    elif first_bracket == -1:
        start = first_brace
    else:
        start = min(first_brace, first_bracket)
    open_char  = text[start]
    close_char = "}" if open_char == "{" else "]"
    depth = 0
    for i in range(start, len(text)):
        if text[i] == open_char:
            depth += 1
        elif text[i] == close_char:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _is_current_year(entry: dict) -> bool:
    date_str = (entry.get("date") or "").strip()
    if not date_str:
        return True
    start = date_str.split(" to ")[0].strip()
    try:
        return dt.date.fromisoformat(start[:10]).year == YEAR
    except ValueError:
        return True


def _load_existing() -> list[dict]:
    if not OUTPUT_FILE.exists():
        return []
    return yaml.safe_load(OUTPUT_FILE.read_text()) or []


def _load_extractors() -> dict:
    if not EXTRACTORS_FILE.exists():
        return {}
    return yaml.safe_load(EXTRACTORS_FILE.read_text()) or {}


def _apply_extractor(html: str, extractor: dict) -> list[dict] | None:
    """
    Apply stored CSS selectors to raw HTML.
    Returns a list of dicts (possibly with null values), or None if extraction
    produced nothing useful (no nodes matched the container, or all fields null).
    """
    try:
        from parsel import Selector
    except ImportError:
        return None

    sel = Selector(text=html)
    field_selectors: dict = extractor.get("fields", {})
    container_css: str | None = extractor.get("container")

    def _extract_node(node) -> dict:
        row: dict = {}
        for field, css in field_selectors.items():
            if not css:
                row[field] = None
                continue
            value = node.css(css).get()
            row[field] = value.strip() if value else None
        return row

    if container_css:
        nodes = sel.css(container_css)
        if not nodes:
            return None
        return [_extract_node(n) for n in nodes]

    row = _extract_node(sel)
    # Only return if at least one field was populated
    return [row] if any(v for v in row.values()) else None


def _extraction_valid(entries: list[dict]) -> bool:
    """Extraction is considered valid when at least one entry has a date."""
    return any(e.get("date") for e in entries)


def _upsert(
    existing: list[dict],
    fresh_by_event: dict[str, list[dict]],
    managed_names: set[str],
) -> tuple[list[dict], int, int, int]:
    managed_lower = {n.lower() for n in managed_names}

    existing_managed: dict[tuple, dict] = {}
    untouched: list[dict] = []
    for e in existing:
        series = (e.get("series") or "").lower()
        if series in managed_lower:
            existing_managed[(series, e.get("date") or "")] = e
        else:
            untouched.append(e)

    seen: set[tuple] = {
        ((e.get("series") or e.get("name") or "").lower(), e.get("date") or "")
        for e in untouched
    }
    # Seed URL dedup from untouched only — existing_managed URLs must not block
    # fresh entries from matching and refreshing the same event.
    seen_urls: set[str] = {
        e["event_website"]
        for e in untouched
        if e.get("event_website")
    }

    updated = list(untouched)
    refreshed_keys: set[tuple] = set()
    n_new = n_changed = n_unchanged = 0

    for name, entries in fresh_by_event.items():
        for entry in entries:
            url = entry.get("event_website") or ""
            if url and url in seen_urls:
                continue
            key = (name.lower(), entry.get("date") or "")
            if key in seen:
                continue
            seen.add(key)
            if url:
                seen_urls.add(url)
            prior = existing_managed.get(key)
            if prior is None:
                n_new += 1
                updated.append(entry)
            elif prior == entry:
                n_unchanged += 1
                updated.append(prior)
                refreshed_keys.add(key)
            else:
                n_changed += 1
                updated.append(entry)
                refreshed_keys.add(key)

    # Preserve existing managed entries not covered by this scrape run.
    for key, entry in existing_managed.items():
        if key not in refreshed_keys:
            updated.append(entry)

    return updated, n_new, n_changed, n_unchanged


# ── Pipelines ─────────────────────────────────────────────────────────────────

class ClaudeExtractionPipeline:
    @classmethod
    def from_crawler(cls, crawler):
        pipeline = cls()
        pipeline.crawler = crawler
        return pipeline

    def open_spider(self):
        spider = self.crawler.spider
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("Set ANTHROPIC_API_KEY before running.")
        self._client = AsyncAnthropic(api_key=api_key)
        self._extractors = _load_extractors()
        self._extractors_dirty = False
        spider.fresh_by_event: dict[str, list[dict]] = {}
        spider.total_in_tok = 0
        spider.total_out_tok = 0
        self._fields = ", ".join(TechEvent.model_fields.keys())

    def _save_extractors(self):
        EXTRACTORS_FILE.write_text(
            yaml.safe_dump(self._extractors, sort_keys=True, allow_unicode=True)
        )

    async def process_item(self, item):
        spider = self.crawler.spider
        name = item["event_name"]
        url  = item["url"]

        # ── 1. Try local CSS selectors ────────────────────────────────────────
        extractor = self._extractors.get(name)
        if extractor:
            local = _apply_extractor(item["html"], extractor)
            if local and _extraction_valid(local):
                validated: list[dict] = []
                for entry in local:
                    entry["series"] = name
                    try:
                        validated.append(TechEvent.model_validate(entry).model_dump())
                    except ValidationError:
                        pass
                year_entries = [e for e in validated if _is_current_year(e)]
                if year_entries:
                    spider.fresh_by_event[name] = year_entries
                    print(f"  {name}: {len(year_entries)} event(s) [local]")
                    return item

        # ── 2. Fall back to Claude; request events + new selectors ────────────
        html_clean = _clean_html(item["html"])[:MAX_HTML_CHARS]

        prompt = f"""Analyse the cleaned HTML below from "{name}" ({url}) and extract all {YEAR} events.

Cleaned HTML (scripts and styles removed):
{html_clean}

Return a JSON object with exactly two keys:

1. "events" — array of event objects for {YEAR}. One object per event occurrence.
   For recurring series (monthly/quarterly meetups), list EVERY occurrence separately.
   Include both past and upcoming {YEAR} events; exclude anything outside {YEAR}.
   Fields per object (null if unknown): {self._fields}
   Rules:
   - mode: "online" | "in-person" | "hybrid"
   - scope: "local meetup" | "national conference" | "international conference"
   - date: "YYYY-MM-DD" or "YYYY-MM-DD to YYYY-MM-DD"
   - ticket_status: "available" | "sold_out" | "not_opened" | null
   - cfp_deadline: "YYYY-MM-DD" or null
   - source_url: page that confirms the date/venue
   Empty array [] if no {YEAR} events found.

2. "selectors" — CSS selectors (parsel/Scrapy syntax) that would extract each field
   directly from the HTML above, so future scrapes can skip the Claude call.
   - Use "::text" for text nodes, "::attr(href)" for attributes.
   - For fields requiring interpretation (mode, scope, description, ticket_status,
     topic, series), set to null — they can't be reliably CSS-selected.
   - If events appear as repeating list items, set "_container" to the CSS selector
     for the repeating block; all other selectors are then relative to that block.
   - Set any field to null if no reliable selector exists.

Example response format:
{{
  "events": [{{"name": "...", "date": "{YEAR}-...", "venue": "...", ...}}],
  "selectors": {{
    "_container": null,
    "name": "h1.event-title::text",
    "date": "#event-date::text",
    "venue": ".location span::text",
    "event_website": "a.register::attr(href)",
    "mode": null,
    "country": ".country::text",
    "scope": null,
    "description": null,
    "ticket_booking_link": "a.tickets::attr(href)",
    "ticket_status": null,
    "cfp_submission_link": "a.cfp::attr(href)",
    "cfp_deadline": ".cfp-deadline::text",
    "source_url": null
  }}
}}
"""

        response = await self._client.messages.create(
            model=MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        spider.total_in_tok  += response.usage.input_tokens
        spider.total_out_tok += response.usage.output_tokens

        text = next(
            (b.text for b in response.content if b.type == "text"), ""
        ).strip()

        extracted = _extract_json(text)
        if not extracted:
            spider.logger.warning("%s: no JSON in response", name)
            spider.fresh_by_event[name] = []
            return item

        try:
            parsed = json.loads(extracted)
        except json.JSONDecodeError as exc:
            spider.logger.warning("%s: JSON parse error: %s", name, exc)
            spider.fresh_by_event[name] = []
            return item

        # Accept both new {events, selectors} format and plain array (fallback)
        if isinstance(parsed, dict) and "events" in parsed:
            raw_entries = parsed["events"]
            new_selectors = parsed.get("selectors") or {}
            if new_selectors:
                container = new_selectors.pop("_container", None)
                self._extractors[name] = {
                    "container": container,
                    "fields": {k: v for k, v in new_selectors.items()},
                }
                self._extractors_dirty = True
        elif isinstance(parsed, list):
            raw_entries = parsed
        else:
            raw_entries = []

        validated = []
        for entry in raw_entries:
            try:
                validated.append(TechEvent.model_validate(entry).model_dump())
            except ValidationError as exc:
                spider.logger.warning("%s: skipped invalid entry: %s", name, exc)

        year_entries = [e for e in validated if _is_current_year(e)]
        for e in year_entries:
            e["series"] = name

        spider.fresh_by_event[name] = year_entries
        print(f"  {name}: {len(year_entries)} event(s) [claude]")
        return item

    def close_spider(self):
        if self._extractors_dirty:
            self._save_extractors()


class YamlWriterPipeline:
    @classmethod
    def from_crawler(cls, crawler):
        pipeline = cls()
        pipeline.crawler = crawler
        return pipeline

    def close_spider(self):
        spider  = self.crawler.spider
        fresh   = getattr(spider, "fresh_by_event", {})
        in_tok  = getattr(spider, "total_in_tok", 0)
        out_tok = getattr(spider, "total_out_tok", 0)

        if not fresh:
            return

        managed_names = set(fresh.keys())
        existing = _load_existing()
        merged, n_new, n_changed, n_unchanged = _upsert(existing, fresh, managed_names)

        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_FILE.write_text(
            yaml.safe_dump(merged, sort_keys=False, allow_unicode=True)
        )

        cost = (in_tok * _COST_PER_M_INPUT + out_tok * _COST_PER_M_OUTPUT) / 1_000_000
        total = sum(len(v) for v in fresh.values())

        print(f"\nScraped {total} event(s) → wrote {len(merged)} to {OUTPUT_FILE}")
        print(f"New: {n_new}  Changed: {n_changed}  Unchanged: {n_unchanged}")
        if in_tok or out_tok:
            print(f"Tokens: {in_tok:,} in / {out_tok:,} out  |  Cost: ${cost:.4f}")