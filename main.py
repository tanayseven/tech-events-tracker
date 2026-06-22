"""
main.py

Runs the Scrapy events spider, which:
  1. Fetches each URL in events_list.yaml
  2. Sends the HTML to Claude for structured extraction
  3. Upserts results into event/<year>.yaml

Requirements:
  ANTHROPIC_API_KEY must be set.

Usage:
  uv run python main.py
  uv run scrapy crawl events   # alternative
"""

import os
import sys

from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings

from tech_events.spiders.events import EventsSpider


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Error: ANTHROPIC_API_KEY is not set.\n\n  export ANTHROPIC_API_KEY=sk-ant-...")

    process = CrawlerProcess(get_project_settings())
    process.crawl(EventsSpider)
    process.start()


if __name__ == "__main__":
    main()