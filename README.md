# Tech Events Tracker

Tracks a curated list of tech conferences and meetups, populates event data via AI-assisted web search, and serves a searchable static webpage.

## How it works

1. `main.py` uses Claude (`claude-haiku-4-5`) with the `web_search` tool to search for each conference in `CONFERENCE_LIST` and writes results to `event/<year>.yaml`.
2. `app.py` is a Flask + Frozen-Flask app that reads the YAML and renders a responsive HTML page via a Jinja2 template.
3. Running with `freeze` mode produces a fully static `build/` directory ready to deploy anywhere.

## Setup

Requires [mise](https://mise.jdx.dev/) and [uv](https://docs.astral.sh/uv/).

```bash
mise install       # installs uv
uv sync            # installs Python dependencies into .venv
```

## Usage

### Fetch / refresh event data

```bash
export ANTHROPIC_API_KEY=sk-ant-...
uv run python main.py
```

Re-running is safe — it upserts managed conference entries and leaves any manually added entries untouched.

### Run the dev server

```bash
uv run python app.py
```

Open `http://localhost:5000`.

### Build static site

```bash
uv run python app.py freeze
```

Output goes to `build/`.

## Adding or removing conferences

Edit `CONFERENCE_LIST` in `main.py` — it's a `dict[url, name]`. Re-run `main.py` to pick up changes.

## Event data schema

Each entry in `event/<year>.yaml` has these fields:

| Field | Description |
|---|---|
| `name` | Event name |
| `series` | Parent series (e.g. "Expert Talks Bangalore") |
| `mode` | `online` / `in-person` / `hybrid` |
| `venue` | Venue name |
| `country` | Country |
| `topic` | Primary topic |
| `description` | Short description |
| `scope` | `local meetup` / `national conference` / `international conference` |
| `date` | ISO date or range: `YYYY-MM-DD` or `YYYY-MM-DD to YYYY-MM-DD` |
| `conference_website` | Official event page |
| `ticket_booking_link` | Ticketing URL |
| `ticket_status` | `available` / `sold_out` / `not_opened` |
| `cfp_submission_link` | CFP submission URL |
| `cfp_deadline` | ISO date the CFP closes |
| `source_url` | Page that confirms the date/venue |