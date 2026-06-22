import scrapy


class PageItem(scrapy.Item):
    event_name = scrapy.Field()  # canonical name from events_list.yaml
    url = scrapy.Field()         # URL that was fetched
    html = scrapy.Field()        # raw response HTML