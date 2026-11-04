"""
core/submitter_http.py Ã¢â‚¬â€ Tier 1: fill and POST a form without a browser.

Fast path. Works whenever the form is server-rendered and does not need JS.
Carries cookies and any CSRF token it can find.
"""

import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from core import fieldmap, formdetect
from core.soup import safe_parse as _safe_parse

SUCCESS_WORDS = re.compile(
    r"(thank|thanks|cheers|success|sent|received|we'?ll be in touch|"
    r"get back to you|appreciate|submitted|message has been|"
    r"got your message|touch soon|be in contact)",
    re.I,
)

FAIL_WORDS = re.compile(
    r"(required|invalid|error|please fill|must be|captcha|spam|"
    r"something went wrong|try again)",
    re.I,
)


def detect_block(resp_or_status, body=""):
    """
    Recognise a WAF/CDN block page so we report 'blocked' rather than the
    misleading 'no form found'.

    A real page can mention 'captcha' in its own form, so the reliable signal
    is: an error-ish status AND a small body. A 403 with a short stub is a
    block; a 200 with 60KB is a genuine page.
    """
    code = getattr(resp_or_status, "status_code", resp_or_status)
    body = body or ""
    low = body.lower()

    hard_markers = (
        "just a moment", "checking your browser", "you have been blocked",
        "access denied", "attention required", "enable javascript and cookies",
        "ddos protection by", "sucuri website firewall",
    )
    if any(m in low for m in hard_markers):
        return "waf"

    if code in (403, 429, 503) and len(body) < 30000:
        return f"http{code}"

    return None


def _unlocked_fetch(url):
    """
    Try Bright Data Web Unlocker for a blocked URL. Returns html or None.

    One shared Unlocker instance for the process; the per-host 'worth trying'
    check happens here, not at construction, so gov.uk-style refusals are
    skipped without disabling the unlocker for other sites.
    """
    global _UNLOCKER
    try:
        from core.unlocker import Unlocker, worth_trying
        if not worth_trying(url):
            return None
        if _UNLOCKER is None:
            _UNLOCKER = Unlocker()
        if not _UNLOCKER.enabled:
            return None
        _status, html, _note = _UNLOCKER.get(url)
        return html or None
    except Exception:
        return None


_UNLOCKER = None


def unlocker_stats():
    try:
        return _UNLOCKER.stats() if _UNLOCKER else "unlocker: unused"
    except Exception:
        return "unlocker: n/a"


def _csrf_inputs(soup):
    """Hidden inputs that must round-trip (CSRF, form ids, honeypots)."""
    out = {}
    for el in soup.find_all("input", attrs={"type": re.compile("^hidden$", re.I)}):
        n = el.get("name")
        if n:
            out[n] = el.get("value") or ""
    return out


def _client(cfg):
    """
    HTTP client for Tier 1.

    Prefers curl_cffi, which impersonates Chrome's TLS/JA3 fingerprint. This
    matters enormously: a large slice of small-dealer sites sit behind
    Cloudflare's "Verifying your connection / Enable JavaScript" interstitial
    and answered plain httpx with a 429/403 challenge page FOREVER. curl_cffi
    returns the real 200 page at the same speed (~0.5s) with no browser.

    Falls back to httpx if curl_cffi is unavailable.
    """
    headers = {
        "User-Agent": cfg.get("user_agent", "Mozilla/5.0"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    }
    timeout = cfg.get("request_timeout", 20)

    try:
        from curl_cffi import requests as _creq
        return _creq.Session(
            impersonate=cfg.get("impersonate", "chrome"),
            headers=headers,
            timeout=timeout,
            verify=False,
            allow_redirects=True,
        )
    except Exception:
        return httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout, connect=min(6, timeout)),
            limits=httpx.Limits(max_connections=64,
                                max_keepalive_connections=32,
                                keepalive_expiry=30),
            headers=headers,
            verify=False,
        )


