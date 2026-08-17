"""Agent 2 -- shortest-path routes from the RBS portal, cached.

    https://rbs.indianrail.gov.in/ShortPath/

WHAT THIS FILE HAS LEARNED THE HARD WAY
---------------------------------------
1. **Post directly. Do not auto-discover the form.** A previous rewrite parsed
   ShortPath.jsp to find the form and its fields. It broke fetching entirely:
   `soup.find("form")` grabs the *first* form, which on these IR JSP pages is
   often a nav widget, and fields built in JavaScript were silently dropped, so
   the servlet returned the empty form page. The payload below is the one that
   built the existing 38k-row cache. Do not "improve" the field names without
   checking DevTools first.

2. **A portal failure is not "no route exists".** Swallowing every exception
   and writing FAILED is exactly why "it isn't fetching" used to be invisible.
   Transport problems are cached as ERROR, counted, and surfaced; a genuine
   empty answer is FAILED. The audit and the UI treat them differently.

3. **Codes must be normalised identically everywhere.** See
   `core.schema.normalise_station_code`: Excel turns 1234 into "1234.0" and the
   cache lookup then misses forever, in silence.

4. **A refused TCP handshake will not heal inside one run.** After a few
   consecutive connection errors the circuit opens and the rest of the batch
   fails fast, rather than spending minutes on exponential backoff waiting for
   an answer that is not coming.

Added later: a maximum age on cached routes, so a row fetched once is not
served forever, and a bulk cache read so a fully-cached run is not bottlenecked
on one SQLite connection per pair.
"""
import concurrent.futures
import os
import random
import re
import threading
import time

import requests
from bs4 import BeautifulSoup

from config import RBS_URL
from core.cache import RBSCache
from core.schema import normalise_station_code

RBS_FORM_PAGE = "https://rbs.indianrail.gov.in/ShortPath/ShortPath.jsp"

# Exactly the payload that built the existing cache.
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
CIRCUIT_BREAK_AFTER = 5

# The portal presented a valid certificate chain when last checked
# (2026-08-17), so certificates are verified: these distances end up in client
# deliverables and an unverified response can be rewritten in transit. The CRIS
# chain has been incomplete in the past, so RBS_INSECURE=1 remains as an escape
# hatch if it regresses.
VERIFY_TLS = os.environ.get("RBS_INSECURE") != "1"

# Junction rule: the cached rows were built with `"JN" in code`. Nothing
# downstream consumes `junctions`, so the original rule is kept to avoid two
# definitions living in one cache. Flip only if you also rebuild the cache.
JUNCTION_BY_NAME = False

_CODE_RE = re.compile(r"^[A-Z0-9]{2,8}$")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")

# Kept as a module-level alias: the orchestrator and older callers import
# norm_code from here.
norm_code = normalise_station_code


def _code_from_cell(cell):
    """The station code out of a route cell, ignoring what is nested in it.

    The code cell is not just a code. A station with sidings carries a link
    after it, so the cell reads `GDYA Sidings: [MPBG, THSG]`, and joining the
    cell's strings with a space produced exactly that -- which then failed the
    code pattern, and the station was skipped without a word. Every station
    with sidings vanished from every route parsed this way: 8 of 24 survived on
    BBTR -> BIA, the destination among the casualties.

    Joining with a newline instead keeps the link text on its own line, so the
    first line is the code. The pattern check stays as strict as it was -- it
    was never the problem, and it is what rejects the stray one-letter cells.
    """
    return cell.get_text("\n", strip=True).split("\n")[0].strip().upper()


def is_usable_route(source, destination, route_sequence):
    """Is this parse a route, or the portal echoing the origin back?

    RBS answers an unroutable pair -- typically a destination code that does not
    exist -- with a page listing the origin alone. `_parse` reads one code off
    it and 0 km, which is indistinguishable from a real result unless the length
    is checked. Cached as SUCCESS it becomes a 0 km "route" that later reads as
    a real flow touching no corridor station, so the pair is never retried and
    never appears in the unresolved audit either.

    A single station is only ever legitimate when origin and destination are the
    same place.
    """
    if not route_sequence:
        return False
    return len(route_sequence) >= 2 or norm_code(source) == norm_code(destination)


class RBSError(Exception):
    """Transport/parse failure -- distinct from 'RBS has no route for this pair'."""


