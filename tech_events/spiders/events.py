"""
Spider that fetches every URL in events_list.yaml and yields a PageItem
containing the raw HTML. Extraction is handled downstream in the pipeline.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import scrapy
import yaml

from tech_events.items import PageItem

YEAR = dt.datetime.now().year


class EventsSpider(scrapy.Spider):
    name = "events"

    async def start(self):
        raw = yaml.safe_load(Path("events_list.yaml").read_text()) or []
        for entry in raw:
            url = entry["url"].replace("{year}", str(YEAR))
            yield scrapy.Request(
                url,
                callback=self.parse,
                meta={"event_name": entry["name"], "dont_verify_ssl": True},
                errback=self.on_error,
            )

    def parse(self, response):
        yield PageItem(
            event_name=response.meta["event_name"],
            url=response.url,
            html=response.text,
        )

    def on_error(self, failure):
        self.logger.error(
            "Failed to fetch %s: %s",
            failure.request.url,
            failure.value,
        )