import hashlib
import json
import os
import re
from collections import Counter
from urllib.parse import urldefrag, urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup

ALLOWED_DOMAINS = ("ics.uci.edu", "cs.uci.edu", "informatics.uci.edu", "stat.uci.edu")
MAX_BYTES = 2 * 1024 * 1024         # HTML pages are ~30 KB at the median; anything past this is a data dump

# Append-only log, one JSON line per crawled page
LOG_FILE = "crawl_log.jsonl"

# ranks.nl default English stopword list
STOPWORDS = frozenset("""
a about above after again against all am an and any are aren't as at be because
been before being below between both but by can't cannot could couldn't did
didn't do does doesn't doing don't down during each few for from further had
hadn't has hasn't have haven't having he he'd he'll he's her here here's hers
herself him himself his how how's i i'd i'll i'm i've if in into is isn't it
it's its itself let's me more most mustn't my myself no nor not of off on once
only or other ought our ours ourselves out over own same shan't she she'd she'll
she's should shouldn't so some such than that that's the their theirs them
themselves then there there's these they they'd they'll they're they've this
those through to too under until up very was wasn't we we'd we'll we're we've
were weren't what what's when when's where where's which while who who's whom
why why's with won't would wouldn't you you'd you'll you're you've your yours
yourself yourselves
""".split())

# Pages with less visible text than this tend to be navigation/error shells
# A page with meaningful information value should have about 2-3 sentences worth of words (let's say 50); Navigation and UI Menu words count
MIN_WORDS = 50

# Detect trap URLs via query keys that only re-render same content
# (share buttons, comment replies, calendar views...)
TRAP_QUERY_KEYS = {"share", "replytocom", "action", "do", "rev", "version", "idx",
                   "ical", "outlook-ical", "tribe-bar-date", "eventdisplay", "c", "o", "sort", "orderby"}
# Faceted-search keys (filter[units], tribe__ecp_custom_46[0], ...)
# Every combination of facets is a new URL over the same listing
TRAP_QUERY_PREFIXES = ("filter", "tribe")
MAX_URL_LENGTH = 300

# Simhash fingerprints within this many bits are near-duplicates (Manku et al., 2007: 3 of 64 bits)
SIMHASH_BITS = 64
NEAR_DUP_DISTANCE = 3

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
    # Is there a page at all?
    raw = resp.raw_response
    if raw is None:
        return []
    # Is it a redirect the cache did not follow?
    if 300 <= resp.status < 400:
        location = raw.headers.get("Location")
        return [absolute(url, location)] if location else []
    # Is it a real page with actual data under 2MB (as defined by MAX_BYTES)?
    if resp.status != 200 or not raw.content or len(raw.content) > MAX_BYTES:
        return []
    # Is it actually an HTML URL?
    if "html" not in raw.headers.get("Content-Type", "text/html").lower():
        return []
    # Where did this content come from? Be certain about URLs that actually
    # hold the content and handle relative URLs using raw.url
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

    # Every word counts toward page length, and only content words count toward frequency
    words = tokenize(soup.get_text(" "))
    freqs = Counter(w for w in words if w not in STOPWORDS and len(w) > 1 and not w.isdigit())
    fingerprint = simhash(freqs)

    # Check before recording so a page is never a duplicate of itself
    duplicate = is_near_duplicate(page_url, fingerprint)
    record_page(page_url, len(words), fingerprint, freqs)

    # A page is counted but its links not followed when it carries little text or repeated seen content
    if len(words) < MIN_WORDS or duplicate:
        return []

    return [absolute(page_url, a["href"]) for a in soup.find_all("a", href=True)]

