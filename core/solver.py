"""
core/solver.py — NoneCap captcha solver (the APIs the user already paid for).

MEASURED FACTS (2026-11-04)
  * NoneCap API works, key live, 715 credits.
  * Endpoint: POST https://api.nonecap.com/v1/solves?wait=N
  * Poll:     GET  https://api.nonecap.com/v1/solves/{id}
  * Returns a P1_ / E0_ token to inject into the form field.
  * SUPPORTED TYPES ONLY: hcaptcha, hcaptcha_enterprise, recaptcha_v3.
    There is NO recaptcha_v2 type -> v2 sitekeys are sent as recaptcha_v3.
  * Balance: GET https://api.nonecap.com/v1/me -> credits_balance

THE BUG THIS FIXES
  The bot clicked Submit, a puzzle appeared, the widget never got solved, and
  it still recorded "submitted". Now we:
    1. detect whether a captcha widget ACTUALLY renders (not a substring match)
    2. read the real sitekey from the live widget
    3. wait for the user token to populate the response field
    4. if it stays empty, solve via NoneCap and inject the token
    5. only report SUCCESS if a real success signal appears after submit
"""

import os
import re
import time

import httpx

BASE_API = "https://api.nonecap.com"
WAIT_MAX = 90


def _key():
    k = os.environ.get("NONECAP_API_KEY", "").strip()
    if k:
        return k
    # fall back to .secrets/env.txt
    for p in (r"C:\Users\SellJ\Desktop\OUTREACH el\.secrets\env.txt",):
        try:
            for l in open(p, encoding="utf-8"):
                l = l.strip()
                if l.startswith("NONECAP_API_KEY="):
                    v = l.split("=", 1)[1].strip().strip("]")
                    os.environ["NONECAP_API_KEY"] = v
                    return v
        except Exception:
            pass
    return ""


class Solver:
    def __init__(self, timeout=150):
        self.key = _key()
        self.enabled = bool(self.key)
        self.c = httpx.Client(timeout=timeout)
        self.solved = 0
        self.failed = 0
        self.last_error = ""

    @property
    def _h(self):
        return {"Authorization": "Bearer " + self.key,
                "Content-Type": "application/json"}

    def balance(self):
        try:
            r = self.c.get(BASE_API + "/v1/me", headers=self._h)
            return (r.json() or {}).get("credits_balance")
        except Exception:
            return None

    def solve(self, kind, sitekey, url, user_agent=None):
        """
        Returns (token, note). token is '' on failure.

        The KIND IS CORRECTED FROM THE KEY FORMAT, because selectors lie:
        graff.com matched an hCaptcha element but carried a Google key
        ("6LdNfLIZ..."), and sending that as type=hcaptcha is a 422.
          * UUID          -> hcaptcha
          * 6.../6L...    -> recaptcha  (API type recaptcha_v3; there is no v2)
          * 0x...         -> turnstile  (not supported by the API)
        """
        if not self.enabled:
            return "", "no key"

        k = (sitekey or "").strip()
        if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", k, re.I):
            kind = "hcaptcha"
        elif re.match(r"^6[0-9A-Za-z_\-]{30,}$", k):
            kind = "recaptcha_v3"          # API has no v2; v3 accepts v2 keys
        elif re.match(r"^0x[0-9A-Fa-f]+$", k):
            kind = "turnstile"
        if kind not in ("hcaptcha", "hcaptcha_enterprise", "recaptcha_v3"):
            kind = "recaptcha_v3" if not k else "hcaptcha"

        body = {"type": kind, "sitekey": sitekey, "url": url}
        if user_agent:
            body["user_agent"] = user_agent
        try:
            r = self.c.post(BASE_API + "/v1/solves",
                            params={"wait": WAIT_MAX}, headers=self._h, json=body)
        except Exception as e:
            self.failed += 1
            return "", "post " + type(e).__name__
        if r.status_code != 200:
            self.failed += 1
            self.last_error = r.text[:180]
            return "", "http%d %s" % (r.status_code, r.text[:90])
        d = r.json()
        tok = d.get("token")
        if tok:
            self.solved += 1
            return tok, "solved"
        sid = d.get("id") or d.get("solve_id")
        if not sid:
            self.failed += 1
            return "", "no id"
        for _ in range(30):
            time.sleep(3)
            try:
                p = self.c.get(BASE_API + "/v1/solves/" + str(sid), headers=self._h)
                pd = p.json()
            except Exception:
                continue
            tok = pd.get("token")
            if tok:
                self.solved += 1
                return tok, "solved(poll)"
            st = (pd.get("state") or pd.get("status") or "").lower()
            if st in ("failed", "error", "canceled", "cancelled"):
                self.failed += 1
                return "", "state=" + st
        self.failed += 1
        return "", "poll timeout"

    def report(self, solve_id, outcome="rejected"):
        try:
            self.c.post(BASE_API + "/v1/feedback", headers=self._h,
                        json={"feedback": [{"solve_id": solve_id,
                                            "outcome": outcome}]})
        except Exception:
            pass

    def stats(self):
        return "solver: %d solved / %d failed" % (self.solved, self.failed)


_SOLVER = None


def get_solver():
    global _SOLVER
    if _SOLVER is None:
        _SOLVER = Solver()
    return _SOLVER
