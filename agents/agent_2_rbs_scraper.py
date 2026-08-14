"""
RBS (Rates Branch System) scraper - https://rbs.indianrail.gov.in/ShortPath/

Changes vs the original:
  * Targets the real JSP app: GETs the form page first (cookies + hidden fields),
    then POSTs to the form's own action URL instead of a hardcoded RBS_URL.
  * Field names are auto-discovered from the form and merged with FIELD_MAP,
    so you only edit one dict if CRIS renames a parameter.
  * Header-aware table parsing (finds the station/distance columns by their
    header text) with a positional fallback.
  * Junctions detected from the station NAME column, not the code. Codes are
    things like "SBC"/"TK" - they almost never contain "JN".
  * Distance = max cumulative value seen, not "whatever the last row parsed".
  * Thread-local sessions, retries with backoff, and a global rate limiter.
    20 concurrent workers against a CRIS box will get you throttled or blocked.
"""

import random
import re
import threading
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import concurrent.futures
import warnings

from core.cache import RBSCache

warnings.filterwarnings('ignore', message='Unverified HTTPS request')

# --- Endpoints -------------------------------------------------------------
RBS_BASE = "https://rbs.indianrail.gov.in/ShortPath/"
RBS_FORM_URL = urljoin(RBS_BASE, "ShortPath.jsp")          # the page with the form
RBS_FALLBACK_ACTION = urljoin(RBS_BASE, "ShortPathServlet")  # used if form has no action

# --- Request payload -------------------------------------------------------
# Verify these against DevTools -> Network -> the POST fired by "Get Distance",
# then copy the exact names here. Everything else (hidden fields, tokens) is
# picked up automatically from the form.
FIELD_MAP = {
    "source": "srcCode",       # e.g. srcCode / fromStn / sourceStn
    "destination": "destCode",  # e.g. destCode / toStn / destStn
}
STATIC_FIELDS = {
    "gaugeType": "S",      # S = broad gauge ("standard" in RBS terms)
    "distance": "goods",   # goods | coaching
    "PageName": "ShortPath",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

MAX_WORKERS = 5
MIN_INTERVAL = 0.35   # seconds between requests, globally
MAX_RETRIES = 3

_CODE_RE = re.compile(r"^[A-Z0-9]{2,8}$")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


class RateLimiter:
    """Global minimum spacing between outbound requests."""

    def __init__(self, min_interval):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap + random.uniform(0, 0.15))
            self._last = time.monotonic()


