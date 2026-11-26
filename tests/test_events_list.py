"""Sanity checks on events_list.yaml so URL-update PRs can't merge a broken list."""

from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

EVENTS_LIST = Path(__file__).resolve().parent.parent / "events_list.yaml"


@pytest.fixture(scope="module")
def entries():
    data = yaml.safe_load(EVENTS_LIST.read_text())
    assert isinstance(data, list) and data, "events_list.yaml must be a non-empty list"
    return data


def test_every_entry_has_only_name_and_url(entries):
    for entry in entries:
        assert isinstance(entry, dict), entry
        assert set(entry) == {"name", "url"}, entry
        assert isinstance(entry["name"], str) and entry["name"].strip(), entry
        assert isinstance(entry["url"], str) and entry["url"].strip(), entry


def test_urls_are_absolute_http(entries):
    for entry in entries:
        url = entry["url"].replace("{year}", "2026")
        parsed = urlparse(url)
        assert parsed.scheme in ("http", "https"), entry
        assert parsed.netloc and " " not in url, entry


def test_only_year_placeholder_is_used(entries):
    for entry in entries:
        stripped = entry["url"].replace("{year}", "")
        assert "{" not in stripped and "}" not in stripped, entry


def test_names_are_unique(entries):
    names = [e["name"].lower() for e in entries]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"duplicate event names: {sorted(dupes)}"


def test_urls_are_unique(entries):
    urls = [e["url"].rstrip("/").lower() for e in entries]
    dupes = {u for u in urls if urls.count(u) > 1}
    assert not dupes, f"duplicate URLs: {sorted(dupes)}"
