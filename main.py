"""
agent_search.py

Searches the web (via Claude's web_search tool) for a fixed list of named
tech events, keeps only current-year results, and upserts them into a YAML
file. Re-running updates changed fields and adds newly-announced dates --
it never adds events outside the list below.

Requirements:
    pip install anthropic pyyaml

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python agent_search.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import yaml
from anthropic import Anthropic
from pydantic import BaseModel, ValidationError
from typing import Literal

YEAR = dt.datetime.now().year
OUTPUT_FILE = f"event/{YEAR}.yaml"


def _load_event_list() -> dict[str, str]:
    raw = yaml.safe_load(Path("events_list.yaml").read_text()) or []
    return {
        entry["url"].replace("{year}", str(YEAR)): entry["name"]
        for entry in raw
    }


EVENT_LIST = _load_event_list()

MODEL = "claude-haiku-4-5"


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


def _extract_json_array(text: str) -> str | None:
    """Pull the first complete JSON array out of text that may contain prose or fences."""
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


# Pricing per million tokens for claude-haiku-4-5
_COST_PER_M_INPUT  = 1.00
_COST_PER_M_OUTPUT = 5.00


def search_event(
    client: Anthropic, name: str, url: str, today: dt.date
) -> tuple[list[dict], int, int, list[str]]:
    """Return (entries, input_tokens, output_tokens, warnings) for a single event series search."""

    warnings: list[str] = []

    fields_inline = ", ".join(TechEvent.model_fields.keys())
    prompt = f"""Find all {today.year} events for "{name}".

Start by fetching the official page: {url}
Then do at most one web search only if the page lacks {today.year} dates.

Return a JSON array only — no markdown, no commentary. One object per event occurrence.
For recurring series (monthly/quarterly meetups), list EVERY occurrence separately.
Include both past and upcoming {today.year} events; exclude anything outside {today.year}.

Fields per object (null if unknown): {fields_inline}

Rules:
- mode: "online" | "in-person" | "hybrid"
- scope: "local meetup" | "national conference" | "international conference"
- date: "YYYY-MM-DD" or "YYYY-MM-DD to YYYY-MM-DD"
- ticket_status: "available" | "sold_out" | "not_opened" | null
- cfp_deadline: "YYYY-MM-DD" or null
- source_url: page that confirms the date/venue

Empty array [] if no {today.year} events found.
"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        tools=[
            {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 2},
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 1},
        ],
        messages=[{"role": "user", "content": prompt}],
    )

    in_tok  = response.usage.input_tokens
    out_tok = response.usage.output_tokens

    text_blocks = [b.text for b in response.content if b.type == "text"]
    if not text_blocks:
        warnings.append(f"No text response, skipping.")
        return [], in_tok, out_tok, warnings

    raw = text_blocks[-1].strip()

    extracted = _extract_json_array(raw)
    if extracted is None:
        warnings.append(f"Could not find JSON array in response.")
        return [], in_tok, out_tok, warnings

    try:
        entries = json.loads(extracted)
    except json.JSONDecodeError as exc:
        warnings.append(f"JSON parse error: {exc}")
        return [], in_tok, out_tok, warnings

    if not isinstance(entries, list):
        warnings.append(f"Expected list, got {type(entries).__name__}.")
        return [], in_tok, out_tok, warnings

    validated: list[dict] = []
    for entry in entries:
        try:
            validated.append(TechEvent.model_validate(entry).model_dump())
        except ValidationError as exc:
            warnings.append(f"Skipped invalid entry: {exc}")

    return validated, in_tok, out_tok, warnings


def is_current_year(entry: dict, year: int) -> bool:
    date_str = (entry.get("date") or "").strip()
    if not date_str:
        return True  # no confirmed date yet -> assume current year
    start = date_str.split(" to ")[0].strip()
    try:
        return dt.date.fromisoformat(start[:10]).year == year
    except ValueError:
        return True  # unparseable date format -> assume current year


def load_existing(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text())
    return data or []