class RateLimiter:
    """Global spacing between requests, shared across worker threads."""

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
    def __init__(self, max_age_days=None, workers=MAX_WORKERS, url=RBS_URL,
                 strict=False, verify_tls=None):
        """
        max_age_days expires cached routes; None serves any cached SUCCESS.
        strict=True re-raises RBSError instead of caching ERROR. Use it in a
        smoke test so a dead portal fails loudly rather than quietly producing
        a spreadsheet full of blank distances.
        """
        self.cache = RBSCache()
        self.max_age_days = max_age_days
        self.workers = workers
        self.url = url
        self.strict = strict
        self.verify_tls = VERIFY_TLS if verify_tls is None else verify_tls

        self._local = threading.local()
        self._limiter = RateLimiter(MIN_INTERVAL)
        self._stats_lock = threading.Lock()

        self.stats = {"cache_hits": 0, "fetched": 0, "no_route": 0,
                      "errors": 0, "skipped_circuit_open": 0}
        self._consecutive_conn_errors = 0
        self.circuit_open = False
        self.pair_status = {}          # (src, dest) -> CACHE|FETCHED|NO_ROUTE|ERROR
        self.last_error = None

    # -- net --------------------------------------------------------------

    def _session(self):
        """One session per thread; a shared cookie jar under N threads is a bug farm."""
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.verify = self.verify_tls
            # A dead proxy in the environment produces a bare Errno 111. Set
            # RBS_NO_PROXY=1 to bypass it, or RBS_PROXY to point at a live one.
            if os.environ.get("RBS_NO_PROXY"):
                session.trust_env = False
            elif os.environ.get("RBS_PROXY"):
                session.proxies = {"http": os.environ["RBS_PROXY"],
                                   "https": os.environ["RBS_PROXY"]}
            session.headers.update({
                "User-Agent": USER_AGENT,
                "Referer": RBS_FORM_PAGE,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            })
            # Best-effort warm-up for JSESSIONID. Non-fatal: the original code
            # never did this and still worked, so a failure must not stop the POST.
            try:
                self._limiter.wait()
                session.get(RBS_FORM_PAGE, timeout=20)
            except Exception:
                pass
            self._local.session = session
        return session

    def _post(self, source, destination):
        if self.circuit_open:
            self._bump("skipped_circuit_open")
            raise RBSError("circuit open: RBS refused repeated connections")

        payload = dict(STATIC_FIELDS)
        payload[SRC_FIELD] = source
        payload[DEST_FIELD] = destination

        last = None
        for attempt in range(MAX_RETRIES):
            try:
                self._limiter.wait()
                response = self._session().post(self.url, data=payload, timeout=45)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise RBSError(f"HTTP {response.status_code} from RBS")
                response.raise_for_status()
                with self._stats_lock:
                    self._consecutive_conn_errors = 0
                return response.text, response.status_code
            except requests.exceptions.ConnectionError as exc:
                # No TCP handshake at all. Backing off will not help.
                last = exc
                with self._stats_lock:
                    self._consecutive_conn_errors += 1
                    if self._consecutive_conn_errors >= CIRCUIT_BREAK_AFTER:
                        self.circuit_open = True
                break
            except Exception as exc:
                last = exc
                if attempt < MAX_RETRIES - 1:
                    time.sleep((2 ** attempt) + random.uniform(0, 0.5))
        raise RBSError(f"{source}->{destination}: {type(last).__name__}: {last}")

    # -- parsing ----------------------------------------------------------

    @staticmethod
    def _rows_to_route(rows, code_i, dist_i, name_i=None):
        codes, junctions, max_distance = [], [], 0.0
        for row in rows:
            cols = row.find_all("td")
            if len(cols) <= max(code_i, dist_i):
                continue

            code = _code_from_cell(cols[code_i])
            if not _CODE_RE.match(code):
                continue
            codes.append(code)

            if JUNCTION_BY_NAME and name_i is not None and len(cols) > name_i:
                name = cols[name_i].get_text(" ", strip=True).upper()
                if re.search(r"\bJN\b|\bJUNCTION\b", name):
                    junctions.append(code)
            elif "JN" in code:
                junctions.append(code)

            # Tolerant: the cell may carry thousands separators or trailing text.
            cell = cols[dist_i].get_text(" ", strip=True).replace(",", "")
            match = _NUM_RE.search(cell)
            if match:
                max_distance = max(max_distance, float(match.group()))

        return max_distance, codes, junctions

    @classmethod
    def _parse(cls, html):
        """Positional layout first -- that is the one proven against this servlet.

        Header-aware detection is a fallback for a future redesign, not the
        primary path.
        """
        soup = BeautifulSoup(html, "html.parser")

        distance, codes, junctions = cls._rows_to_route(soup.find_all("tr"), 1, 3, 2)
        if codes:
            return distance, codes, junctions

        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue
            headers = [c.get_text(" ", strip=True).lower()
                       for c in rows[0].find_all(["th", "td"])]

            def find(*keywords):
                for index, header in enumerate(headers):
                    if any(k in header for k in keywords):
                        return index
                return None

            code_i, dist_i = find("code"), find("distance", "km", "dist")
            if code_i is None or dist_i is None:
                continue
            distance, codes, junctions = cls._rows_to_route(
                rows[1:], code_i, dist_i, find("name"))
            if codes:
                return distance, codes, junctions

        return 0.0, [], []

    @classmethod
    def parse_detailed(cls, html):
        """Per-station rows: [{code, name, cumulative_km}, ...].

        The pipeline only needs codes and a total, so _parse throws the names
        away. A human reading a route wants them, so this keeps them. Additive
        on purpose -- _parse stays exactly as it is, because it is the path the
        38k-row cache was built with.
        """
        soup = BeautifulSoup(html, "html.parser")
        stations = []
        for row in soup.find_all("tr"):
            cols = row.find_all("td")
            if len(cols) <= 3:
                continue
            code = _code_from_cell(cols[1])
            if not _CODE_RE.match(code):
                continue
            name = cols[2].get_text(" ", strip=True)
            cell = cols[3].get_text(" ", strip=True).replace(",", "")
            match = _NUM_RE.search(cell)
            stations.append({
                "code": code,
                "name": name,
                "cumulative_km": float(match.group()) if match else None,
            })
        return stations

    def lookup(self, source, destination):
        """One OD pair, with everything a human would want to see.

        Cache-first, so repeating a query costs nothing. Returns a dict rather
        than printing, so callers can tabulate or export.
        """
        source, destination = norm_code(source), norm_code(destination)
        cached = self.cache.get_route(source, destination, max_age_days=self.max_age_days)

        if cached and cached["status"] == "SUCCESS":
            self._bump("cache_hits")
            return {
                "source": source, "destination": destination,
                "distance_km": cached["distance"],
                "route": cached["route_sequence"],
                "junctions": cached["junctions"],
                "stations": None,          # names are not stored in the cache
                "origin": "cache",
                "fetched_at": cached.get("fetched_at"),
                "error": None,
            }

        try:
            html, _status = self._post(source, destination)
        except RBSError as exc:
            self._bump("errors")
            self.last_error = str(exc)
            return {"source": source, "destination": destination, "distance_km": None,
                    "route": [], "junctions": [], "stations": None,
                    "origin": "error", "fetched_at": None, "error": str(exc)}

        distance, codes, junctions = self._parse(html)
        detailed = self.parse_detailed(html)
        if not is_usable_route(source, destination, codes):
            self._bump("no_route")
            self.cache.set_route(source, destination, 0, [], [], status="FAILED")
            reason = ("RBS returned no route for this pair" if not codes else
                      f"RBS returned only {codes[0]} -- check that {destination} "
                      "is a valid station code")
            return {"source": source, "destination": destination, "distance_km": None,
                    "route": [], "junctions": [], "stations": [],
                    "origin": "no_route", "fetched_at": None, "error": reason}

        self._bump("fetched")
        self.cache.set_route(source, destination, distance, codes, junctions,
                             status="SUCCESS")
        return {
            "source": source, "destination": destination, "distance_km": distance,
            "route": codes, "junctions": junctions, "stations": detailed,
            "origin": "fetched", "fetched_at": "just now", "error": None,
        }

    # -- single -----------------------------------------------------------

    def _bump(self, key):
        with self._stats_lock:
            self.stats[key] += 1

    def _mark(self, source, destination, state):
        with self._stats_lock:
            self.pair_status[(source, destination)] = state

    def _fetch_single_route(self, source, destination):
        source, destination = norm_code(source), norm_code(destination)
        try:
            html, http_status = self._post(source, destination)
            distance, route_sequence, junctions = self._parse(html)
        except RBSError as exc:
            # A portal problem is NOT "no route exists". Recorded as ERROR so
            # status_counts() and the audit can tell them apart, and so
            # purge_status() can clear them before a retry.
            self._bump("errors")
            self._mark(source, destination, "ERROR")
            self.last_error = str(exc)
            self.cache.set_route(source, destination, 0, [], [], status="ERROR")
            if self.strict:
                raise
            return source, destination, None, [], [], None

        # A single station returned for two different codes is the portal
        # echoing the origin back, not a route. Recorded as NO_ROUTE for the
        # same reason an empty parse is: a 0 km SUCCESS row would be believed
        # by everything downstream and never looked at again.
        if not is_usable_route(source, destination, route_sequence):
            self._bump("no_route")
            self._mark(source, destination, "NO_ROUTE")
            self.cache.set_route(source, destination, 0, [], [], status="FAILED",
                                 http_status=http_status)
            return source, destination, None, [], [], None

        self._bump("fetched")
        self._mark(source, destination, "FETCHED")
        self.cache.set_route(source, destination, distance, route_sequence, junctions,
                             status="SUCCESS", http_status=http_status)
        return source, destination, distance, route_sequence, junctions, "just now"

    # -- batch ------------------------------------------------------------

    def get_routes_batch(self, od_pairs, progress_callback=None, max_workers=None):
        """Resolve many OD pairs: the whole cache in one read, then fetch misses.

        Reading the cache in bulk first matters more than it looks. A typical
        run is almost entirely cache hits, and resolving those with one query
        rather than one connection per pair is what keeps a fully-cached
        full-year run to seconds instead of many minutes.
        """
        if not od_pairs:
            return {}

        unique_pairs = list(dict.fromkeys(
            (norm_code(s), norm_code(d)) for s, d in od_pairs))
        results = {}

        cached = self.cache.get_routes_bulk(unique_pairs, max_age_days=self.max_age_days)
        misses = []
        for pair in unique_pairs:
            row = cached.get(pair)
            if row and row["status"] == "SUCCESS":
                results[pair] = (row["distance"], row["route_sequence"],
                                 row["junctions"], row.get("fetched_at"))
                self._bump("cache_hits")
                self._mark(pair[0], pair[1], "CACHE")
            else:
                misses.append(pair)

        if progress_callback:
            progress_callback(len(results), len(unique_pairs))
        if not misses:
            return results

        completed = len(results)
        workers = max_workers or self.workers
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(self._fetch_single_route, src, dest)
                       for src, dest in misses]
            for future in concurrent.futures.as_completed(futures):
                src, dest, distance, route, junctions, fetched_at = future.result()
                results[(src, dest)] = (distance, route, junctions, fetched_at)
                completed += 1
                if progress_callback and completed % 100 == 0:
                    progress_callback(completed, len(unique_pairs))

        return results

    # -- diagnostic -------------------------------------------------------

    def diagnose(self, source="NGP", destination="BPL"):
        """Run ONE request and report what actually came back.

        Call this from the machine that cannot reach the portal, before blaming
        the parser.
        """
        payload = dict(STATIC_FIELDS)
        payload[SRC_FIELD] = source
        payload[DEST_FIELD] = destination

        out = {"url": self.url, "payload": payload, "verify_tls": self.verify_tls}
        try:
            session = self._session()
            response = session.post(self.url, data=payload, timeout=45)
            html = response.text
            soup = BeautifulSoup(html, "html.parser")
            distance, codes, _junctions = self._parse(html)
            out.update({
                "http_status": response.status_code,
                "bytes": len(html),
                "cookies": dict(session.cookies),
                "tables_found": len(soup.find_all("table")),
                "rows_found": len(soup.find_all("tr")),
                "parsed_distance": distance,
                "parsed_stations": len(codes),
                "route_head": codes[:6],
                "looks_like_login_or_error": any(
                    w in html.lower()
                    for w in ("login", "session expired", "exception", "error")
                ),
                "snippet": re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:400],
            })
        except Exception as exc:
            out["exception"] = f"{type(exc).__name__}: {exc}"
        return out


if __name__ == "__main__":
    import json
    import sys

    scraper = RBSScraper(strict=True)
    if "--diagnose" in sys.argv:
        print(json.dumps(scraper.diagnose(), indent=2, default=str))
    else:
        print(scraper.get_routes_batch([("NGP", "BPL"), ("DURG", "RJN")]))
        print(scraper.stats)
