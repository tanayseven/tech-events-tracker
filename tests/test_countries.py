import pytest

from countries import ISO_CODES, flag, normalize_countries


@pytest.mark.parametrize("raw,expected", [
    ("India", ["India"]),
    ("US", ["United States"]),
    ("USA", ["United States"]),
    ("United States", ["United States"]),
    ("Munich, Germany", ["Germany"]),
    ("Rio de Janeiro, Brazil", ["Brazil"]),
    ("Seoul, South Korea", ["South Korea"]),
    ("Seattle, WA, USA", ["United States"]),
    ("United Kingdom / United States", ["United Kingdom", "United States"]),
    ("UK & USA", ["United Kingdom", "United States"]),
    ("Korea, South", ["South Korea"]),
    ("Czech Republic", ["Czechia"]),
    ("Atlantis", ["Atlantis"]),           # unknown: kept, not dropped
    (None, []),
    ("", []),
    ('{\n "@type": "Event"}', []),        # JSON junk is not a country
])
def test_normalize(raw, expected):
    assert normalize_countries(raw) == expected


def test_flag():
    assert flag("Spain") == "🇪🇸"
    assert flag("United States") == "🇺🇸"
    assert flag("Atlantis") == "🌍"


def test_every_code_is_two_letters():
    assert all(len(c) == 2 and c.isupper() for c in ISO_CODES.values())
