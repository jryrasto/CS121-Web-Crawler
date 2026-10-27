from threading import Thread

from inspect import getsource
from utils.download import download
from utils import get_logger
import requests
import scraper
import time

# Wait before retrying a failed cache server (30s, doubling; capped @ 10min)
RETRY_DELAY = 30
MAX_RETRY_DELAY = 600
# After this many failures on one URL, check whether the cache is up
FLAG_AFTER = 3

class Worker(Thread):
    def __init__(self, worker_id, config, frontier):
        self.logger = get_logger(f"Worker-{worker_id}", "Worker")
        self.config = config
        self.frontier = frontier
        # basic check for requests in scraper
        assert {getsource(scraper).find(req) for req in {"from requests import", "import requests"}} == {-1}, "Do not use requests in scraper.py"
        assert {getsource(scraper).find(req) for req in {"from urllib.request import", "import urllib.request"}} == {-1}, "Do not use urllib.request in scraper.py"
        super().__init__(daemon=True)
        
    def run(self):
        while True:
            tbd_url = self.frontier.get_tbd_url()
            if not tbd_url:
                self.logger.info("Frontier is empty. Stopping Crawler.")
                break
            try:
                resp = self.download_with_retry(tbd_url)
                self.logger.info(
                    f"Downloaded {tbd_url}, status <{resp.status}>, "
                    f"using cache {self.config.cache_server}.")
                scraped_urls = scraper.scraper(tbd_url, resp)
            except Exception:
                self.logger.exception(f"Failed to process {tbd_url}; skipping it")
                scraped_urls = []
            for scraped_url in scraped_urls:
                self.frontier.add_url(scraped_url)
            self.frontier.mark_url_complete(tbd_url)
            time.sleep(self.config.time_delay)

    # When the cache server fails, retry the same URL instead of letting it be marked complete
    def download_with_retry(self, url):
        delay = RETRY_DELAY
        failures = 0
        while True:
            resp, reason = self.try_download(url)
            if resp:
                return resp
            failures += 1
            # A whole-server outage and one URL that always breaks the cache looks the same.
            # A seed tells them apart such that if the cache answers it, the cache is up, so give up on this URL
            # (run() logs it and moves on) instead of stalling the crawl on it forever.
            if failures >= FLAG_AFTER and self.try_download(self.config.seed_urls[0])[0]:
                raise RuntimeError(f"cache is up but keeps failing on {url} ({reason})")
            self.logger.warning(
                f"Cache server failure ({reason}) on {url}; retrying in {delay}s.")
            time.sleep(delay)
            delay = min(delay * 2, MAX_RETRY_DELAY)

    # On one attempt, return (response, None) on a real answer, (None, reason) on cache server failure
    def try_download(self, url):
        try:
            resp = download(url, self.config, self.logger)
            if resp.raw_response is not None or 600 <= resp.status < 700:
                return resp, None
            return None, f"status {resp.status}"
        except requests.RequestException as e:
            return None, type(e).__name__
