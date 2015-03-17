from collections import deque

from scrapy.core.scheduler import Scheduler
from scrapy.http import Request
from scrapy import log
from scrapy.utils.misc import load_object

from crawlfrontier.contrib.scrapy.manager import ScrapyFrontierManager
from crawlfrontier.settings import Settings

STATS_PREFIX = 'crawlfrontier'

DOWNLOADER_MIDDLEWARE = 'crawlfrontier.contrib.scrapy.middlewares.schedulers.SchedulerDownloaderMiddleware'
SPIDER_MIDDLEWARE = 'crawlfrontier.contrib.scrapy.middlewares.schedulers.SchedulerSpiderMiddleware'


class StatsManager(object):
    """
        'crawlfrontier/crawled_pages_count': 489,
        'crawlfrontier/crawled_pages_count/200': 382,
        'crawlfrontier/crawled_pages_count/301': 37,
        'crawlfrontier/crawled_pages_count/302': 58,
        'crawlfrontier/crawled_pages_count/400': 5,
        'crawlfrontier/crawled_pages_count/403': 1,
        'crawlfrontier/crawled_pages_count/404': 1,
        'crawlfrontier/crawled_pages_count/999': 5,
        'crawlfrontier/iterations': 5,
        'crawlfrontier/links_extracted_count': 39805,
        'crawlfrontier/pending_requests_count': 0,
        'crawlfrontier/redirected_requests_count': 273,
        'crawlfrontier/request_errors_count': 11,
        'crawlfrontier/request_errors_count/DNSLookupError': 1,
        'crawlfrontier/request_errors_count/ResponseNeverReceived': 9,
        'crawlfrontier/request_errors_count/TimeoutError': 1,
        'crawlfrontier/returned_requests_count': 500,
    """
    def __init__(self, stats, prefix=STATS_PREFIX):
        self.stats = stats
        self.prefix = prefix

    def add_seeds(self, count=1):
        self._inc_value('seeds_count', count)

    def add_crawled_page(self, status_code, n_links):
        self._inc_value('crawled_pages_count')
        self._inc_value('crawled_pages_count/%s' % str(status_code))
        self._inc_value('links_extracted_count', n_links)

    def add_redirected_requests(self, count=1):
        self._inc_value('redirected_requests_count', count)

    def add_returned_requests(self, count=1):
        self._inc_value('returned_requests_count', count)

    def add_request_error(self, error_code):
        self._inc_value('request_errors_count')
        self._inc_value('request_errors_count/%s' % str(error_code))

    def set_iterations(self, iterations):
        self._set_value('iterations', iterations)

    def set_pending_requests(self, pending_requests):
        self._set_value('pending_requests_count', pending_requests)

    def _get_stats_name(self, variable):
        return '%s/%s' % (self.prefix, variable)

    def _inc_value(self, variable, count=1):
        self.stats.inc_value(self._get_stats_name(variable), count)

    def _set_value(self, variable, value):
        self.stats.set_value(self._get_stats_name(variable), value)


class CrawlFrontierScheduler(Scheduler):

    def __init__(self, crawler):

        # Add scrapy integration middlewares for scheduler
        self._add_middlewares(crawler)

        self.crawler = crawler
        self.stats_manager = StatsManager(crawler.stats)
        self._pending_requests = deque()
        self.redirect_enabled = crawler.settings.get('REDIRECT_ENABLED')

        frontier_settings = crawler.settings.get('FRONTIER_SETTINGS', None)
        if not frontier_settings:
            log.msg('FRONTIER_SETTINGS not found! Using default frontier settings...', log.WARNING)

        frontier_settings = Settings(frontier_settings or None)
        frontier_settings.AUTO_START = False

        scrapy_parameters = {'crawler': crawler}
        self.frontier = ScrapyFrontierManager(frontier_settings, **scrapy_parameters)

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def enqueue_request(self, request):
        if not self._request_is_redirected(request):
            self.frontier.add_seeds([request])
            self.stats_manager.add_seeds()
            return True
        elif self.redirect_enabled:
            self._add_pending_request(request)
            self.stats_manager.add_redirected_requests()
            return True
        return False

    def next_request(self):
        request = self._get_next_request()
        if request:
            self.stats_manager.add_returned_requests()
        return request

    def process_spider_output(self, response, result, spider):
        links = []
        for element in result:
            if isinstance(element, Request):
                links.append(element)
            else:
                yield element
        self.frontier.page_crawled(response=response,
                                   links=links)
        self.stats_manager.add_crawled_page(response.status, len(links))

    def process_exception(self, request, exception, spider):
        error_code = self._get_exception_code(exception)
        self.frontier.request_error(request=request, error=error_code)
        self.stats_manager.add_request_error(error_code)

    def open(self, spider):
        log.msg('Starting frontier', log.INFO)
        if not self.frontier.manager.auto_start:
            scrapy_kwargs = {'spider': spider}
            self.frontier.start(**scrapy_kwargs)

    def close(self, reason):
        log.msg('Finishing frontier (%s)' % reason, log.INFO)
        scrapy_kwargs = {'reason': reason}
        self.frontier.stop(**scrapy_kwargs)
        self.stats_manager.set_iterations(self.frontier.manager.iteration)
        self.stats_manager.set_pending_requests(len(self))

    def __len__(self):
        return len(self._pending_requests)

    def has_pending_requests(self):
        return len(self) > 0

    def _get_next_request(self):
        if not self.frontier.manager.finished and \
                len(self) < self.crawler.engine.downloader.total_concurrency:
            for request in self.frontier.get_next_requests():
                self._add_pending_request(request)
        return self._get_pending_request()

    def _add_pending_request(self, request):
        return self._pending_requests.append(request)

    def _get_pending_request(self):
        return self._pending_requests.popleft() if self._pending_requests else None

    def _get_exception_code(self, exception):
        try:
            return exception.__class__.__name__
        except:
            return '?'

    def _request_is_redirected(self, request):
        return request.meta.get('redirect_times', 0) > 0

    def _add_middlewares(self, crawler):
        """
        Adds crawl-frontier scrapy scheduler downloader and spider middlewares.
        Hack to avoid defining crawl-frontier scrapy middlewares in settings.
        Middleware managers (downloader+spider) has already been initialized at this moment.
        """
        self._add_middleware_to_manager(manager=crawler.engine.downloader.middleware,
                                        mw=load_object(DOWNLOADER_MIDDLEWARE).from_crawler(crawler))
        self._add_middleware_to_manager(manager=crawler.engine.scraper.spidermw,
                                        mw=load_object(SPIDER_MIDDLEWARE).from_crawler(crawler))

    def _add_middleware_to_manager(self, manager, mw):
        """
        Adds mw to already initialized middleware manager.
        Reproduces the mw add process at the end of the middleware manager mws list.
        """
        manager.middlewares = manager.middlewares + (mw,)
        manager._add_middleware(mw)