def upsert(
    existing: list[dict], fresh_by_event: dict[str, list[dict]]
) -> tuple[list[dict], int, int, int]:
    """Merge fresh entries into existing, writing only new or changed managed entries.

    Returns (merged, n_new, n_changed, n_unchanged).
    Unmanaged entries (not in EVENT_LIST) are always preserved as-is.
    """
    managed_names = [n.lower() for n in EVENT_LIST.values()]

    existing_managed: dict[tuple, dict] = {}
    untouched: list[dict] = []
    for e in existing:
        if any(m in (e.get("name") or "").lower() for m in managed_names):
            key = ((e.get("name") or "").lower(), e.get("date") or "")
            existing_managed[key] = e
        else:
            untouched.append(e)

    seen: set[tuple] = {
        ((e.get("name") or "").lower(), e.get("date") or "")
        for e in untouched
    }
    updated = list(untouched)
    n_new = n_changed = n_unchanged = 0

    for name in EVENT_LIST.values():
        for entry in fresh_by_event.get(name, []):
            key = ((entry.get("name") or "").lower(), entry.get("date") or "")
            if key in seen:
                continue
            seen.add(key)
            prior = existing_managed.get(key)
            if prior is None:
                n_new += 1
                updated.append(entry)
            elif prior == entry:
                n_unchanged += 1
                updated.append(prior)
            else:
                n_changed += 1
                updated.append(entry)

    return updated, n_new, n_changed, n_unchanged


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("Set ANTHROPIC_API_KEY before running this script.")

    client = Anthropic(api_key=api_key)
    today = dt.date.today()

    md: list[str] = [
        "# Tech Events Tracker — Run Report",
        f"*{dt.datetime.now():%Y-%m-%d %H:%M} · model: {MODEL}*",
        "",
        "## Design decisions",
        "",
        "- **web_fetch first** — each event's known URL is fetched directly before "
        "falling back to web search, targeting exactly the right page instead of "
        "broad search results and substantially reducing input tokens.",
        "- **max_uses caps** — `web_fetch` is capped at 2 uses and `web_search` at 1 "
        "per event to prevent runaway token usage.",
        "- **Pydantic validation** — every AI-returned entry is validated against "
        "`TechEvent` before being written to YAML, enforcing enums on `mode`, `scope`, "
        "and `ticket_status` and discarding malformed objects.",
        "- **Upsert strategy** — events in `events_list.yaml` are fully replaced on "
        "each run; entries not in that list (manually added) are preserved untouched.",
        "- **Year filter** — entries with missing or unparseable dates are assumed to "
        "be current-year future events and are kept.",
        "",
        "## Run log",
        "",
    ]

    fresh_by_event: dict[str, list[dict]] = {}
    total_in_tok = 0
    total_out_tok = 0

    for url, name in EVENT_LIST.items():
        print(f"Searching: {name}")
        entries, in_tok, out_tok, warnings = search_event(client, name, url, today)
        total_in_tok  += in_tok
        total_out_tok += out_tok
        year_entries = [e for e in entries if is_current_year(e, today.year)]
        for e in year_entries:
            e.setdefault("series", name)
        fresh_by_event[name] = year_entries

        md.append(f"### {name}")
        if year_entries:
            dates = ", ".join(e.get("date") or "TBD" for e in year_entries)
            md.append(f"- Found: {len(year_entries)} event(s) — {dates}")
        else:
            md.append("- Found: 0 events")
        md.append(f"- Tokens: {in_tok:,} in / {out_tok:,} out")
        for w in warnings:
            md.append(f"- ⚠ {w}")
        md.append("")

    output_path = Path(OUTPUT_FILE)
    existing = load_existing(output_path)
    merged, n_new, n_changed, n_unchanged = upsert(existing, fresh_by_event)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(merged, sort_keys=False, allow_unicode=True))

    total_cost = (total_in_tok * _COST_PER_M_INPUT + total_out_tok * _COST_PER_M_OUTPUT) / 1_000_000

    md += [
        "## Summary",
        "",
        f"Wrote **{len(merged)}** event(s) to `{output_path}`",
        "",
        "| | |",
        "|---|---|",
        f"| New entries | {n_new} |",
        f"| Changed entries | {n_changed} |",
        f"| Unchanged entries | {n_unchanged} |",
        f"| Input tokens | {total_in_tok:,} |",
        f"| Output tokens | {total_out_tok:,} |",
        f"| **Total cost** | **${total_cost:.4f}** |",
        f"| Rate | ${_COST_PER_M_INPUT}/1M in · ${_COST_PER_M_OUTPUT}/1M out |",
    ]

    Path("changes.md").write_text("\n".join(md) + "\n")

    print(f"\nWrote {len(merged)} event(s) to {output_path}  ({n_new} new, {n_changed} changed, {n_unchanged} unchanged)")
    print(f"Tokens: {total_in_tok:,} in / {total_out_tok:,} out  |  Cost: ${total_cost:.4f}")
    print("Report: changes.md")


if __name__ == "__main__":
    main()