def detect_captcha(html):
    """
    Identify a captcha, distinguishing silent (v3) from interactive (v2).

    v3 scores the request invisibly and lets the POST through, so it should not
    block us. v2 / hCaptcha / Turnstile need a real human.

    ── WHY THIS WAS REWRITTEN (2026-11-04) ─────────────────────────────────
    The old version was a bare substring match: `if "hcaptcha" in html`.
    Measured against a real browser, 1,429 pages were tagged as captcha when
    NO WIDGET EVER LOADED -- "hcaptcha" appeared only inside a JS template
    string / a lazy-loaded module name. Shopify contact pages are the worst
    offenders: window.hcaptcha is undefined and there is no widget script.

    Consequence: those pages were filed `needs_manual` UNCONDITIONALLY
    (see the hard_captcha branch in attempt()), guaranteeing an unconfirmable
    verdict on ~1,400 sites that had no captcha problem at all.

    The fix requires actual EVIDENCE of a widget, not a word:
      * a widget element WITH a sitekey, or
      * a loader that will create one (the api.js script), AND
      * for hCaptcha specifically, an element/attr, not just the noun.

    Strongest-evidence first. Returns None when there is no real widget.
    """
    h = (html or "").lower()

    # ── ORDER MATTERS ───────────────────────────────────────────────────
    # graff.com proved it: an hCaptcha mention plus a data-sitekey was labelled
    # hcaptcha, while the browser showed a reCAPTCHA widget. reCAPTCHA is
    # checked FIRST when its api.js is actually loaded, because a page can
    # mention hcaptcha (analytics, abandoned config) without using it.
    grec_api = ("recaptcha/api.js" in h or "gstatic.com/recaptcha" in h
                or "google.com/recaptcha" in h)
    if grec_api:
        if (re.search(r'class="[^"]*g-recaptcha[^"]*"', h) and "sitekey" in h) \
           or re.search(r'g-recaptcha["\'][^>]{0,200}data-sitekey', h) \
           or re.search(r'data-sitekey', h):
            return "recaptcha_v2"
        return "recaptcha_v3"

    # ── hCaptcha: needs its OWN loader or a real widget element ─────────
    hcap_el = re.search(r'class="[^"]*h-captcha[^"]*"', h)
    hcap_attr = re.search(r'data-hcaptcha-sitekey', h)
    hcap_api = re.search(r'hcaptcha\.com/1/api\.js|js\.hcaptcha\.com', h)
    if hcap_el or hcap_attr or hcap_api:
        return "hcaptcha"

    # ── Turnstile ───────────────────────────────────────────────────────
    if "challenges.cloudflare.com" in h or "cf-turnstile" in h:
        return "turnstile"

    if "g-recaptcha" in h or "grecaptcha" in h:
        return "recaptcha_v3"
    return None


