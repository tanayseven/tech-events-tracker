import re

import app as site


def test_country_dropdown_has_flags_and_no_cities_or_json():
    html = site.app.test_client().get("/").get_data(as_text=True)
    select = re.search(r'<select id="filter-country".*?</select>', html, re.S).group(0)
    options = re.findall(r'<option value="([^"]+)">([^<]+)</option>', select)
    assert options, "country dropdown is empty"
    names = [v for v, _ in options]
    assert not any("," in n or "{" in n or "/" in n for n in names), names
    assert "USA" not in names and "US" not in names
    assert len(names) == len(set(names))
    # Every option label starts with a flag (regional-indicator pair), no globe.
    for value, label in options:
        assert label.strip() != value and "🌍" not in label, label