## Relative -> absolute against real page URL, then defragment
#  A malformed href makes urljoin raise; "" is then rejected by is_valid instead of crashing the worker thread.
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
        ## Only fetch HTTP and HTTPS URLs & only receive an ASCII URL
        if parsed.scheme not in set(["http", "https"]) or not url.isascii():
            return False
        ## Only fetch the allowed domain itself, or anything under ".{ALLOWED_DOMAIN}/"
        ## Isolates cases such as https://evil.com/?ref=ics.uci.edu or physics.uci.edu
        host = parsed.hostname or ""
        if not any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS):
            return False
        ## URLs that exceed the character limit tend to accumulate parameters and not be real pages
        if len(url) > MAX_URL_LENGTH:
            return False
        ## URLs that have growing path segments appearing >= 3 times tend to be traps
        segments = [s for s in parsed.path.lower().split("/") if s]
        if segments and Counter(segments).most_common(1)[0][1] >= 3:
            return False
        ## URLs with a full ISO or US/EU date format in the path or query are calendar links that can chain to the next day forever
        if re.search(r"(\d{4}-\d{2}-\d{2}|\b\d{1,2}-\d{1,2}-\d{4})(?=[/?&]|$)", parsed.path + "?" + parsed.query):
            return False
        ## URLs with query keys that only change how the page is presented multiply it into many with essentially the same content
        query_keys = {k.lower() for k in parse_qs(parsed.query, keep_blank_values=True)}
        if query_keys & TRAP_QUERY_KEYS or any(k.startswith(TRAP_QUERY_PREFIXES) for k in query_keys):
            return False

        # Extended extension filters
        return not re.match(
            r".*\.(css|js|bmp|gif|jpe?g|ico"
            + r"|png|tiff?|mid|mp2|mp3|mp4"
            + r"|wav|avi|mov|mpeg|ram|m4v|mkv|ogg|ogv|pdf"
            + r"|ps|eps|tex|ppt|pptx|doc|docx|xls|xlsx|names"
            + r"|data|dat|exe|bz2|tar|msi|bin|7z|psd|dmg|iso"
            + r"|epub|dll|cnf|tgz|sha1"
            + r"|thmx|mso|arff|rtf|jar|csv"
            + r"|rm|smil|wmv|swf|wma|zip|rar|gz"
            + r"|svg|webm|mpg|ppsx|odt|ods|odp|apk|war|img|sql|txt|xml|json|ics"
            + r"|ova|ovf|vmdk|vdi|qcow2|vhd|vhdx|xz|deb|rpm|whl|pkg)$", parsed.path.lower())

    ## Real HTMLs may contain broken links, and urlparse raises ValueError on things like an unclosed "["
    except ValueError:
        # urlparse rejects malformed hosts such as "http://[bad".
        return False

## Tokenizer keeping ASCII alphanumeric runs, extended to keep inner
#  apostrophes in words like "don't" so they stay whole and match the
#  stopword list.
def tokenize(text):
    text = text.replace("\u2019", "'")
    return [w.lower() for w in re.findall(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)*",text)]

## Read the log back, skipping a line half-written by a crash
def read_log():
    if not os.path.exists(LOG_FILE):
        return
    with open(LOG_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue

## Save one line per page as JSON; appending is crash-safe and O(1) per page
def record_page(url, word_count, fingerprint, freqs):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"url":url,"words":word_count,"simhash":fingerprint,"freqs":freqs}) + "\n")
    _seen_fingerprints.append((fingerprint, url))

# Each token votes +weight/-weight on each bit of its hash, and the sign of each bit's total becomes the fingerprint bit
# Using blake2b instead of hash() as the latter is salted per process, and fingerprints survive restarts
def simhash(freqs):
    votes = [0] * SIMHASH_BITS
    for token, weight in freqs.items():
        h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
        for bit in range(SIMHASH_BITS):
            votes[bit] += weight if h >> bit & 1 else -weight
    return sum(1 << bit for bit in range(SIMHASH_BITS) if votes[bit] > 0)

# Linear scan of ~50k comparisons, but the 500ms politeness delay covers it
def is_near_duplicate(url, fingerprint):
    return any(u != url and bin(fingerprint ^ f).count("1") <= NEAR_DUP_DISTANCE
               for f, u in _seen_fingerprints)

_seen_fingerprints = [(rec["simhash"], rec["url"]) for rec in read_log()]
