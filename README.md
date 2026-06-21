# Tech Events Tracker

 <img src="assets/conference-tracker.png" alt="Tech Events Tracker logo" width="100" />

**Live site: [techevents.tanay.tech](https://techevents.tanay.tech/)**

[![GitHub Actions Workflow Status](https://img.shields.io/github/actions/workflow/status/tanayseven/tech-events-tracker/build.yml?branch=main&style=for-the-badge&label=CI)](https://github.com/tanayseven/tech-events-tracker/actions)
[![Website](https://img.shields.io/website?style=for-the-badge&url=https%3A%2F%2Ftechevents.tanay.tech&up_message=online&down_message=offline)](https://techevents.tanay.tech)
[![GitHub License](https://img.shields.io/github/license/tanayseven/tech-events-tracker?style=for-the-badge)](https://github.com/tanayseven/tech-events-tracker/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/Python-3.14-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-3.1.3-000000?style=for-the-badge&logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![Pydantic](https://img.shields.io/badge/Pydantic-2.13.4-E92063?style=for-the-badge&logo=pydantic&logoColor=white)](https://docs.pydantic.dev/)
[![Amazon S3](https://img.shields.io/badge/Amazon%20S3-FF9900?style=for-the-badge&logo=amazons3&logoColor=white)](https://aws.amazon.com/s3/)
[![Claude](https://img.shields.io/badge/Claude-Haiku_4.5-D97706?style=for-the-badge&logo=anthropic&logoColor=white)](https://www.anthropic.com/)

Tracks a curated list of tech conferences and meetups, populates event data via AI-assisted web search, and serves a searchable static webpage.

## How it works

1. `main.py` uses Claude (`claude-haiku-4-5`) with the `web_search` tool to search for each entry in `EVENT_LIST` and writes results to `event/<year>.yaml`.
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

Re-running is safe — it upserts managed event entries and leaves any manually added entries untouched.

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

## Adding or removing tracked events

Edit `EVENT_LIST` in `main.py` — it's a `dict[url, name]`. Re-run `main.py` to pick up changes.

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
| `event_website` | Official event page |
| `ticket_booking_link` | Ticketing URL |
| `ticket_status` | `available` / `sold_out` / `not_opened` |
| `cfp_submission_link` | CFP submission URL |
| `cfp_deadline` | ISO date the CFP closes |
| `source_url` | Page that confirms the date/venue |