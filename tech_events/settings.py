BOT_NAME = "tech_events"
SPIDER_MODULES = ["tech_events.spiders"]
NEWSPIDER_MODULE = "tech_events.spiders"

ROBOTSTXT_OBEY = False
DOWNLOAD_DELAY = 1
HTTPCACHE_ENABLED = False
LOG_LEVEL = "WARNING"

DOWNLOADER_MIDDLEWARES = {
    "scrapy.downloadermiddlewares.offsite.OffsiteMiddleware": None,
}

DEFAULT_REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (tech-events-tracker; +https://github.com/tanayseven/tech-events-tracker)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en",
}

ITEM_PIPELINES = {
    "tech_events.pipelines.ClaudeExtractionPipeline": 200,
    "tech_events.pipelines.YamlWriterPipeline": 300,
}