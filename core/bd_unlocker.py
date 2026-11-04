"""
core/bd_unlocker.py — working BrightData Web Unlocker client.

WHY THIS REPLACES core/unlocker.py's request path:

The old code POSTed to `api.brightdata.com/request` and always got a bare
`502 Bad Gateway`. That endpoint is the wrong one for a Web Unlocker zone —
and worse, it fails *silently* at the HTTP layer.

The correct Web Unlocker flow is ASYNC, two calls:

    POST /unblocker/req        {"zone": ..., "url": ...}  -> {"response_id": ...}
    GET  /unblocker/get_result ?response_id=...            -> 200 body | 202 not-ready

Verified working 2026-11-03 against example.com, hotfrog, merchantcircle.

Two distinct failure modes must NOT be confused, because one is retryable and
the other is permanent:

  * HTTP 402 + "Residential Failed (bad_endpoint) ... no KYC" — the target
    blocks BrightData's non-KYC residential pool. Affects yellowpages,
    superpages, manta, dexknows. NOT retryable; fill the KYC form to fix.
  * HTTP 410/404/5xx — genuinely gone or transient.

The 402 arrives as a JSON body with HTTP 200 on the poll, so it must be
sniffed from the body, never from the status code.
"""

import json
import time

import httpx

BASE = "https://api.brightdata.com"
SUBMIT = f"{BASE}/unblocker/req"
RESULT = f"{BASE}/unblocker/get_result"

# Not retryable: the site rejects non-KYC residential access outright.
KYC_MARKER = "no KYC"


class BrightDataError(Exception):
    pass


class BrightDataUnlocker:
    def __init__(self, api_key=None, zone="web_unlocker1", timeout=90,
                 poll_interval=2.0, max_wait=120):
        if not api_key:
            raise BrightDataError("no BrightData API key")
        self.key = api_key
        self.zone = zone
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_wait = max_wait
        self._client = httpx.Client(timeout=timeout)
        self.stats = {"submit": 0, "ok": 0, "kyc_blocked": 0, "gone": 0,
                      "not_found": 0, "error": 0, "timeout": 0}

    @property
    def _h(self):
        return {"Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json"}

    def _classify(self, body):
        """Return (kind, detail) from a JSON error body, or (None, None)."""
        try:
            d = json.loads(body)
        except Exception:
            return None, None
        if not isinstance(d, dict) or not d.get("error") and "status_code" not in d:
            return None, None
        code = d.get("status_code")
        msg = d.get("message", "")
        if KYC_MARKER in msg or code == 402:
            return "kyc_blocked", msg
        if code == 410:
            return "gone", msg
        if code == 404:
            return "not_found", msg
        return "error", f"{code} {msg}"

    def fetch(self, url, zone=None):
        """
        Return (html, note). `html` is None on failure; `note` explains why.
        Never raises for a per-URL failure — a batch must survive one bad site.
        """
        z = zone or self.zone
        try:
            r = self._client.post(SUBMIT, headers=self._h,
                                  json={"zone": z, "url": url})
        except Exception as e:
            self.stats["error"] += 1
            return None, f"submit {type(e).__name__}"

        self.stats["submit"] += 1
        if r.status_code != 200:
            # the old code path died here with an unhelpful 502
            self.stats["error"] += 1
            return None, f"submit http{r.status_code}"

        rid = (r.json() or {}).get("response_id")
        if not rid:
            self.stats["error"] += 1
            return None, "no response_id"

        t0 = time.time()
        while time.time() - t0 < self.max_wait:
            try:
                p = self._client.get(RESULT, headers=self._h,
                                     params={"response_id": rid})
            except Exception as e:
                self.stats["error"] += 1
                return None, f"poll {type(e).__name__}"

            if p.status_code == 202:
                time.sleep(self.poll_interval)
                continue
            if p.status_code == 200:
                kind, detail = self._classify(p.text)
                if kind:
                    self.stats[kind] += 1
                    return None, kind
                self.stats["ok"] += 1
                return p.text, "ok"
            # 404 can mean "not ready yet" on some accounts - re-poll briefly
            if p.status_code in (404, 425):
                time.sleep(self.poll_interval)
                continue
            self.stats["error"] += 1
            return None, f"poll http{p.status_code}"

        self.stats["timeout"] += 1
        return None, "timeout"

    def enabled(self):
        return True

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass

    def summary(self):
        s = self.stats
        return (f"bd: {s['ok']} ok / {s['submit']} sent  "
                f"(kyc-blocked {s['kyc_blocked']}, gone {s['gone']}, "
                f"err {s['error']}, timeout {s['timeout']})")


def from_env():
    """Build from .secrets, or None when no key/zone is configured."""
    import os
    # force=True: load_env() is idempotent-guarded, and another module may
    # already have run it before this one imported. Without force, a caller
    # that imports bd_unlocker first can silently see an empty key.
    try:
        from core import envload
        envload.load_env(force=True)
    except Exception:
        pass
    key = os.environ.get("BRIGHTDATA_API_KEY", "").strip()
    zone = os.environ.get("BRIGHTDATA_ZONE", "web_unlocker1").strip()
    if not key:
        return None
    try:
        return BrightDataUnlocker(key, zone)
    except BrightDataError:
        return None


if __name__ == "__main__":
    # Running this file directly puts core/ on sys.path, not the repo root,
    # so `from core import ...` would fail. Add the parent too.
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    u = from_env()
    if not u:
        print("no BRIGHTDATA_API_KEY")
        raise SystemExit(1)
    for target in ("https://example.com/",
                   "https://www.hotfrog.com/search/CA/jewelry",
                   "https://www.yellowpages.com/search?search_terms=jewelry&geo_location_terms=CA"):
        html, note = u.fetch(target)
        print(f"{note:<14} len={len(html) if html else 0:>7}  {target[:60]}")
    print(u.summary())