class RBSScraper:
    def __init__(self, form_url=RBS_FORM_URL):
        self.cache = RBSCache()
        self.form_url = form_url
        self._local = threading.local()
        self._limiter = RateLimiter(MIN_INTERVAL)
        self._form_lock = threading.Lock()
        self._form_spec = None  # (action_url, hidden_fields, known_field_names)

    # -- session / form bootstrap ------------------------------------------
    def _session(self):
        """One session per thread: cookie jars are not safe to mutate concurrently."""
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.verify = False  # CRIS chain is frequently incomplete
            s.headers.update({
                "User-Agent": USER_AGENT,
                "Referer": self.form_url,
                "Origin": "https://rbs.indianrail.gov.in",
            })
            self._local.session = s
        return s

    def _form_spec_for(self, session):
        """Fetch the form once, cache action URL + hidden inputs for all threads."""
        with self._form_lock:
            if self._form_spec is not None:
                # still need this thread's session to hold a JSESSIONID
                if not session.cookies:
                    self._limiter.wait()
                    session.get(self.form_url, timeout=30)
                return self._form_spec

            self._limiter.wait()
            r = session.get(self.form_url, timeout=30)
            soup = BeautifulSoup(r.text, "html.parser")
            form = soup.find("form")

            if form is None:
                self._form_spec = (RBS_FALLBACK_ACTION, {}, set())
                return self._form_spec

            action = urljoin(self.form_url, form.get("action") or RBS_FALLBACK_ACTION)
            hidden, names = {}, set()
            for el in form.find_all(["input", "select", "textarea"]):
                name = el.get("name")
                if not name:
                    continue
                names.add(name)
                if (el.get("type") or "").lower() == "hidden":
                    hidden[name] = el.get("value", "") or ""

            self._form_spec = (action, hidden, names)
            return self._form_spec

    def _build_payload(self, source, destination, hidden, names):
        payload = dict(hidden)
        for k, v in STATIC_FIELDS.items():
            if not names or k in names:
                payload[k] = v
        payload[FIELD_MAP["source"]] = source
        payload[FIELD_MAP["destination"]] = destination
        return payload

    def _post_with_retry(self, session, action, payload):
        last_exc = None
        for attempt in range(MAX_RETRIES):
            try:
                self._limiter.wait()
                r = session.post(action, data=payload, timeout=30)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                return r
            except Exception as e:      # noqa: BLE001 - retry on anything transient
                last_exc = e
                time.sleep((2 ** attempt) + random.uniform(0, 0.5))
        raise last_exc

    # -- parsing ------------------------------------------------------------
    @staticmethod
    def _column_index(headers, *keywords):
        for i, h in enumerate(headers):
            low = h.lower()
            if any(k in low for k in keywords):
                return i
        return None

    @classmethod
    def _parse_route_table(cls, soup):
        """Return (distance, [codes], [junction codes]) from the results table."""
        best = (0.0, [], [])

        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue

            headers = [c.get_text(" ", strip=True)
                       for c in rows[0].find_all(["th", "td"])]
            code_i = cls._column_index(headers, "code")
            name_i = cls._column_index(headers, "station name", "name")
            dist_i = cls._column_index(headers, "distance", "km", "dist")

            if code_i is None or dist_i is None:
                # Fallback to the original layout: code in col 1, distance in col 3
                code_i, name_i, dist_i, data_rows = 1, 2, 3, rows
            else:
                data_rows = rows[1:]

            codes, junctions, max_dist = [], [], 0.0
            for row in data_rows:
                cols = row.find_all("td")
                if len(cols) <= max(code_i, dist_i):
                    continue

                code = cols[code_i].get_text(" ", strip=True).split("\n")[0].strip().upper()
                if not _CODE_RE.match(code):
                    continue
                codes.append(code)

                name = ""
                if name_i is not None and len(cols) > name_i:
                    name = cols[name_i].get_text(" ", strip=True).upper()
                # RBS writes junctions as "... JN" in the name column
                if re.search(r"\bJN\b|\bJUNCTION\b", name) or re.search(r"\bJN\b", code):
                    junctions.append(code)

                m = _NUM_RE.search(cols[dist_i].get_text(" ", strip=True).replace(",", ""))
                if m:
                    max_dist = max(max_dist, float(m.group()))

            if len(codes) > len(best[1]):
                best = (max_dist, codes, junctions)

        return best

    # -- single fetch -------------------------------------------------------
    def _fetch_single_route(self, source, destination):
        source, destination = source.strip().upper(), destination.strip().upper()

        cached = self.cache.get_route(source, destination)
        if cached and cached['status'] == 'SUCCESS':
            return (source, destination, cached['distance'],
                    cached['route_sequence'], cached['junctions'])

        try:
            session = self._session()
            action, hidden, names = self._form_spec_for(session)
            payload = self._build_payload(source, destination, hidden, names)

            r = self._post_with_retry(session, action, payload)
            soup = BeautifulSoup(r.text, "html.parser")

            distance, route_sequence, junctions = self._parse_route_table(soup)

            if not route_sequence or distance <= 0:
                self.cache.set_route(source, destination, 0, [], [], status="FAILED")
                return source, destination, None, [], []

            self.cache.set_route(source, destination, distance,
                                 route_sequence, junctions, status="SUCCESS")
            return source, destination, distance, route_sequence, junctions

        except Exception:  # noqa: BLE001
            self.cache.set_route(source, destination, 0, [], [], status="FAILED")
            return source, destination, None, [], []

    # -- batch --------------------------------------------------------------
    def get_routes_batch(self, od_pairs, max_workers=MAX_WORKERS):
        """Fetch multiple OD pairs. od_pairs: list of (source, destination)."""
        if not od_pairs:
            return {}

        unique = list({(s.strip().upper(), d.strip().upper()) for s, d in od_pairs})
        results = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(self._fetch_single_route, s, d) for s, d in unique]
            for fut in concurrent.futures.as_completed(futures):
                src, dest, dist, route, juncs = fut.result()
                results[(src, dest)] = (dist, route, juncs)

        return results


if __name__ == "__main__":
    scraper = RBSScraper()
    print(scraper.get_routes_batch([("SBC", "TK"), ("SC", "BZA")]))
