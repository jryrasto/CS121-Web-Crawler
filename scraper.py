import hashlib
import json
import os
import re
from collections import Counter
from urllib.parse import urlparse, urldefrag, urljoin, parse_qs

from bs4 import BeautifulSoup

ALLOWED_DOMAINS = ("ics.uci.edu", "cs.uci.edu", "informatics.uci.edu", "stat.uci.edu")
MAX_BYTES = 2 * 1024 * 1024         # HTML pages are ~30 KB at the median; anything past this is a data dump

def scraper(url, resp):
    links = extract_next_links(url, resp)
    return [link for link in links if is_valid(link)]

def extract_next_links(url, resp):
    # Implementation required.
    # url: the URL that was used to get the page
    # resp.url: the actual url of the page
    # resp.status: the status code returned by the server. 200 is OK, you got the page. Other numbers mean that there was some kind of problem.
    # resp.error: when status is not 200, you can check the error here, if needed.
    # resp.raw_response: this is where the page actually is. More specifically, the raw_response has two parts:
    #         resp.raw_response.url: the url, again
    #         resp.raw_response.content: the content of the page!
    # Return a list with the hyperlinks (as strings) scrapped from resp.raw_response.content

    ## Only a successful, non-empty, reasonably sized HTML response is a page
    # 1. Is there a page at all?
    raw = resp.raw_response
    if raw is None:
        return []
    # 2. Is it a redirect the cache did not follow?
    if 300 <= resp.status < 400:
        location = raw.headers.get("Location")
        return [absolute(url, location)] if location else []
    # 3. Is it a real page with actual data under 2MB (as defined by MAX_BYTES)?
    if resp.status != 200 or not raw.content or len(raw.content) > MAX_BYTES:
        return []
    # 4. Is it actually an HTML URL?
    if "html" not in raw.headers.get("Content-Type", "text/html"):
        return []
    # 5. Where did this content come from? Be certain about URLs that actually
    #    hold the content and handle relative URLs using raw.url
    page_url = urldefrag(raw.url).url
    if not is_valid(page_url):
        return []

    ## Using lxml for speed and tolerance of broken HTML. Remove <script> and
    #  <style> HTML markup content as code before reading text
    #  raw.content is used over raw.text as BeautifulSoup can detect encoding from
    #  the page's own charset; no charset on a text/html response can mangle UTF-8 pages
    soup = BeautifulSoup(raw.content, "lxml")
    for tag in soup(["script","style","noscript"]):
        tag.decompose()

    words = tokenize(soup.get_text(" "))

    return [absolute(page_url, a["href"]) for a in soup.find_all("a", href=True)]

## Relative -> absolute against real page URL, then defragment.
#  A malformed href makes urljoin raise; "" is then rejected by is_valid instead
#  of crashing the worker thread.
def absolute(base, href):
    try:
        return urldefrag(urljoin(base, href.strip())).url
    except ValueError:
        return ""

def is_valid(url):
    # Decide whether to crawl this url or not. 
    # If you decide to crawl it, return True; otherwise return False.
    # There are already some conditions that return False.
    try:
        parsed = urlparse(url)
        ## Only fetch HTTP and HTTPS URLs & Only receive an ASCII URL
        if parsed.scheme not in set(["http", "https"]) or not url.isascii():
            return False
        ## Only fetch the allowed domain itself, or anything under ".{ALLOWED_DOMAIN}/"
        ## Isolates cases such as https://evil.com/?ref=ics.uci.edu or physics.uci.edu
        host = parsed.hostname or ""
        if not any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS):
            return False
        return not re.match(
            r".*\.(css|js|bmp|gif|jpe?g|ico"
            + r"|png|tiff?|mid|mp2|mp3|mp4"
            + r"|wav|avi|mov|mpeg|ram|m4v|mkv|ogg|ogv|pdf"
            + r"|ps|eps|tex|ppt|pptx|doc|docx|xls|xlsx|names"
            + r"|data|dat|exe|bz2|tar|msi|bin|7z|psd|dmg|iso"
            + r"|epub|dll|cnf|tgz|sha1"
            + r"|thmx|mso|arff|rtf|jar|csv"
            + r"|rm|smil|wmv|swf|wma|zip|rar|gz)$"
            + r"|svg|webm|mpg|ppsx|odt|ods|odp|apk|war|img|sql|txt|xml|json)$", parsed.path.lower())

    except TypeError:
        print ("TypeError for ", parsed)
        raise

    ## Real HTMLs may contain broken links, and urlparse raises ValueError on things like an unclosed "["
    except ValueError:
        # urlparse rejects malformed hosts such as "http://[bad".
        return False

## Tokenizer keeping ASCII alphanumeric runs, extended to keep inner
#  apostrophes in words like "don't" so they stay whole and match the
#  stopword list.
def tokenize(text):
    text = text.replace("\u2019", "'")
    return [w.lower() for w in re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)*",text)]
