"""
core/unlocker.py — Bright Data Web Unlocker fallback.

Used ONLY when plain httpx fails: 403s, WAF block pages, or captcha walls.
Using it for everything costs 3-13s per request instead of ~1s, so the fast
path stays plain HTTP and this is the safety net.

Bright Data returns HTTP 200 with an EMPTY BODY on failure and puts the reason
in the x-brd-error header, so both body length and header must be checked.
"""

import time

import httpx

from core import envload

API = "https://api.brightdata.com/request"


class Unlocker:
    """Thin wrapper. Returns (status, html) where html is '' on failure."""

    def __init__(self, timeout=90):
        self.key, self.zone = envload.brightdata()
        self.timeout = timeout
        self.enabled = bool(self.key)
        self.calls = 0
        self.wins = 0
        self.last_error = ""

    def get(self, url):
        """
        Fetch via Bright Data. Returns (status_code, html, note).
        html is '' if the unlocker could not get a real page.
        """
        if not self.enabled:
            return 0, "", "no key"
        self.calls += 1
        try:
            r = httpx.post(
                API,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.key}"},
                json={"zone": self.zone, "url": url, "format": "raw"},
                timeout=self.timeout,
            )
        except Exception as e:
            self.last_error = f"{type(e).__name__}"
            return 0, "", self.last_error

        err = r.headers.get("x-brd-error", "")
        body = r.text or ""

        # 200 + empty body + error header = the unlocker gave up
        if err:
            self.last_error = err[:120]
            return r.status_code, "", err[:120]
        if len(body) < 200:
            self.last_error = "tiny body"
            return r.status_code, "", "tiny body"

        self.wins += 1
        return r.status_code, body, ""

    def stats(self):
        return f"unlocker: {self.wins}/{self.calls} useful"


# Bright Data refuses some hosts entirely — skip them, no point retrying.
_REFUSED = ("gov.uk", "gov.com")


def worth_trying(url):
    low = (url or "").lower()
    return not any(h in low for h in _REFUSED)
