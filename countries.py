"""
Country-name normalisation and flag emojis for the site.

Scraped `country` values are inconsistent ("US", "USA", "United States",
"Munich, Germany", "United Kingdom / United States"). `normalize_countries()`
turns one raw value into a list of canonical country names, and `flag()` gives
the emoji flag for a canonical name. Applied at render time in app.py, so
re-scrapes can't reintroduce the variants.
"""

from __future__ import annotations

import re

# Canonical name → ISO 3166-1 alpha-2 code.
ISO_CODES: dict[str, str] = {
    "Argentina": "AR", "Australia": "AU", "Austria": "AT", "Bahrain": "BH",
    "Bangladesh": "BD", "Belgium": "BE", "Brazil": "BR", "Bulgaria": "BG",
    "Canada": "CA", "Chile": "CL", "China": "CN", "Colombia": "CO",
    "Croatia": "HR", "Czechia": "CZ", "Denmark": "DK", "Dominican Republic": "DO",
    "Egypt": "EG", "Estonia": "EE", "Finland": "FI", "France": "FR",
    "Germany": "DE", "Greece": "GR", "Hong Kong": "HK", "Hungary": "HU",
    "Iceland": "IS", "India": "IN", "Indonesia": "ID", "Ireland": "IE",
    "Israel": "IL", "Italy": "IT", "Japan": "JP", "Kenya": "KE",
    "Latvia": "LV", "Lithuania": "LT", "Luxembourg": "LU", "Malaysia": "MY",
    "Mexico": "MX", "Morocco": "MA", "Nepal": "NP", "Netherlands": "NL",
    "New Zealand": "NZ", "Nigeria": "NG", "Norway": "NO", "Pakistan": "PK",
    "Peru": "PE", "Philippines": "PH", "Poland": "PL", "Portugal": "PT",
    "Qatar": "QA", "Romania": "RO", "Saudi Arabia": "SA", "Serbia": "RS",
    "Singapore": "SG", "Slovakia": "SK", "Slovenia": "SI", "South Africa": "ZA",
    "South Korea": "KR", "Spain": "ES", "Sri Lanka": "LK", "Sweden": "SE",
    "Switzerland": "CH", "Taiwan": "TW", "Thailand": "TH", "Turkey": "TR",
    "Ukraine": "UA", "United Arab Emirates": "AE", "United Kingdom": "GB",
    "United States": "US", "Uruguay": "UY", "Vietnam": "VN",
}

# Lower-cased variant → canonical name.
_ALIASES: dict[str, str] = {
    "us": "United States", "usa": "United States", "u.s.": "United States",
    "u.s.a.": "United States", "united states of america": "United States",
    "america": "United States",
    "uk": "United Kingdom", "u.k.": "United Kingdom", "great britain": "United Kingdom",
    "britain": "United Kingdom", "england": "United Kingdom", "scotland": "United Kingdom",
    "wales": "United Kingdom",
    "korea": "South Korea", "republic of korea": "South Korea", "korea, south": "South Korea",
    "czech republic": "Czechia",
    "uae": "United Arab Emirates",
    "the netherlands": "Netherlands", "holland": "Netherlands",
    "türkiye": "Turkey", "turkiye": "Turkey",
    "viet nam": "Vietnam",
}
_ALIASES.update({name.lower(): name for name in ISO_CODES})

# Separators between multiple countries in one value ("London & New York" style).
_MULTI = re.compile(r"\s*(?:/|&|;|\band\b)\s*")


def _canonical(part: str) -> str | None:
    part = part.strip().strip(".").strip()
    if not part:
        return None
    key = part.lower()
    if key in _ALIASES:
        return _ALIASES[key]
    # "City, Country" / "City, State, Country" → the last component.
    if "," in part:
        return _canonical(part.rsplit(",", 1)[1])
    return part  # unknown country: keep as-is rather than drop it


def normalize_countries(raw) -> list[str]:
    """Raw scraped country value → list of unique canonical names (may be empty)."""
    if not isinstance(raw, str) or not raw.strip():
        return []
    # Junk such as a JSON blob is not a country.
    if raw.lstrip().startswith(("{", "[")):
        return []
    whole = raw.strip()
    if whole.lower() in _ALIASES:  # e.g. "Korea, South" contains a comma
        return [_ALIASES[whole.lower()]]
    out: list[str] = []
    for part in _MULTI.split(whole):
        name = _canonical(part)
        if name and name not in out:
            out.append(name)
    return out


def flag(name: str) -> str:
    """Emoji flag for a canonical country name; 🌍 when unknown."""
    code = ISO_CODES.get(name)
    if not code:
        return "🌍"
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code)
