"""
agent_search.py

Searches the web (via Claude's web_search tool) for a fixed list of named
tech conferences, keeps only future events, and upserts the results into
a YAML file. Re-running it updates changed fields and adds newly-announced
dates for the same conferences -- it never adds conferences outside the
list below.

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

# ----------------------------------------------------------------------
# The ONLY conferences this script will ever search for / write to YAML.
# Add/remove names here to change scope.
# ----------------------------------------------------------------------
YEAR = dt.datetime.now().year
CONFERENCE_LIST = {
    "https://in.pycon.org/": "PyCon India",
    "https://gopherconindia.org/": "GopherCon India",
    f"https://{YEAR}.pyconfhyd.org/": "PyConf Hyderabad",
    "https://events.linuxfoundation.org/open-source-summit-india/": "Open Source Summit India",
    f"https://fossunited.org/indiafoss/{YEAR}": "India FOSS",
    "https://www.opensourceindia.in/": "Open Source India",
    "https://www.meetup.com/experttalks-bengaluru/": "Expert Talks Bangalore",
    "https://www.meetup.com/expert-talks-chennai/": "Expert Talks Chennai",
    "https://rubyconfth.com/": "Rubyconf Thailand",
    "https://rubyconf.in/": "Rubyconf India",
    "https://rubycon.it/": "RubyCon Italy",
    "https://deccanqueenonrails.com/": "Deccan Queen on Rails",
    "https://events.linuxfoundation.org/kubecon-cloudnativecon-india/": "KubeCon India",
}

OUTPUT_FILE = f"event/{YEAR}.yaml"
MODEL = "claude-haiku-4-5"

FIELDS = [
    "name",
    "series",              # parent series name, e.g. "Expert Talks Bangalore"
    "mode",                # "online" | "in-person" | "hybrid"
    "venue",
    "country",
    "topic",
    "description",
    "scope",                # "local meetup" | "national conference" | "international conference"
    "date",                 # ISO date or date range string, e.g. "2026-09-12" or "2026-09-12 to 2026-09-14"
    "conference_website",   # official site / "conference booking" page
    "ticket_booking_link",
    "ticket_status",       # "available" | "sold_out" | "not_opened" | null
    "cfp_submission_link",
    "cfp_deadline",        # ISO date the CFP closes, e.g. "2026-07-31"
    "source_url",           # the page the info was confirmed from
]


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


def search_conference(client: Anthropic, name: str, url: str, today: dt.date) -> list[dict]:
    """Ask Claude to web-search for a single conference and return a list
    of future event entries (a conference series may have multiple
    upcoming editions/dates)."""

    fields_inline = ", ".join(FIELDS)
    prompt = f"""Search for all {today.year} events in "{name}" (site: {url}).

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
        max_tokens=4000,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        messages=[{"role": "user", "content": prompt}],
    )

    # The final answer is in the last text block; tool-use/tool-result
    # blocks come before it.
    block_types = [b.type for b in response.content]
    print(f"  [debug] response block types: {block_types}", file=sys.stderr)

    text_blocks = [b.text for b in response.content if b.type == "text"]
    if not text_blocks:
        print(f"  [!] No text response for {name!r}, skipping.", file=sys.stderr)
        return []

    raw = text_blocks[-1].strip()
    print(f"  [debug] raw text ({len(raw)} chars): {raw[:300]!r}", file=sys.stderr)

    extracted = _extract_json_array(raw)
    if extracted is None:
        print(f"  [!] Could not find JSON array for {name!r}:\n{raw[:500]}", file=sys.stderr)
        return []

    try:
        entries = json.loads(extracted)
    except json.JSONDecodeError as exc:
        print(f"  [!] Could not parse JSON for {name!r} ({exc}):\n{extracted[:500]}", file=sys.stderr)
        return []

    if not isinstance(entries, list):
        print(f"  [!] Expected list, got {type(entries).__name__} for {name!r}", file=sys.stderr)
        return []

    print(f"  [debug] parsed {len(entries)} entries; dates: {[e.get('date') for e in entries]}", file=sys.stderr)
    return entries


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


def upsert(existing: list[dict], fresh_by_conference: dict[str, list[dict]]) -> list[dict]:
    """Replace each managed conference's entries with the freshly-searched
    ones. Entries for conferences not in CONFERENCE_LIST are left untouched."""

    managed_names = [n.lower() for n in CONFERENCE_LIST.values()]
    untouched = [
        e for e in existing
        if not any(m in (e.get("name") or "").lower() for m in managed_names)
    ]

    # Only seed `seen` from untouched (unmanaged) entries so that managed
    # conference events are always fully replaced by the fresh fetch.
    seen: set[tuple] = {
        ((e.get("name") or "").lower(), e.get("date") or "")
        for e in untouched
    }
    updated = list(untouched)
    for name in CONFERENCE_LIST.values():
        for entry in fresh_by_conference.get(name, []):
            key = ((entry.get("name") or "").lower(), entry.get("date") or "")
            if key not in seen:
                seen.add(key)
                updated.append(entry)
    return updated


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("Set ANTHROPIC_API_KEY before running this script.")

    client = Anthropic(api_key=api_key)
    today = dt.date.today()

    fresh_by_conference: dict[str, list[dict]] = {}
    for url, name in CONFERENCE_LIST.items():
        print(f"Searching: {name}")
        entries = search_conference(client, name, url, today)
        year_entries = [e for e in entries if is_current_year(e, today.year)]
        for e in year_entries:
            e.setdefault("series", name)
        print(f"  -> {len(year_entries)} event(s) found")
        fresh_by_conference[name] = year_entries

    output_path = Path(OUTPUT_FILE)
    existing = load_existing(output_path)
    merged = upsert(existing, fresh_by_conference)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(merged, sort_keys=False, allow_unicode=True))

    print(f"\nWrote {len(merged)} event(s) to {output_path}")


if __name__ == "__main__":
    main()
