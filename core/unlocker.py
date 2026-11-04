"""
core/unlocker.py — Bright Data Web Unlocker fallback.

Used ONLY when plain httpx fails: 403s, WAF block pages, or captcha walls.
Using it for everything costs 3-13s per request instead of ~1s, so the fast
path stays plain HTTP and this is the safety net.

── WHY THIS WAS REWRITTEN (2026-11-03) ───────────────────────────────────────

The old implementation POSTed to `api.brightdata.com/request` and got a bare
`502 Bad Gateway` on EVERY call, including example.com. That endpoint is the
wrong one for a Web Unlocker zone, and it fails at the HTTP layer with no
explanation, so the failure looked like an outage.

The correct Web Unlocker flow is asynchronous, two calls:

    POST /unblocker/req       {"zone":...,"url":...}  -> {"response_id":...}
    GET  /unblocker/get_result?response_id=...         -> 200 body | 202 pending

It delegates to core.bd_unlocker, which owns that handshake.

── TWO FAILURE MODES THAT MUST NOT BE CONFLATED ─────────────────────────────

  * 402 "Residential Failed (bad_endpoint) ... no KYC" — the TARGET blocks
    BrightData's non-KYC residential pool. Permanent until KYC is filled.
    Confirmed to affect: yellowpages, superpages, manta, dexknows.
    Retrying those in a loop wastes 8-13s each, so they are cached per run.
  * 410/404/5xx — gone or transient; worth one retry.

The 402 comes back as a JSON body on an HTTP 200 poll, so it must be sniffed
from the body, never from the status code.
"""

import os
import sys

# Running this file directly puts core/ on sys.path, not the repo root, so
# `from core import ...` would fail before __main__ ever runs.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from core import envload
from core.bd_unlocker import BrightDataUnlocker


class Unlocker:
    """Thin wrapper. Returns (status, html, note) where html is '' on failure."""

    def __init__(self, timeout=20):
        # force=True: load_env() is idempotent, and a caller may have already
        # triggered it before this module imported.
        envload.load_env(force=True)
        self.key, self.zone = envload.brightdata()
        # 90s was the original default. When the zone is dead that burns 90
        # SECONDS PER PAGE, which measured ~96% of total wall-clock time.
        # 20s is ample for a working unlocker and caps the damage when broken.
        self.timeout = timeout
        self.enabled = bool(self.key)
        self.calls = 0
        self.wins = 0
        self.last_error = ""
        self._bd = None
        # Hosts proven to reject non-KYC residential access. Once seen, skip
        # them for the rest of the run instead of paying the round-trip again.
        self._kyc_hosts = set()
        if self.enabled:
            try:
                self._bd = BrightDataUnlocker(self.key, self.zone, timeout=timeout)
            except Exception as e:
                self.last_error = f"{type(e).__name__}"
                self._bd = None

    def get(self, url):
        """
        Fetch via Bright Data. Returns (status_code, html, note).
        html is '' if the unlocker could not get a real page.
        """
        if not self.enabled or self._bd is None:
            return 0, "", self.last_error or "no key"

        # Circuit breaker: if the first few calls all fail, stop paying the
        # latency penalty on every remaining page in the run.
        if self.calls >= 3 and self.wins == 0:
            return 0, "", "circuit-open (unlocker dead this run)"

        host = ""
        try:
            from urllib.parse import urlparse
            host = urlparse(url).netloc.lower()
        except Exception:
            pass

        # Already known to 402 on this host — don't spend another round-trip.
        if host and host in self._kyc_hosts:
            return 402, "", "kyc_blocked (cached)"
        # Bright Data refuses these hosts entirely.
        if not worth_trying(url):
            return 0, "", "host refused"

        self.calls += 1
        html, note = self._bd.fetch(url)

        if html:
            self.wins += 1
            return 200, html, ""

        self.last_error = note
        if note == "kyc_blocked" and host:
            self._kyc_hosts.add(host)
        return 0, "", note

    def stats(self):
        extra = f" ({len(self._kyc_hosts)} kyc-blocked hosts)" if self._kyc_hosts else ""
        return f"unlocker: {self.wins}/{self.calls} useful{extra}"

    def close(self):
        if self._bd:
            self._bd.close()


# Bright Data refuses some hosts entirely — skip them, no point retrying.
_REFUSED = ("gov.uk", "gov.com")


def worth_trying(url):
    low = (url or "").lower()
    return not any(h in low for h in _REFUSED)


if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    u = Unlocker()
    print("enabled:", u.enabled)
    for t in ("https://example.com/",
              "https://www.hotfrog.com/search/CA/jewelry",
              "https://www.yellowpages.com/search?search_terms=jewelry"):
        st, html, note = u.get(t)
        print(f"  {st:>4} len={len(html):>7} note={note!r:<22} {t[:52]}")
    print(u.stats())
