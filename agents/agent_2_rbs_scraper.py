"""
RBS (Rates Branch System) scraper - https://rbs.indianrail.gov.in/ShortPath/

WHY THIS WAS REWRITTEN AGAIN
----------------------------
The bundled cache.db holds 38,255 rows, all status=SUCCESS, with real route
sequences and distances. Its `junctions` values look like ["RJN", "AJNI"] -
that is the *substring* rule from the ORIGINAL scraper, not the station-name
rule from the version currently in the repo. So the cache was built by the
original code, which means the original POST worked:

    POST https://rbs.indianrail.gov.in/ShortPath/ShortPathServlet
    srcCode=<src>&destCode=<dest>&gaugeType=S&distance=goods&PageName=ShortPath

The "auto-discover the form" rewrite is what stopped it fetching:

  1. STATIC_FIELDS were only sent if their names appeared in the first <form>
     on ShortPath.jsp. If the JSP builds those fields in JavaScript, or names
     the visible inputs differently from what the servlet reads, gaugeType /
     distance / PageName were silently DROPPED and the servlet returned the
     empty form page.
  2. soup.find("form") grabs the FIRST form. On these IR JSP pages that is
     often a nav/search widget, so `action` resolved to the wrong URL.
  3. If the bootstrap GET raised, the broad `except` cached FAILED for every
     single OD pair in the batch.
  4. `distance <= 0` was treated as a failure.
  5. Every failure - network, SSL, wrong URL, HTML change - was swallowed and
     written as FAILED, which is exactly why "it isn't fetching" is invisible.

This version goes back to the direct POST and keeps only the additions that
were actually safe: retries, rate limiting, thread-local sessions, tolerant
distance parsing, and - most importantly - errors you can SEE.
"""

import concurrent.futures
import random
import re
import threading
import time
import warnings

import requests
from bs4 import BeautifulSoup

from core.cache import RBSCache
from config import RBS_URL

warnings.filterwarnings('ignore', message='Unverified HTTPS request')

RBS_FORM_PAGE = "https://rbs.indianrail.gov.in/ShortPath/ShortPath.jsp"

# Exactly the payload that built the existing 38k-row cache. Do not "improve"
# these names without checking DevTools first.
STATIC_FIELDS = {
    "gaugeType": "S",
    "distance": "goods",
    "PageName": "ShortPath",
}
SRC_FIELD = "srcCode"
DEST_FIELD = "destCode"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

MAX_WORKERS = 6
MIN_INTERVAL = 0.30      # global spacing between requests, seconds
MAX_RETRIES = 3

# Junction rule: the 38k cached rows were built with `"JN" in code`. Nothing
# downstream consumes `junctions` (grep: only stored and passed through), so we
# keep the original rule to avoid two definitions living in one cache. Flip to
# True only if you also plan to rebuild the cache.
JUNCTION_BY_NAME = False

_CODE_RE = re.compile(r"^[A-Z0-9]{2,8}$")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


class RBSError(Exception):
    """Transport/parse failure - distinct from 'RBS has no route for this pair'."""


class RateLimiter:
    def __init__(self, min_interval):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap + random.uniform(0, 0.1))
            self._last = time.monotonic()


