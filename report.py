from collections import Counter
from urllib.parse import urlparse

from scraper import read_log

## One streaming pass that keeps only running totals, so memory
# grows with unique URLs and vocab, not with the size of the log.
def main():
    seen = set()
    longest = None          # format: (url, word count)
    totals = Counter()
    subdomains = Counter()

    ## Uniqueness is defragmented URL. Pages logged twice count once
    #  (i.e. from redownload after restart, or again through redirect)
    for rec in read_log():
        url = rec["url"]
        if url in seen:
            continue
        seen.add(url)
        
        if longest is None or rec["words"] > longest[1]:
            longest = (url, rec["words"])
        # Stopwords already dropped when each page was logged
        totals.update(rec["freqs"])
        # Same dot-boundary as is_valid, so "physics.uci.edu" doesn't count as ics
        host = urlparse(url).hostname
        if host == "ics.uci.edu" or host.endswith(".ics.uci.edu"):
            subdomains[host] += 1

    print(f"1. Unique pages: {len(seen)}\n")
    if longest:
        print(f"2. Longest page: {longest[0]} ({longest[1]} words)\n")
    else:       # Handle printing empty or missing logs instead of crashing
        print("2. Longest page: none (no pages logged yet)\n")
    print(f"3. Top 50 common words: ")
    for word, count in totals.most_common(50):
        print(f"{word}, {count}")
    print()
    print(f"4. Subdomains of ics.uci.edu: {len(subdomains)}")
    for host in sorted(subdomains):
        print(f"http://{host}, {subdomains[host]}")

if __name__ == "__main__":
    main()