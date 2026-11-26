"""
verify_urls.py

For every conference in events_list.yaml, search the web (DuckDuckGo via the
`ddgs` package — no API key) by the event's NAME, and check whether the URL on
file still shows up in the results. Report-only: events_list.yaml is never
modified.

Output:
  - console table (verdict, name, current URL, top search result)
  - url_report.yaml listing entries that need attention, with candidates

Verdicts:
  OK         the stored URL's host appears in the top search results
  CHANGED    it doesn't, and a result whose domain resembles the event name is
             suggested (check by hand)
  UNSURE     it doesn't, but no result looks like the event's own site; nothing
             is suggested
  NOT FOUND  the search returned nothing usable
  ERROR      the search failed (e.g. rate limited after retries)

Usage:
  uv run python verify_urls.py
  uv run python verify_urls.py --only "PyCon India" --only "Nullcon"
  uv run python verify_urls.py --results 10 --delay 3
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import tldextract
import yaml
from ddgs import DDGS
from ddgs.exceptions import DDGSException

from tech_events.pipelines import EVENTS_LIST_FILE, YEAR, _retemplatize

REPORT_FILE = Path("url_report.yaml")
IGNORE_FILE = Path("verify_urls_ignore.yaml")
MAX_CONSECUTIVE_ERRORS = 5  # circuit breaker: assume search is blocked after this many
# When the breaker trips, pause this long and retry the failed entries (blocks are
# often brief); after the last pause, give up on the remaining entries.
BLOCK_COOLDOWNS_S = (60, 180)

OK = "OK"
CHANGED = "CHANGED"
NOT_FOUND = "NOT FOUND"
UNSURE = "UNSURE"
ERROR = "ERROR"

# Ticketing, listing, social and news sites that rank well but are never the
# official homepage. Matched on registered domain.
AGGREGATORS = {
    # search-engine ad/redirect links (e.g. bing.com/aclick) leak into results
    "bing.com", "duckduckgo.com", "yahoo.com",
    "10times.com", "allevents.in", "bookmyshow.com", "conference-hall.io",
    "conferenceindex.org", "conferank.com", "conferencegrid.com",
    "conventiongrid.com", "conventions.io", "confs.tech", "crunchbase.com",
    "cyberedboard.io", "dev.events", "eventbrite.com", "eventbrite.co.uk",
    "eventbrite.com.au", "eventhive.tech", "facebook.com", "flickr.com",
    "github.com", "github.io", "insider.in", "instagram.com", "konfhub.com",
    "linkedin.com", "lu.ma", "medium.com", "meetup.com", "reddit.com",
    "security.world", "sessionize.com", "techsnap.in", "townscript.com",
    "twitter.com", "wikipedia.org", "x.com", "youtube.com",
}

# Use the bundled public-suffix snapshot; never fetch it over the network.
_tld = tldextract.TLDExtract(suffix_list_urls=())


@dataclass
class Result:
    name: str
    current_url: str                    # as written (may contain {year})
    verdict: str = ERROR
    matched_url: str | None = None      # search result that confirmed the stored host
    suggested: str | None = None        # proposed events_list.yaml value for CHANGED
    candidates: list[dict] = field(default_factory=list)  # top non-aggregator results
    error: str | None = None


# ── URL helpers ───────────────────────────────────────────────────────────────

def _host(url: str | None) -> str:
    """Lowercase host without www., with {year} expanded."""
    if not url:
        return ""
    url = url.strip().replace("{year}", str(YEAR))
    if "://" not in url:
        url = "//" + url
    return urlsplit(url).hostname.removeprefix("www.") if urlsplit(url).hostname else ""


def _same_site(a: str, b: str) -> bool:
    """Same host, or one is a subdomain of the other (2026.x.org ↔ x.org)."""
    ha, hb = _host(a), _host(b)
    if not ha or not hb:
        return False
    return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)


def _is_aggregator(url: str) -> bool:
    return _tld(url).top_domain_under_public_suffix.lower() in AGGREGATORS


# ── Search + verdict ──────────────────────────────────────────────────────────

# Words too common in event names to identify a site on their own.
_GENERIC = {
    "conference", "conf", "summit", "world", "tech", "days", "day", "open",
    "source", "india", "international", "global", "annual", "expo", "show",
    "symposium", "forum", "event", "events", "community", "developer",
    "developers", "great", "local", "north", "south", "east", "west", "america",
}


def _resembles(name: str, url: str) -> bool:
    """
    Does the URL's host look like it belongs to the event `name`? True when the
    host contains a distinctive word of the name (reactfoo.in), the name's
    acronym as a host label (lpc.events for Linux Plumbers Conference), or two or
    more of the name's words (bengalurutechsummit.com for Bangalore Tech Summit).
    """
    host = _host(url)
    if not host:
        return False
    flat = re.sub(r"[^a-z0-9]", "", host)
    labels = host.split(".")
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) >= 3]
    if any(w not in _GENERIC and len(w) >= 4 and w in flat for w in words):
        return True
    acronym = "".join(w[0] for w in re.findall(r"[a-z0-9]+", name.lower()))
    if len(acronym) >= 3 and acronym in labels:
        return True
    return sum(w in flat for w in words) >= 2


def _query(name: str) -> str:
    return f'"{name}" {YEAR} conference'


def _verdict(current: str, hits: list[dict], top_n: int,
             rejected: list[str] = (), name: str | None = None) -> Result:
    """
    Pure decision logic over search hits (each a dict with 'href', 'title').
    `rejected` are URLs a human already turned down for this event; candidates on
    the same site are never suggested. With `name`, only a candidate whose host
    resembles the event name is suggested (CHANGED); otherwise the result is
    UNSURE and nothing is proposed.
    """
    r = Result(name="", current_url=current)
    urls = [h.get("href") for h in hits if h.get("href")]
    if not urls:
        r.verdict = NOT_FOUND
        return r

    filtered = [
        {"url": h["href"], "title": h.get("title", "")}
        for h in hits
        if h.get("href") and not _is_aggregator(h["href"])
        and not any(_same_site(h["href"], rj) for rj in rejected)
    ]
    if name:  # name-like sites first, keeping search order within each group
        filtered.sort(key=lambda c: not _resembles(name, c["url"]))
    r.candidates = filtered[:3]

    for u in urls[:top_n]:
        if _same_site(current, u):
            r.verdict, r.matched_url = OK, u
            return r

    if not r.candidates:
        r.verdict = NOT_FOUND
        return r
    if name and not _resembles(name, r.candidates[0]["url"]):
        r.verdict = UNSURE  # top hits are articles/listings, not the event's own site
        return r
    r.verdict = CHANGED
    r.suggested = _retemplatize(r.candidates[0]["url"], current)
    return r


def search(ddgs: DDGS, query: str, max_results: int, retries: int = 3) -> list[dict]:
    """
    Run a text search, retrying with backoff. ddgs reports throttling and
    transient backend failures as RatelimitException/TimeoutException or as a
    plain DDGSException("No results found."), so every DDGSException is retried.
    """
    backoff = 5.0
    for attempt in range(retries):
        try:
            return ddgs.text(query, max_results=max_results) or []
        except DDGSException:
            if attempt == retries - 1:
                raise
            time.sleep(backoff)
            backoff *= 2
    return []


def check_entry(ddgs: DDGS, entry: dict, max_results: int, top_n: int,
                rejected: list[str] = ()) -> Result:
    name, url = entry["name"], entry["url"]
    try:
        hits = search(ddgs, _query(name), max_results)
    except DDGSException as exc:
        r = Result(name=name, current_url=url, error=str(exc))
        print(f"  {name}: {ERROR} ({exc})", flush=True)
        return r
    r = _verdict(url, hits, top_n, rejected, name=name)
    r.name = name
    print(f"  {name}: {r.verdict}", flush=True)
    return r


# ── Output ────────────────────────────────────────────────────────────────────

_ORDER = {CHANGED: 0, UNSURE: 1, NOT_FOUND: 2, ERROR: 3, OK: 4}


def _print_table(results: list[Result]) -> None:
    rows = [(r.verdict, r.name, r.current_url,
             r.suggested or r.matched_url or "-") for r in results]
    headers = ("VERDICT", "NAME", "CURRENT", "SEARCH RESULT / SUGGESTED")
    widths = [min(60, max(len(h), *(len(row[i]) for row in rows)))
              for i, h in enumerate(headers)]

    def fmt(row):
        return "  ".join(str(c)[:w].ljust(w) for c, w in zip(row, widths))

    print()
    print(fmt(headers))
    print(fmt(tuple("-" * w for w in widths)))
    for row in sorted(rows, key=lambda r: (_ORDER.get(r[0], 9), r[1].lower())):
        print(fmt(row))


def _write_report(results: list[Result]) -> int:
    flagged = sorted((r for r in results if r.verdict != OK), key=lambda r: r.name.lower())
    if not flagged:
        REPORT_FILE.unlink(missing_ok=True)
        return 0
    report = [
        {
            "name": r.name,
            "verdict": r.verdict,
            "current_url": r.current_url,
            "suggested_url": r.suggested,
            "candidates": r.candidates,
            **({"error": r.error} if r.error else {}),
        }
        for r in flagged
    ]
    REPORT_FILE.write_text(yaml.safe_dump(report, sort_keys=False, allow_unicode=True))
    return len(flagged)


# ── Entry point ───────────────────────────────────────────────────────────────

def _load_ignore(path: Path = None) -> tuple[set[str], dict[str, list[str]]]:
    """
    Read the human-reviewed ignore file. Returns (skip names, reject map), both
    keyed by lowercase event name.
    """
    path = path or IGNORE_FILE
    data = (yaml.safe_load(path.read_text()) if path.exists() else None) or {}
    skip = {str(n).lower() for n in data.get("skip") or []}
    reject = {str(k).lower(): list(v or []) for k, v in (data.get("reject") or {}).items()}
    return skip, reject


def _norm_url(url: str) -> str:
    """Host + path, lowercase, no www./scheme/trailing slash; {year} expanded."""
    u = url.strip().replace("{year}", str(YEAR))
    if "://" not in u:
        u = "//" + u
    parts = urlsplit(u)
    host = (parts.hostname or "").removeprefix("www.")
    return f"{host}{parts.path.rstrip('/')}".lower()


def apply_changes(all_entries: list[dict], results: list[Result]) -> list[Result]:
    """
    Write CHANGED suggestions into `all_entries` (in place) and return the results
    that were applied. A suggestion is dropped if its URL is already in the list
    (for any event), so only genuinely new URLs are proposed.
    """
    existing = {_norm_url(e["url"]) for e in all_entries}
    by_name = {e["name"]: e for e in all_entries}
    applied = []
    for r in results:
        if r.verdict != CHANGED or not r.suggested or r.name not in by_name:
            continue
        if _norm_url(r.suggested) in existing:
            continue
        by_name[r.name]["url"] = r.suggested
        existing.add(_norm_url(r.suggested))
        applied.append(r)
    return applied


def write_summary(path: Path, applied: list[Result], results: list[Result]) -> None:
    """Markdown body for the automated PR."""
    lines = [
        f"Automated `verify_urls.py` run ({len(results)} events searched via DuckDuckGo).",
        "",
        "Each change below came from search ranking — open the suggested link and "
        "confirm it is the official site before merging. To reject a suggestion, "
        f"add it under `reject:` (or the event under `skip:`) in `{IGNORE_FILE}`.",
        "",
        "| Event | Current | Suggested | Other candidates |",
        "|---|---|---|---|",
    ]
    def cell(text: str) -> str:
        return text.replace("|", "\\|").replace("\n", " ")

    for r in sorted(applied, key=lambda r: r.name.lower()):
        others = "<br>".join(f"[{cell(c['title'] or c['url'])}]({c['url']})"
                             for c in r.candidates[1:]) or "—"
        lines.append(f"| {cell(r.name)} | {r.current_url} | {r.suggested} | {others} |")
    rest = [r for r in results if r.verdict in (UNSURE, NOT_FOUND, ERROR)]
    if rest:
        lines += ["", "<details><summary>Unsure / not found / errors (no change made)</summary>", ""]
        for r in sorted(rest, key=lambda r: (_ORDER[r.verdict], r.name.lower())):
            top = f" — top hit: {r.candidates[0]['url']}" if r.candidates else ""
            lines.append(f"- {r.name}: {r.verdict}{f' ({r.error})' if r.error else ''}{top}")
        lines += ["", "</details>"]
    path.write_text("\n".join(lines) + "\n")


def _select(entries: list[dict], only: list[str] | None) -> list[dict]:
    if not only:
        return entries
    wanted = {n.lower() for n in only}
    picked = [e for e in entries if e["name"].lower() in wanted]
    for m in sorted(wanted - {e["name"].lower() for e in picked}):
        print(f"warning: no entry named {m!r} in {EVENTS_LIST_FILE}", file=sys.stderr)
    return picked


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Check events_list.yaml URLs against a DuckDuckGo search (no API key).")
    parser.add_argument("--only", action="append", metavar="NAME",
                        help="check only this event name (repeatable)")
    parser.add_argument("--results", type=int, default=5,
                        help="top N results in which the stored URL must appear (default 5)")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="seconds between searches, to avoid rate limits (default 2)")
    parser.add_argument("--apply", action="store_true",
                        help="write new CHANGED URLs into events_list.yaml (used by CI to open a PR)")
    parser.add_argument("--summary", type=Path, metavar="PATH",
                        help="with --apply: write a markdown summary of applied changes here")
    args = parser.parse_args(argv)

    all_entries = yaml.safe_load(EVENTS_LIST_FILE.read_text()) or []
    skip, reject = _load_ignore()
    entries = _select(all_entries, args.only)
    skipped = [e for e in entries if e["name"].lower() in skip]
    entries = [e for e in entries if e["name"].lower() not in skip]
    if not entries:
        sys.exit("Nothing to check.")

    print(f"Searching for {len(entries)} event(s) via DuckDuckGo"
          f"{f' ({len(skipped)} skipped via {IGNORE_FILE})' if skipped else ''}...")
    ddgs = DDGS()
    results = []
    consecutive_errors = 0
    cooldowns = list(BLOCK_COOLDOWNS_S)
    i = 0
    while i < len(entries):
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            if cooldowns:
                # Search looks blocked. Pause, then retry the entries that just failed.
                pause = cooldowns.pop(0)
                print(f"  {MAX_CONSECUTIVE_ERRORS} searches failed in a row — "
                      f"pausing {pause:g}s, then retrying.", flush=True)
                time.sleep(pause)
                del results[-consecutive_errors:]
                i -= consecutive_errors
                consecutive_errors = 0
                continue
            # Still blocked; stop hammering it. Remaining entries are reported as
            # ERROR and left unchanged.
            results += [Result(name=e["name"], current_url=e["url"],
                               error="not searched: search appears blocked")
                        for e in entries[i:]]
            print(f"  {MAX_CONSECUTIVE_ERRORS} searches failed in a row — "
                  f"stopping; {len(entries) - i} event(s) not checked.", flush=True)
            break
        if i:
            time.sleep(args.delay)
        r = check_entry(ddgs, entries[i], max_results=max(args.results, 10),
                        top_n=args.results,
                        rejected=reject.get(entries[i]["name"].lower(), []))
        consecutive_errors = consecutive_errors + 1 if r.verdict == ERROR else 0
        results.append(r)
        i += 1

    _print_table(results)
    counts = {v: sum(r.verdict == v for r in results) for v in (OK, CHANGED, UNSURE, NOT_FOUND, ERROR)}
    flagged = _write_report(results)

    print()
    print("  ".join(f"{k}: {v}" for k, v in counts.items()))
    if flagged:
        print(f"{flagged} entr{'y' if flagged == 1 else 'ies'} need attention → {REPORT_FILE}")
        print("CHANGED suggestions come from search ranking — verify before editing events_list.yaml.")
    else:
        print("All URLs look current.")

    if args.apply:
        applied = apply_changes(all_entries, results)
        if applied:
            EVENTS_LIST_FILE.write_text(
                yaml.safe_dump(all_entries, sort_keys=False, allow_unicode=True))
            print(f"Applied {len(applied)} new URL(s) to {EVENTS_LIST_FILE}.")
        else:
            print(f"No new URLs to apply; {EVENTS_LIST_FILE} unchanged.")
        if args.summary:
            write_summary(args.summary, applied, results)

    # Search being blocked (e.g. the runner's IP is throttled) is outside this
    # repo's control and usually clears by the next run, so it is a warning, not a
    # failure: a red run would mark main's commit as failing. In Actions it shows
    # as an annotation on the run rather than silently reporting "no changes".
    if counts[ERROR] == len(results):
        msg = ("Every search failed — DuckDuckGo may be blocking or throttling this host. "
               "No URLs were checked.")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::warning title=URL check skipped::{msg}")
        else:
            print(f"warning: {msg}", file=sys.stderr)


if __name__ == "__main__":
    main()