def _same_form_present(body, inputs):
    """
    True if the response still contains a form with the same field names we
    filled. A successfully-submitted form is usually replaced or reset, so its
    continued presence is a decent signal that the POST did not take.
    """
    if not body:
        return False
    names = [i["name"] for i in inputs if i.get("name")]
    if not names:
        return False
    hits = sum(1 for n in names if n in body)
    return hits >= max(2, len(names) // 2)


def attempt(url, cfg, client):
    """
    Try to find and submit a form at `url` over plain HTTP.

    Returns dict: ok, status, detail, http_code, form_action, fields_hit, tier
    status is one of: submitted | needs_manual | no_form | http_fail | failed
    """
    from core import db

    cache = {}   # per-site: the homepage was fetched twice (crawl + scan)

    def fetch(u):
        if u in cache:
            return cache[u]
        try:
            r = client.get(u)
            ct = r.headers.get("content-type", "")
            if "html" not in ct.lower():
                out = (str(r.url), None, r)
            else:
                out = (str(r.url), r.text, r)
        except Exception:
            out = (u, None, None)
        cache[u] = out
        return out

    tried_pages = 0
    dead_streak = 0
    best_attempt = None   # remember the most informative outcome
    forms_seen = 0
    last_err = ""

    # â”€â”€ Phase 1: scan candidates and keep the best-scoring form â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Submitting to the FIRST form found used to pick newsletter stubs and
    # login boxes. Scan first, then submit once to the strongest match.
    best_cand = None      # (score, cand, final_url, form, inputs, soup, html)
    best_attempt = None
    forms_seen = 0
    last_err = ""

    for cand in formdetect.candidate_urls(lambda u: fetch(u)[:2], url, cfg):
        tried_pages += 1
        if tried_pages > cfg.get("max_candidate_pages", 12):
            break
        try:
            final_url, html, resp = fetch(cand)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
        # Dead or hanging host: every further guess just waits out the full
        # timeout (measured 144s/site). Stop if the homepage, or two pages in
        # a row, got no response at all.
        if resp is None:
            dead_streak += 1
            if tried_pages == 1 or dead_streak >= 2:
                last_err = "site not responding"
                break
            continue
        dead_streak = 0
        if not html:
            continue
        final_url = str(final_url)

        # A block page has no form â€” note it but keep trying.
        blocked = detect_block(resp if resp is not None else 200, html)
        if blocked and len(html) < 12000:
            code = getattr(resp, "status_code", 0)
            best_attempt = dict(
                ok=False, status="blocked", tier="http", http_code=code,
                detail=f"{blocked} block page (no form served)",
                error=f"blocked by {blocked} retry via browser")
            # Bright Data fallback: only pays the slow path when actually blocked
            unlocked = _unlocked_fetch(cand)
            if unlocked:
                html = unlocked
                final_url = str(cand)
                blocked = None
            else:
                continue

        try:
            soup = _safe_parse(html)
            if soup is None:
                continue
            found = formdetect.find_best_form(soup, final_url, cfg)
        except Exception as e:
            last_err = f"scan: {type(e).__name__}: {e}"
            continue

        # No inline form? The contact form may live in a cross-origin iframe
        # (Wufoo, JotForm, Typeform, Google Forms...). Fetch the iframe itself
        # and treat it as a standalone form page.
        if not found:
            try:
                from core import embeds
                for src in (embeds.iframe_srcs(html) or [])[:3]:
                    kind = embeds.is_form_iframe(src)
                    if not kind:
                        continue
                    target = embeds.google_form_url(src) if kind == "google" else src
                    fh = fetch(target)[1]
                    if not fh:
                        continue
                    try:
                        fsoup = _safe_parse(fh)
                        if fsoup is None:
                            continue
                        ffound = formdetect.find_best_form(fsoup, target, cfg)
                    except Exception:
                        continue
                    if ffound:
                        # Submit against the IFRAME document
                        fform, finputs, fscore = ffound
                        fmapping, _ = fieldmap.map_fields(finputs, cfg)
                        if fmapping:
                            forms_seen += 1
                            if best_cand is None or fscore > best_cand[0] - 1:
                                best_cand = (fscore + 20, cand, target, fform,
                                             finputs, fsoup, fh, fmapping, None)
                    break
            except Exception as e:
                last_err = f"iframe: {type(e).__name__}: {e}"
            if best_cand is not None:
                continue

        if not found:
            continue

        form, inputs, score = found
        forms_seen += 1
        try:
            mapping, _un = fieldmap.map_fields(inputs, cfg)
        except Exception:
            mapping = {}
        if not mapping:
            continue

        # Rank candidates. Score matters most, but a page that actually served
        # 200 is worth more than an identical form scraped off a 404 page —
        # on some sites every /contact path 404s and only the homepage works.
        pagestatus = getattr(resp, "status_code", 200)
        ok_page = 200 <= pagestatus < 300
        rank = score + (5 if ok_page else 0)
        if best_cand is None or rank > best_cand[0]:
            best_cand = (rank, cand, final_url, form, inputs, soup, html,
                         mapping, resp)

        # ── Early exit ──────────────────────────────────────────────────────
        # Measured: a site cost 27 serial requests and 9.5s, and the form was
        # usually found at request 6 — the other 21 were wasted 404-guessing.
        # If we have a strong form on a live 200 page, no later candidate is
        # realistically going to beat it. Stop.
        if ok_page and rank >= cfg.get("scan_good_enough", 15):
            break

    # â”€â”€ Phase 2: submit once to the best form found â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if best_cand is not None and cfg.get("scan_only"):
        return dict(ok=True, status="form_found", tier="http",
                    form_page=best_cand[2], detail=f"score={best_cand[0]}")

    if best_cand is not None:
        try:
            score, cand, final_url, form, inputs, soup, html, mapping, resp = best_cand
            out = _submit_form(form, inputs, mapping, soup, final_url, html, cfg, client)
            if out.get("status") in ("submitted",):
                return out
            if best_attempt is None or out.get("status") != "blocked":
                best_attempt = out
        except Exception as e:
            last_err = f"submit: {type(e).__name__}: {e}"

    if best_attempt:
        return best_attempt

    if forms_seen:
        return dict(ok=False, status="needs_manual", tier="http",
                    error="form found but could not be completed")

    return dict(ok=False, status="no_form", tier="http",
                error=last_err or "no usable contact form found")



def _submit_form(form, inputs, mapping, soup, final_url, html, cfg, client):
    """
    Fill and submit one already-chosen form. Returns a result dict.

    Separate from attempt() so the caller can scan many candidates and then
    submit exactly once to the best-scoring form.
    """
    cap = detect_captcha(html)

    # Payload: ONLY this form's hidden controls, then our values. Pulling
    # hiddens from the whole page leaks another form's keys and hijacks it.
    payload = {}
    for el in form.find_all("input"):
        if (el.get("type") or "").lower() == "hidden":
            n = el.get("name")
            if n:
                payload[n] = el.get("value") or ""
    if not payload:
        payload = dict(_csrf_inputs(soup))

    for i, val in mapping.items():
        n = inputs[i]["name"]
        if n:
            payload[n] = val

    variants = formdetect.action_variants(form, final_url)
    method = (form.get("method") or "post").upper()
    action = variants[0]

    fields_hit = ",".join(
        "{}={}".format(
            inputs[i]['name'] or inputs[i]['id'],
            fieldmap.classify(inputs[i]['name'] + inputs[i]['id'] + inputs[i]['label']))
        for i in mapping
    )

    r = None
    err = ""
    best_resp = None
    for act in variants:
        try:
            if method == "GET":
                cand_r = client.get(act, params=payload)
            else:
                cand_r = client.post(
                    act, data=payload,
                    headers={"Referer": final_url,
                             "Content-Type": "application/x-www-form-urlencoded"},
                )
                # A form declared POST but 5xx-ing often actually wants GET.
                if cand_r.status_code >= 500:
                    g = client.get(act, params=payload)
                    if g.status_code < 500:
                        cand_r = g
        except Exception as e:
            err = str(e)[:200]
            continue

        # Remember the best response so far, but keep trying variants while we
        # only have a 4xx. Some sites 404 the action path but accept a POST to
        # the page URL itself.
        if best_resp is None or (best_resp.status_code >= 400
                                 and cand_r.status_code < best_resp.status_code):
            best_resp = cand_r
            action = act
        if cand_r.status_code < 400:
            break

    r = best_resp
    if r is None:
        return dict(ok=False, status="failed", error=err or "request error",
                    tier="http", form_action=action, fields_hit=fields_hit)

    body = r.text or ""
    code = r.status_code
    capt = f"captcha={cap}; " if cap else ""

    # Success detection, strongest first:
    #  1. landed on a thank-you / success URL
    #  2. success wording AND the form is gone
    #  3. success wording alone
    #  4. 2xx and the form is gone
    final_path = str(r.url).lower()
    redirect_win = bool(re.search(
        r"thank|success|sent|received|confirm|gracias|merci|danke", final_path))
    body_win = bool(SUCCESS_WORDS.search(body))
    fail = bool(FAIL_WORDS.search(body))
    form_still_there = _same_form_present(body, inputs)

    if redirect_win:
        return dict(ok=True, status="submitted", tier="http", http_code=code,
                    detail=capt + "redirected to confirmation page",
                    form_action=action, fields_hit=fields_hit)

    if body_win and not fail:
        detail = ("success wording + form cleared" if not form_still_there
                  else "success wording on response")
        return dict(ok=True, status="submitted", tier="http", http_code=code,
                    detail=capt + detail, form_action=action, fields_hit=fields_hit)

    if cap and not body_win:
        # reCAPTCHA **v3** runs silently in the background and scores the
        # request — the POST still goes through. Treating every recaptcha page
        # as 'needs_manual' was over-cautious and buried real successes.
        # Only v2 / hCaptcha / Turnstile actually need a human.
        hard_captcha = cap in ("recaptcha_v2", "hcaptcha", "turnstile")
        if not hard_captcha:
            # v3-style: accept if the response looks clean and the form went
            if code < 400 and not fail:
                return dict(ok=True, status="submitted", tier="http", http_code=code,
                            detail=capt + "silent-captcha, 2xx accepted",
                            form_action=action, fields_hit=fields_hit)
        # A REAL interactive captcha with no token: this is the one case where
        # the HTTP tier genuinely cannot know. Report it distinctly so the
        # browser tier is clearly the answer, instead of lumping it with every
        # other ambiguous 2xx.
        return dict(ok=False, status="needs_manual", tier="http", http_code=code,
                    detail=capt + "interactive captcha, no token (needs browser/solver)",
                    form_action=action, fields_hit=fields_hit)

    if code >= 400:
        return dict(ok=False, status="failed", tier="http", http_code=code,
                    error=f"HTTP {code}", form_action=action, fields_hit=fields_hit)

    if not form_still_there:
        return dict(ok=True, status="submitted", tier="http", http_code=code,
                    detail="2xx and form no longer present",
                    form_action=action, fields_hit=fields_hit)

    # ── Ambiguous: 2xx but the form is still rendered. ─────────────────────
    # Most contact forms do not redirect or print a thank-you, so this is the
    # biggest bucket. Get a second signal: re-GET the form page and compare
    # with what we originally saw. If the server remembered our submission the
    # page usually differs (field repopulated, notice added, timestamp moved).
    second = _second_signal(client, final_url, inputs, html)
    if second == "changed":
        return dict(ok=True, status="submitted", tier="http", http_code=code,
                    detail=capt + "2xx + form page changed on re-fetch",
                    form_action=action, fields_hit=fields_hit)
    if second == "gone":
        return dict(ok=True, status="submitted", tier="http", http_code=code,
                    detail=capt + "2xx + form gone on re-fetch",
                    form_action=action, fields_hit=fields_hit)

    return dict(ok=False, status="needs_manual", tier="http", http_code=code,
                detail=capt + "2xx, form unchanged on re-fetch (unconfirmed)",
                form_action=action, fields_hit=fields_hit)


def _second_signal(client, page_url, inputs, original_html):
    """
    Re-fetch the form page and look for evidence our submission registered.

    Returns 'gone' | 'changed' | None. Conservative on purpose — only reports a
    change when something material differs, so we do not invent successes.
    """
    try:
        r = client.get(page_url)
    except Exception:
        return None
    if r.status_code != 200 or not r.text:
        return None

    body = r.text

    # form vanished entirely -> strong signal
    if not _same_form_present(body, inputs):
        return "gone"

    # success wording that was NOT in the original page
    had_before = bool(SUCCESS_WORDS.search(original_html or ""))
    has_now = bool(SUCCESS_WORDS.search(body))
    if has_now and not had_before:
        return "changed"

    # a visible notice/alert block appearing
    if re.search(r"(alert|notice|success|confirmation|thank)[^>]{0,60}"
                 r"(box|message|div|panel)", body, re.I):
        return "changed"

    # our email echoed into a repopulated field -> 'sent' state
    if re.search(r'value="[^"]*@[^"]*"', body):
        return "changed"

    return None


def run_batch(sites, cfg, on_result=None):
    """Process a list of site dicts over HTTP."""
    results = []
    with _client(cfg) as client:
        for s in sites:
            try:
                res = attempt(s["url"], cfg, client)
            except Exception as e:
                res = dict(ok=False, status="failed", tier="http", error=str(e)[:300])
            res["id"] = s["id"]
            res["url"] = s["url"]
            results.append(res)
            if on_result:
                on_result(res)
    return results
