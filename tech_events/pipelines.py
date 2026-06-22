"""
Scrapy item pipelines.

ClaudeExtractionPipeline  — sends fetched HTML to Claude, gets back structured
                             event dicts, stores them on the spider.
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
    """Strip scripts/styles/head and return readable text content."""
    try:
        from lxml.html import fromstring
        doc = fromstring(html)
        for tag in doc.iter("script", "style", "noscript", "head"):
            parent = tag.getparent()
            if parent is not None:
                parent.remove(tag)
        return doc.text_content()
    except Exception:
        text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
        return text


def _extract_json_array(text: str) -> str | None:
    fence = re.search(r"```(?:json)?\s*(\[.*?])\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)
    start = text.find("[")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
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
        spider.fresh_by_event: dict[str, list[dict]] = {}
        spider.total_in_tok = 0
        spider.total_out_tok = 0
        self._fields = ", ".join(TechEvent.model_fields.keys())

    async def process_item(self, item):
        spider = self.crawler.spider
        name = item["event_name"]
        url  = item["url"]
        html = _clean_html(item["html"])[:MAX_HTML_CHARS]

        prompt = f"""Analyse the page content below from "{name}" ({url}) and extract all {YEAR} events.

Page content:
{html}

Return a JSON array only — no markdown, no commentary. One object per event occurrence.
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

        extracted = _extract_json_array(text)
        if not extracted:
            spider.logger.warning("%s: no JSON array in response", name)
            spider.fresh_by_event[name] = []
            return item

        try:
            raw_entries = json.loads(extracted)
        except json.JSONDecodeError as exc:
            spider.logger.warning("%s: JSON parse error: %s", name, exc)
            spider.fresh_by_event[name] = []
            return item

        validated: list[dict] = []
        for entry in raw_entries:
            try:
                validated.append(TechEvent.model_validate(entry).model_dump())
            except ValidationError as exc:
                spider.logger.warning("%s: skipped invalid entry: %s", name, exc)

        year_entries = [e for e in validated if _is_current_year(e)]
        for e in year_entries:
            e["series"] = name

        spider.fresh_by_event[name] = year_entries
        print(f"  {name}: {len(year_entries)} event(s)")
        return item


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
        print(f"Tokens: {in_tok:,} in / {out_tok:,} out  |  Cost: ${cost:.4f}")