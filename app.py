from __future__ import annotations

import datetime as dt
from pathlib import Path

import yaml
from flask import Flask, render_template
from flask_frozen import Freezer

app = Flask(__name__, static_folder="assets", static_url_path="/assets")
app.config["FREEZER_DESTINATION"] = "build"
app.config["FREEZER_RELATIVE_URLS"] = True

YEAR = dt.datetime.now().year


def _load_events() -> list[dict]:
    path = Path(f"event/{YEAR}.yaml")
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text())
    return data or []


def _sort_key(event: dict) -> tuple:
    date_str = (event.get("date") or "").split(" to ")[0].strip()
    try:
        return (0, dt.date.fromisoformat(date_str[:10]))
    except ValueError:
        return (1, dt.date.max)


@app.route("/")
def index():
    all_events = sorted(_load_events(), key=_sort_key)

    # Group by series, preserving date-order of each group's first event.
    series_map: dict[str, list[dict]] = {}
    series_order: list[str] = []
    for event in all_events:
        key = event.get("series") or event.get("name") or ""
        if key not in series_map:
            series_map[key] = []
            series_order.append(key)
        series_map[key].append(event)

    groups = [series_map[k] for k in series_order]

    return render_template(
        "index.html",
        groups=groups,
        total=len(all_events),
        year=YEAR,
        today_iso=dt.date.today().isoformat(),
    )


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "freeze":
        freezer = Freezer(app)
        freezer.freeze()
        print(f"Frozen to {app.config['FREEZER_DESTINATION']}/")
    else:
        app.run(debug=True)