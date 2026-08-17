"""Agent 2 -- shortest-path routes from the RBS portal, cached.

The cache is what makes this tool usable: a full-year OD set is tens of
thousands of station pairs, and fetching them live takes hours. It is also what
makes it dangerous, because a cached route has no expiry unless one is imposed.
Every lookup here therefore carries a maximum age, and a row older than that --
or one with no recorded fetch date at all -- is refetched rather than served.
"""
import concurrent.futures
import threading
import time

import requests
from bs4 import BeautifulSoup

from config import RBS_URL
from core.cache import RBSCache

# The portal presented a valid certificate chain when this was last checked
# (2026-08-17). Verification stays on: these distances end up in client
# deliverables, and an unverified response can be rewritten in transit.
VERIFY_TLS = True

DEFAULT_WORKERS = 12


class RBSScraper:
    def __init__(self, max_age_days=None, workers=DEFAULT_WORKERS, verify_tls=VERIFY_TLS):
        self.cache = RBSCache()
        self.max_age_days = max_age_days
        self.workers = workers
        self.verify_tls = verify_tls
        self._local = threading.local()
        self._lock = threading.Lock()
        self.stats = {"cache_hits": 0, "fetched": 0, "failed": 0, "no_route": 0}

    def _session(self):
        # requests.Session is not thread-safe; one per worker thread.
        if not hasattr(self._local, "session"):
            session = requests.Session()
            session.verify = self.verify_tls
            session.headers.update(
                {"User-Agent": "Mozilla/5.0 (compatible; corridor-assessment/1.0)"}
            )
            self._local.session = session
        return self._local.session

    @staticmethod
    def parse_route(html):
        soup = BeautifulSoup(html, "html.parser")
        sequence, distance = [], 0.0
        for row in soup.find_all("tr"):
            cols = row.find_all("td")
            if len(cols) > 3:
                code = cols[1].text.strip().split("\n")[0].strip()
                if code and len(code) <= 8 and " " not in code:
                    sequence.append(code)
                    text = cols[3].text.strip()
                    if text.replace(".", "", 1).isdigit():
                        distance = float(text)
        return sequence, distance

    def _bump(self, key):
        with self._lock:
            self.stats[key] += 1

    def _fetch_single_route(self, source, destination, retries=2):
        payload = {
            "srcCode": source, "destCode": destination,
            "gaugeType": "S", "distance": "goods", "PageName": "ShortPath",
        }
        for attempt in range(retries + 1):
            try:
                response = self._session().post(RBS_URL, data=payload, timeout=45)
                response.raise_for_status()
                sequence, distance = self.parse_route(response.text)
                if not sequence:
                    # A genuine "no path", distinct from a transport failure.
                    self.cache.set_route(source, destination, 0, [], [], status="FAILED",
                                         http_status=response.status_code)
                    self._bump("no_route")
                    return source, destination, None, [], [], None
                junctions = [code for code in sequence if "JN" in code]
                self.cache.set_route(source, destination, distance, sequence, junctions,
                                     status="SUCCESS", http_status=response.status_code)
                self._bump("fetched")
                return source, destination, distance, sequence, junctions, "just now"
            except Exception:
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                # Do not cache a transport failure as a routing answer -- the
                # pair is fine, the network was not, and writing FAILED here
                # would poison the cache for every later run.
                self._bump("failed")
                stale = self.cache.get_route(source, destination)
                if stale and stale["status"] == "SUCCESS":
                    return (source, destination, stale["distance"], stale["route_sequence"],
                            stale["junctions"], stale.get("fetched_at"))
                return source, destination, None, [], [], None
        return source, destination, None, [], [], None

    def get_routes_batch(self, od_pairs, progress_callback=None):
        """Resolve many OD pairs: the whole cache in one read, then fetch misses.

        Reading the cache in bulk first matters more than it looks. A typical
        run is almost entirely cache hits, and resolving those with one query
        rather than one connection per pair is what keeps a fully-cached
        full-year run to seconds instead of many minutes.
        """
        if not od_pairs:
            return {}

        unique_pairs = list(dict.fromkeys(od_pairs))  # de-duplicate, keep order
        results = {}

        cached = self.cache.get_routes_bulk(unique_pairs, max_age_days=self.max_age_days)
        misses = []
        for pair in unique_pairs:
            row = cached.get(pair)
            if row and row["status"] == "SUCCESS":
                results[pair] = (row["distance"], row["route_sequence"],
                                 row["junctions"], row.get("fetched_at"))
                self._bump("cache_hits")
            else:
                misses.append(pair)

        if progress_callback:
            progress_callback(len(results), len(unique_pairs))
        if not misses:
            return results

        completed = len(results)
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = [executor.submit(self._fetch_single_route, src, dest)
                       for src, dest in misses]
            for future in concurrent.futures.as_completed(futures):
                src, dest, distance, route, junctions, fetched_at = future.result()
                results[(src, dest)] = (distance, route, junctions, fetched_at)
                completed += 1
                if progress_callback and completed % 100 == 0:
                    progress_callback(completed, len(unique_pairs))

        return results