class RBSScraper:
    def __init__(self, url=RBS_URL, strict=False):
        """
        strict=True re-raises RBSError instead of caching FAILED. Use it in a
        smoke test so a dead portal fails loudly instead of quietly producing
        a spreadsheet full of blank distances.
        """
        self.cache = RBSCache()
        self.url = url
        self.strict = strict
        self._local = threading.local()
        self._limiter = RateLimiter(MIN_INTERVAL)
        self.stats = {"cache_hits": 0, "fetched": 0, "no_route": 0, "errors": 0}
        self._stats_lock = threading.Lock()
        self.last_error = None

    # ------------------------------------------------------------------ net
    def _session(self):
        """One session per thread; a shared cookie jar under 20 threads is a bug farm."""
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.verify = False              # CRIS chain is frequently incomplete
            s.headers.update({
                "User-Agent": USER_AGENT,
                "Referer": RBS_FORM_PAGE,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            })
            # Best-effort warm-up for JSESSIONID. NON-FATAL: the original code
            # never did this and still worked, so a failure here must not stop
            # the POST.
            try:
                self._limiter.wait()
                s.get(RBS_FORM_PAGE, timeout=20)
            except Exception:
                pass
            self._local.session = s
        return s

    def _post(self, source, destination):
        payload = dict(STATIC_FIELDS)
        payload[SRC_FIELD] = source
        payload[DEST_FIELD] = destination

        last = None
        for attempt in range(MAX_RETRIES):
            try:
                self._limiter.wait()
                r = self._session().post(self.url, data=payload, timeout=30)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise RBSError(f"HTTP {r.status_code} from RBS")
                r.raise_for_status()
                return r.text
            except Exception as e:
                last = e
                if attempt < MAX_RETRIES - 1:
                    time.sleep((2 ** attempt) + random.uniform(0, 0.5))
        raise RBSError(f"{source}->{destination}: {type(last).__name__}: {last}")

    # --------------------------------------------------------------- parsing
    @staticmethod
    def _rows_to_route(rows, code_i, dist_i, name_i=None):
        codes, junctions, max_dist = [], [], 0.0
        for row in rows:
            cols = row.find_all("td")
            if len(cols) <= max(code_i, dist_i):
                continue

            code = cols[code_i].get_text(" ", strip=True).split("\n")[0].strip().upper()
            if not _CODE_RE.match(code):
                continue
            codes.append(code)

            if JUNCTION_BY_NAME and name_i is not None and len(cols) > name_i:
                name = cols[name_i].get_text(" ", strip=True).upper()
                if re.search(r"\bJN\b|\bJUNCTION\b", name):
                    junctions.append(code)
            elif "JN" in code:
                junctions.append(code)

            cell = cols[dist_i].get_text(" ", strip=True).replace(",", "")
            m = _NUM_RE.search(cell)
            if m:
                max_dist = max(max_dist, float(m.group()))

        return max_dist, codes, junctions

    @classmethod
    def _parse(cls, html):
        """
        Positional layout first - that is the one proven to work against this
        servlet. Header-aware detection is only a fallback for a future
        redesign, not the primary path.
        """
        soup = BeautifulSoup(html, "html.parser")

        dist, codes, juncs = cls._rows_to_route(soup.find_all("tr"), 1, 3, 2)
        if codes:
            return dist, codes, juncs

        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            headers = [c.get_text(" ", strip=True).lower()
                       for c in rows[0].find_all(["th", "td"])]

            def find(*kw):
                for i, h in enumerate(headers):
                    if any(k in h for k in kw):
                        return i
                return None

            code_i, dist_i = find("code"), find("distance", "km", "dist")
            if code_i is None or dist_i is None:
                continue
            dist, codes, juncs = cls._rows_to_route(rows[1:], code_i, dist_i, find("name"))
            if codes:
                return dist, codes, juncs

        return 0.0, [], []

    # ---------------------------------------------------------------- single
    def _bump(self, key):
        with self._stats_lock:
            self.stats[key] += 1

    def _fetch_single_route(self, source, destination):
        source = str(source).strip().upper()
        destination = str(destination).strip().upper()

        cached = self.cache.get_route(source, destination)
        if cached and cached['status'] == 'SUCCESS':
            self._bump("cache_hits")
            return (source, destination, cached['distance'],
                    cached['route_sequence'], cached['junctions'])

        try:
            html = self._post(source, destination)
            distance, route_sequence, junctions = self._parse(html)
        except RBSError as e:
            # A portal/network problem is NOT the same as "no route exists".
            self._bump("errors")
            self.last_error = str(e)
            self.cache.set_route(source, destination, 0, [], [], status="ERROR")
            if self.strict:
                raise
            return source, destination, None, [], []

        if not route_sequence:
            self._bump("no_route")
            self.cache.set_route(source, destination, 0, [], [], status="FAILED")
            return source, destination, None, [], []

        self._bump("fetched")
        self.cache.set_route(source, destination, distance,
                             route_sequence, junctions, status="SUCCESS")
        return source, destination, distance, route_sequence, junctions

    # ----------------------------------------------------------------- batch
    def get_routes_batch(self, od_pairs, max_workers=MAX_WORKERS):
        if not od_pairs:
            return {}

        unique = list({(str(s).strip().upper(), str(d).strip().upper())
                       for s, d in od_pairs})
        results = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(self._fetch_single_route, s, d) for s, d in unique]
            for fut in concurrent.futures.as_completed(futures):
                src, dest, dist, route, juncs = fut.result()
                results[(src, dest)] = (dist, route, juncs)

        return results

    # ------------------------------------------------------------ diagnostic
    def diagnose(self, source="NGP", destination="BPL"):
        """
        Run ONE request and report what actually came back. Call this from a
        machine that can reach rbs.indianrail.gov.in before blaming the parser.
        """
        payload = dict(STATIC_FIELDS)
        payload[SRC_FIELD] = source
        payload[DEST_FIELD] = destination

        out = {"url": self.url, "payload": payload}
        try:
            s = self._session()
            r = s.post(self.url, data=payload, timeout=30)
            html = r.text
            soup = BeautifulSoup(html, "html.parser")
            dist, codes, juncs = self._parse(html)
            out.update({
                "http_status": r.status_code,
                "bytes": len(html),
                "cookies": dict(s.cookies),
                "tables_found": len(soup.find_all("table")),
                "rows_found": len(soup.find_all("tr")),
                "parsed_distance": dist,
                "parsed_stations": len(codes),
                "route_head": codes[:6],
                "looks_like_login_or_error": any(
                    w in html.lower() for w in ("login", "session expired", "exception", "error")
                ),
                "snippet": re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:400],
            })
        except Exception as e:
            out["exception"] = f"{type(e).__name__}: {e}"
        return out


if __name__ == "__main__":
    import json
    import sys

    sc = RBSScraper(strict=True)
    if "--diagnose" in sys.argv:
        print(json.dumps(sc.diagnose(), indent=2, default=str))
    else:
        print(sc.get_routes_batch([("NGP", "BPL"), ("DURG", "RJN")]))
        print(sc.stats)
