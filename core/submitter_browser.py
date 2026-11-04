"""
core/submitter_browser.py — Tier 2: real browser for JS forms, iframes, odd markup.

Only runs for sites Tier 1 could not handle. Uses one persistent browser
context reused across sites, with a fresh page each time.
"""

import random
import re

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from core import fieldmap, formdetect

SUCCESS_WORDS = re.compile(
    r"(thank|thanks|success|sent|received|get back to you|submitted|"
    r"appreciate|be in touch|got your message|cheers)",
    re.I,
)


def _page_soup(page):
    try:
        from core.soup import safe_parse
        return safe_parse(page.content())
    except Exception:
        return None


def _extract_inputs(page, cfg, prefer_contact=True):
    """
    Pull fillable fields from the BEST form on the page.

    THE BUG THE USER CAUGHT: jewelryboxestatebuyers.com/contact/ has TWO Gravity
    Forms -- gform_1 (name+email only, a subscribe/stub) and gform_2 (name,
    email, PHONE). The old code used a flat
    `querySelectorAll('input,textarea,select')` across the WHOLE page, so it
    grabbed fields from both, mapped the first name/email it saw and submitted
    whichever button came first -- the SUBSCRIBE form. The pitch textarea was
    dropped because gform_1 has no message box.

    Now we score each <form> and return ONLY that form's fields, ranked by
    contact-form evidence:
        + textarea            (message box -- the thing that carries the pitch)
        + email field
        + phone field
        + more fillable fields
        + submit control inside the form
        - newsletter/subscribe wording (class, id, action, placeholder)
    """
    js = """
    (prefer) => {
      const SCORE_NEWS = /subscri|newsletter|signup|sign-up|mailing|mc4wp|klaviyo|kl_|mailchimp|footer|social|search/i;
      const forms = [...document.querySelectorAll('form')];
      const vis = el => {
        const st = window.getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden') return false;
        const r = el.getBoundingClientRect();
        return r.width >= 5 && r.height >= 5;
      };
      const fieldsOf = (root) => {
        const out = [];
        root.querySelectorAll('input, textarea, select').forEach(el => {
          const t = (el.type || '').toLowerCase();
          if (['hidden','submit','button','image','reset','file','checkbox','radio','search'].includes(t)) return;
          if (!vis(el)) return;
          let label = '';
          if (el.id) { const l = document.querySelector(`label[for="${el.id}"]`); if (l) label = l.innerText || ''; }
          if (!label && el.closest('label')) label = el.closest('label').innerText || '';
          out.push({name: el.name||'', id: el.id||'', placeholder: el.placeholder||'',
                    aria: el.getAttribute('aria-label')||'', label: label, type: t,
                    tag: el.tagName.toLowerCase(),
                    required: el.required || el.getAttribute('aria-required')==='true',
                    options: []});
        });
        return out;
      };
      const rank = (f, flds) => {
        const sig = ((f.id||'')+' '+(f.className||'')+' '+(f.getAttribute('action')||'')).toLowerCase();
        let s = 0;
        if (flds.some(x => x.tag === 'textarea')) s += 6;
        if (flds.some(x => x.type === 'email')) s += 3;
        if (flds.some(x => x.type === 'tel')) s += 2;
        s += Math.min(flds.length, 6);
        if (f.querySelector('button[type=submit],input[type=submit],button')) s += 2;
        if (SCORE_NEWS.test(sig)) s -= 5;
        const blob = flds.map(x => x.name+' '+x.placeholder+' '+x.label).join(' ').toLowerCase();
        if (/subscri|newsletter|sign ?up/.test(blob)) s -= 4;
        return s;
      };
      let best = null, bestScore = -999, bestFields = [];
      forms.forEach(f => {
        if (!vis(f)) return;
        const flds = fieldsOf(f);
        if (flds.length < 2) return;
        const sc = rank(f, flds);
        if (sc > bestScore) { bestScore = sc; best = f; bestFields = flds; }
      });
      if (best) return {fields: bestFields, formId: best.id||'', score: bestScore,
                        formCount: forms.length};
      const all = fieldsOf(document);
      return {fields: all, formId: '', score: -1, formCount: forms.length};
    }
    """
    try:
        d = page.evaluate(js, prefer_contact)
    except Exception:
        return []
    d = d or {}
    # stash the winning form id so the submit click can be scoped to it
    try:
        cfg["_chosen_form_id"] = d.get("formId") or ""
        cfg["_form_count"] = d.get("formCount")
        cfg["_form_score"] = d.get("score")
    except Exception:
        pass
    return [dict(x, el=None, value="") for x in d.get("fields", [])]


def _detect_widget(page):
    """
    Detect a captcha widget that REALLY renders, and read its sitekey.

    CRITICAL FIX. The HTTP tier's detect_captcha() is a substring match on the
    HTML, so it flagged 1,429 pages as captcha when the widget never loaded.
    Here we require a real widget ELEMENT (or a live iframe) plus a sitekey.

    Returns (kind, sitekey, response_field) or ("", "", "").
    """
    js = """() => {
      const out = {kind:'', key:'', field:''};
      const pick = (sel) => document.querySelector(sel);
      let el = pick('.h-captcha,[data-hcaptcha-sitekey],div[class*=h-captcha]');
      if (el) { out.kind='hcaptcha';
                out.key = el.getAttribute('data-sitekey')||el.getAttribute('data-hcaptcha-sitekey')||'';
                out.field='h-captcha-response'; }
      if (!out.kind) {
        el = pick('.g-recaptcha,[data-sitekey]');
        if (el) { out.kind='recaptcha_v2';
                  out.key = el.getAttribute('data-sitekey')||'';
                  out.field='g-recaptcha-response'; }
      }
      if (!out.kind) {
        el = pick('.cf-turnstile,[data-turnstile-sitekey]');
        if (el) { out.kind='turnstile';
                  out.key = el.getAttribute('data-sitekey')||'';
                  out.field='cf-turnstile-response'; }
      }
      // fall back to reading the sitekey out of a live iframe src
      if (!out.key) {
        document.querySelectorAll('iframe').forEach(f => {
          const s = f.src||'';
          const m = s.match(/[?&](?:k|sitekey)=([^&]+)/);
          if (m && !out.key) { out.key = decodeURIComponent(m[1]);
            if (s.includes('hcaptcha')) out.kind='hcaptcha';
            else if (s.includes('recaptcha')) { out.kind='recaptcha_v2'; out.field='g-recaptcha-response'; }
            else if (s.includes('cloudflare')) out.kind='turnstile';
          }
        });
      }
      if (!out.field) {
        if (out.kind==='hcaptcha') out.field='h-captcha-response';
        else if (out.kind==='turnstile') out.field='cf-turnstile-response';
        else out.field='g-recaptcha-response';
      }
      // ── KIND MUST FOLLOW THE KEY FORMAT, NOT A SELECTOR GUESS ─────────
      // graff.com proved it: a widget was matched as hcaptcha, but the key
      // "6LdNfLIZ..." is a GOOGLE key. hCaptcha keys are UUIDs
      // (xxxxxxxx-xxxx-...). Sending a Google key as type=hcaptcha is a 422.
      if (out.key) {
        const isUUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(out.key);
        const isGoogle = /^6[0-9A-Za-z_\\-]{38,}$/.test(out.key);
        if (isGoogle) { out.kind='recaptcha_v2'; out.field='g-recaptcha-response'; }
        else if (isUUID) { out.kind='hcaptcha'; out.field='h-captcha-response'; }
        else if (/^0x[0-9A-Fa-f]+$/.test(out.key)) { out.kind='turnstile'; out.field='cf-turnstile-response'; }
      }
      // has the user token already landed?
      out.token = '';
      try { out.token = (document.querySelector('[name="'+out.field+'"]')||{}).value||''; } catch(e){}
      out.hasWidget = !!(out.key || document.querySelector('iframe[src*="hcaptcha"],iframe[src*="recaptcha"],iframe[src*="cloudflare"]'));
      return out;
    }"""
    try:
        return page.evaluate(js)
    except Exception:
        return {"kind": "", "key": "", "field": "", "token": "", "hasWidget": False}


def _field_errors(page):
    """
    Detect 'Please match the requested format' style validation errors.

    THE BUG THE USER CAUGHT: a UK phone (+44...) was typed into US/CA forms,
    the browser showed "Please match the requested format", and the bot logged
    it as submitted anyway. Two screenshots proved it (vintagevibesva.com,
    ocrugliquiators.com).

    Returns a list of (field_label, message) for visible validation problems.
    """
    js = """() => {
      const bad = [];
      const rx = /match the requested format|invalid|incorrect|not valid|please enter a valid|does not match|must be a|enter a valid/i;
      document.querySelectorAll('input,textarea,select').forEach(el => {
        if (!el.validationMessage && !(el.getAttribute('aria-invalid')==='true')) {
          // also catch pages that only paint a sibling error
          const sib = el.parentElement ? el.parentElement.innerText : '';
          if (!rx.test(sib)) return;
        }
        const msg = el.validationMessage || '';
        if (msg || el.getAttribute('aria-invalid')==='true') {
          bad.push({name: el.name||el.id||el.type, type: el.type||'', msg: msg});
        } else {
          bad.push({name: el.name||el.id||el.type, type: el.type||'', msg: 'format'});
        }
      });
      return bad;
    }"""
    try:
        out = page.evaluate(js) or []
    except Exception:
        return []
    # dedupe by name
    seen, res = set(), []
    for d in out:
        k = (d.get("name"), d.get("msg"))
        if k in seen:
            continue
        seen.add(k)
        res.append(d)
    return res


def _retry_phone(page, cfg, log):
    """
    A phone field was rejected. Swap to phone_alt (US format) and re-submit.
    Returns True if a retry was made.
    """
    alt = cfg.get("phone_alt")
    if not alt:
        return False
    js = """(val) => {
      let n = 0;
      document.querySelectorAll('input[type="tel"],input[name*="phone" i],input[id*="phone" i],input[name*="tel" i]').forEach(el => {
        el.value = ''; el.focus();
        el.value = val; n++;
        el.dispatchEvent(new Event('input', {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true}));
        el.dispatchEvent(new Event('blur', {bubbles:true}));
      });
      return n;
    }"""
    try:
        n = page.evaluate(js, alt)
    except Exception:
        return False
    if not n:
        return False
    log("  PHONE REJECTED -> swapped to %s in %d field(s), re-submitting" % (alt, n))
    for sel in ("button[type='submit']", "input[type='submit']",
                "button:has-text('Send')", "form button"):
        try:
            el = page.locator(sel).last
            if el.count() and el.is_visible():
                el.click(timeout=6000)
                return True
        except Exception:
            continue
    return False


def _solve_via_api(page, kind, sitekey, cfg, log):
    """
    Solve with NoneCap and inject the token into the form field.
    Returns True if a token was injected.
    """
    try:
        from core.solver import get_solver
    except Exception as e:
        log("solver import failed %s" % e)
        return False
    s = get_solver()
    if not s.enabled:
        log("solver DISABLED (no key)")
        return False
    log("calling NoneCap: %s sitekey=%s" % (kind, (sitekey or "")[:26]))
    tok, note = s.solve(kind, sitekey, page.url,
                        user_agent=(cfg.get("user_agent") or None))
    if not tok:
        log("  solve FAILED (%s) %s" % (note, s.last_error[:60]))
        return False
    log("  solved (%s) token len=%d" % (note, len(tok)))
    field = {"hcaptcha": "h-captcha-response",
             "turnstile": "cf-turnstile-response"}.get(kind, "g-recaptcha-response")
    # inject into every matching element, incl. the textarea hCaptcha uses
    js = """([field, tok]) => {
      let n = 0;
      document.querySelectorAll('[name="'+field+'"], #'+field).forEach(el => {
        el.value = tok; n++;
        el.dispatchEvent(new Event('input', {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true}));
      });
      // also set the hidden textarea hCaptcha/recaptcha expect
      let t = document.querySelector('textarea[name="'+field+'"]');
      if (!t) { t = document.createElement('textarea'); t.name = field; t.style.display='none';
                document.body.appendChild(t); n++; }
      t.value = tok;
      return n;
    }"""
    try:
        n = page.evaluate(js, [field, tok])
        log("  injected token into %d field(s)" % n)
        return True
    except Exception as e:
        log("  inject failed %s" % type(e).__name__)
        return False


def _click_captcha_checkbox(page, fr):
    """
    Click the 'I am not a robot' checkbox if it is present and unsolved.

    The browser tier previously just clicked Submit and hoped the widget had
    resolved on its own. That works for reCAPTCHA v3 (invisible) but is a coin
    flip for hCaptcha / reCAPTCHA v2 / Turnstile, which need a real click.

    Returns a short label if a widget was found, else "".
    """
    # reCAPTCHA v2, hCaptcha and Turnstile all ship the checkbox inside an
    # iframe. Clicking the iframe's box position is more reliable than trying
    # to pierce the cross-origin boundary.
    CANDIDATES = [
        ("recaptcha_v2", "iframe[src*='recaptcha/api2/anchor']"),
        ("recaptcha_v2", "iframe[title*='reCAPTCHA']"),
        ("hcaptcha", "iframe[src*='hcaptcha.com']"),
        ("hcaptcha", "iframe[title*='hCaptcha']"),
        ("turnstile", "iframe[src*='challenges.cloudflare.com']"),
    ]
    found = ""
    for label, sel in CANDIDATES:
        try:
            frames = list(page.frames) if fr is page else [fr]
            el = fr.locator(sel).first
            if el.count() == 0:
                continue
            found = label
            box = el.bounding_box()
            if not box or box.get("width", 0) < 10:
                continue
            # Click into the widget. Small left offset lands on the checkbox,
            # not the logo/text to the right.
            fr.mouse.click(box["x"] + 18, box["y"] + box["height"] / 2)
            page.wait_for_timeout(1200)
        except Exception:
            continue
    return found


def _fill_and_submit(page, inputs, mapping, cfg):
    """Fill by nth-of-type on the live DOM, then click the submit control."""
    filled = 0
    for idx, val in mapping.items():
        if idx >= len(inputs):
            continue
        inp = inputs[idx]
        sel = None
        if inp.get("id"):
            sel = f"#{inp['id']}"
        elif inp.get("name"):
            sel = f"[name='{inp['name']}']"
        else:
            continue
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            if inp.get("tag") == "select":
                try:
                    loc.select_option(label=val)
                except Exception:
                    loc.select_option(index=1)
            else:
                loc.click(timeout=4000)
                page.keyboard.insert_text(val)
            filled += 1
        except Exception:
            continue

    if filled == 0:
        return 0, ""

    # Tick any captcha checkbox BEFORE submitting. Previously we clicked Submit
    # blind, which silently under-delivers on hCaptcha / reCAPTCHA v2 /
    # Turnstile (they require an interaction; only v3 is invisible).
    cap = _click_captcha_checkbox(page, page)

    # Find a submit control INSIDE the chosen form first.
    # Old code used page.locator(sel).last -- the last matching button anywhere
    # on the page, which on a two-form page (jewelryboxestatebuyers.com) clicked
    # the wrong form's button.
    clicked = False
    SELS = ("button[type='submit']",
            "input[type='submit']",
            "button:has-text('Send')",
            "button:has-text('Submit')",
            "button:has-text('Get in touch')",
            "button:has-text('Contact')",
            "button:has-text('Enquire')",
            "button:has-text('Enquiry')",
            "form button")

    scopes = []
    fid = cfg.get("_chosen_form_id") or ""
    if fid:
        scopes.append("#" + fid)
    scopes.append("")           # page-wide fallback

    for scope in scopes:
        for sel in SELS:
            full = (scope + " " + sel).strip()
            try:
                loc = page.locator(full)
                n = loc.count()
                if not n:
                    continue
                btn = loc.first if scope else loc.last
                if btn.is_visible():
                    btn.click(timeout=6000)
                    clicked = True
                    break
            except Exception:
                continue
        if clicked:
            break

    if not clicked:
        try:
            page.keyboard.press("Enter")
            clicked = True
        except Exception:
            pass

    # Give the captcha/JS a moment to settle after submit
    if clicked and cap:
        page.wait_for_timeout(1500)

    return (filled if clicked else 0), cap


def attempt(url, cfg, ctx):
    """
    Browser attempt for one URL. Returns the same result dict shape as Tier 1.

    WATCHDOG: rumanoffs.com wedged the whole run for 23 minutes because the
    candidate list can hold up to 14 URLs and EVERY navigation silently ate the
    default timeout when the page kept a connection open. With no outer bound,
    one bad site blocked the loop forever (log frozen, CPU ~0, so it was
    waiting not working).

    Now the whole attempt is hard-bounded by `browser_site_budget` (default
    90s). Past that we return a `timeout` verdict instead of hanging, and the
    page is force-closed so the shared context stays sane for the next site.
    """
    import time as _t
    _T = _t.time()
    budget = cfg.get("browser_site_budget", 90)
    _timed_out = {"v": False}

    def _m(x):
        if cfg.get("_trace"):
            print("      [attempt %.1fs] %s" % (_t.time() - _T, x), flush=True)

    page = ctx.new_page()
    try:
        page.set_default_timeout(cfg.get("browser_timeout", 25000))
        # Hard ceiling on any single navigation, so a dribbling server cannot
        # burn the whole budget on one candidate.
        try:
            page.set_default_navigation_timeout(cfg.get("browser_nav_timeout", 30000))
        except Exception:
            pass

        # ── Phase 1: resolve the candidate list up front ────────────────────
        # candidate_urls() calls fetch() lazily while iterating, which used to
        # navigate the very page we were about to fill. Resolve first, fill
        # second. Uses the same page but only during discovery.
        cand_list = []
        try:
            for c in formdetect.candidate_urls(lambda u: _quick(u, page), url, cfg):
                cand_list.append(c)
                if len(cand_list) >= 14 or _t.time() - _T > budget:
                    break
        except Exception as e:
            _m("crawl error %s" % str(e)[:60])
        if url not in cand_list:
            cand_list.insert(0, url)
        _m("candidate list: %d" % len(cand_list))

        # Spin per site so no two submissions are identical.
        from core import spin as _spin
        try:
            cfg = _spin.spin_fields(cfg)
        except Exception:
            pass

        # ── Phase 2: try each candidate for real ────────────────────────────
        for hit, cand in enumerate(cand_list, 1):
            # Budget check: stop burning wall-clock on one site. Return the
            # best verdict we have rather than hanging the batch.
            if _t.time() - _T > budget:
                _m("  BUDGET EXCEEDED (%.0fs) after %d cands" % (_t.time() - _T, hit - 1))
                _timed_out["v"] = True
                break

            _m("cand %d %s" % (hit, cand))
            try:
                page.goto(cand, timeout=cfg.get("browser_nav_timeout", 30000),
                          wait_until="domcontentloaded")
                page.wait_for_timeout(600)
            except Exception as e:
                _m("  nav fail %s" % str(e)[:50])
                continue
            _m("  loaded")

            # Main frame first, then any iframes (Typeform/HubSpot embeds)
            frames = [page]
            try:
                frames += [f for f in page.frames if f != page.main_frame]
            except Exception:
                pass

            for fr in frames:
                try:
                    inputs = _extract_inputs(fr, cfg)
                except Exception:
                    continue
                if len(inputs) < 2:
                    continue
                _m("  frame has %d inputs" % len(inputs))

                mapping, unmapped = fieldmap.map_fields(inputs, cfg)
                if not mapping:
                    continue
                _m("  mapping: %s" % list(mapping.keys()))

                n, cap_seen = _fill_and_submit(fr, inputs, mapping, cfg)
                _m("  filled+submit -> %d  captcha=%s" % (n, cap_seen or "-"))
                if n:
                    return _verdict(page, cand, inputs, mapping, cfg, _m,
                                    cap_seen)

        if _timed_out["v"]:
            return dict(ok=False, status="timeout", tier="browser",
                        detail="site budget %.0fs exceeded" % budget,
                        error="timeout")
        return dict(ok=False, status="no_form", tier="browser",
                    error="no fillable form found in browser")
    except Exception as e:
        return dict(ok=False, status="failed", tier="browser", error=str(e)[:300])
    finally:
        try:
            page.close()
        except Exception:
            pass


def _verdict(page, cand, inputs, mapping, cfg, _m, cap_seen):
    """
    Decide submitted vs failed with REAL evidence, after a submit click.

    THE BUG: previously we waited 2.5s, grepped the page for
    thank|success|sent|... and called it submitted. If a captcha puzzle
    appeared and nobody solved it, the page still contained those words in its
    static copy, so we reported a success that never happened.

    Evidence hierarchy, strongest first:
      1. success URL (thank-you / thanks / success path)
      2. success wording AND the form is gone
      3. an explicit ERROR / captcha-not-solved message  -> fail
      4. success wording alone (weaker, but only if no error present)

    Any captcha present is now SOLVED before we look.
    """
    # 1. give the captcha time to appear + solve it
    wid = _detect_widget(page)
    if wid.get("hasWidget") or wid.get("key"):
        _m("  widget: kind=%s key=%s token=%s"
           % (wid.get("kind"), (wid.get("key") or "")[:22], bool(wid.get("token"))))
        if not wid.get("token") and wid.get("key"):
            _solve_via_api(page, wid.get("kind") or "hcaptcha",
                           wid.get("key"), cfg, _m)
            page.wait_for_timeout(1500)
            # re-submit so the site validates with the fresh token
            try:
                for sel in ("button[type='submit']", "input[type='submit']",
                            "form button"):
                    el = page.locator(sel).last
                    if el.count() and el.is_visible():
                        el.click(timeout=5000)
                        _m("  re-submitted with token")
                        break
            except Exception:
                pass

    # 2. wait for a real post-submit state
    page.wait_for_timeout(int(cfg.get("post_submit_wait", 5000)))
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass

    # 2b. FIELD VALIDATION CHECK. A rejected phone/format means the submit did
    #     NOT go through, even though the page looks unchanged. The user caught
    #     this twice on screen. Retry with the alternate phone once, then judge.
    ferrs = _field_errors(page)
    if ferrs:
        _m("  FIELD ERRORS: %s" % [(f.get("name"), f.get("type"), f.get("msg")) for f in ferrs][:4])
        phone_bad = any("tel" in (f.get("type") or "") or "phone" in (f.get("name") or "").lower()
                        or "tel" in (f.get("name") or "").lower() for f in ferrs)
        if phone_bad and _retry_phone(page, cfg, _m):
            page.wait_for_timeout(int(cfg.get("post_submit_wait", 5000)))
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            ferrs = _field_errors(page)
            _m("  after phone retry, field errors: %d" % len(ferrs))
        still_bad = bool(ferrs)

    body = ""
    try:
        body = page.content()
    except Exception:
        pass
    final_url = (page.url or "").lower()

    link = ",".join(
        str(fieldmap.classify(inputs[i].get("name", "") + inputs[i].get("id", "") +
                              inputs[i].get("label", "")))
        for i in mapping if i < len(inputs))

    cap = ("captcha=%s; " % wid.get("kind")) if wid.get("kind") else ""

    # strong: landed on a success URL
    if re.search(r"thank|success|sent|received|confirm|merci|gracias|danke|bedankt", final_url):
        return dict(ok=True, status="submitted", tier="browser",
                    detail=cap + "redirected to success URL",
                    form_action=str(cand), fields_hit=link)

    # explicit failure signals
    err = bool(re.search(
        r"(please (complete|tick|verify|check|enter|match)|captcha (is )?(required|invalid|incorrect)|"
        r"not a robot|complete the captcha|verification failed|please try again|"
        r"match the requested format|not valid|enter a valid|"
        r"this field is required|must be filled|is required)", body, re.I))

    form_gone = not re.search(r"<form", body, re.I)
    success_text = bool(SUCCESS_WORDS.search(body))

    # A still-invalid FIELD beats everything except a genuine success redirect.
    if still_bad and not form_gone:
        return dict(ok=False, status="needs_manual", tier="browser",
                    detail=cap + "FIELD INVALID after retry (form rejected), form present",
                    form_action=str(cand), fields_hit=link)

    if err and not form_gone:
        return dict(ok=False, status="needs_manual", tier="browser",
                    detail=cap + "post-submit error/captcha notice, form still present",
                    form_action=str(cand), fields_hit=link)

    if success_text and form_gone:
        return dict(ok=True, status="submitted", tier="browser",
                    detail=cap + "success wording + form gone",
                    form_action=str(cand), fields_hit=link)

    if success_text:
        return dict(ok=True, status="submitted", tier="browser",
                    detail=cap + "success wording found",
                    form_action=str(cand), fields_hit=link)

    return dict(ok=False, status="needs_manual", tier="browser",
                detail=cap + "submitted, no success/error signal",
                form_action=str(cand), fields_hit=link)


def _quick(u, page):
    """Light navigation used only for candidate discovery."""
    try:
        page.goto(u, timeout=15000, wait_until="domcontentloaded")
        return u, page.content()
    except Exception:
        return u, None


def _shot_path(url):
    import hashlib
    from pathlib import Path
    base = Path(__file__).resolve().parent.parent / "data" / "shots"
    base.mkdir(parents=True, exist_ok=True)
    h = hashlib.md5(url.encode()).hexdigest()[:12]
    return base / f"{h}.png"


def run_batch(sites, cfg, on_result=None):
    """
    Process sites in a single browser session.

    NOTE: uses the explicit start()/stop() form rather than
    `with sync_playwright() as p`. The context-manager form calls stop() on
    exit, which deadlocks when a browser/context is still winding down.
    """
    results = []
    import time as _t
    _T0 = _t.time()

    def _m(x):
        print("[batch %.1fs] %s" % (_t.time() - _T0, x), flush=True)

    _m("calling sync_playwright().start()")
    pw = sync_playwright().start()
    _m("playwright started")
    browser = None
    try:
        _m("launching browser")
        browser = pw.chromium.launch(
            channel="msedge",
            headless=not cfg.get("show_browser", False),
            slow_mo=cfg.get("slow_mo", 0),
            args=["--disable-blink-features=AutomationControlled",
                  "--test-type", "--no-sandbox", "--disable-dev-shm-usage"],
        )
        _m("browser launched (headless=%s)" % (not cfg.get("show_browser", False)))
        ctx = browser.new_context(
            user_agent=cfg.get("user_agent"),
            viewport={"width": 1366, "height": 900},
            ignore_https_errors=True,
        )
        try:
            for s in sites:
                _m("attempt: %s" % s["url"])
                try:
                    res = attempt(s["url"], cfg, ctx)
                except Exception as e:
                    res = dict(ok=False, status="failed", tier="browser",
                               error=str(e)[:200])
                _m("  -> %s" % res.get("status"))
                res["id"] = s["id"]
                res["url"] = s["url"]
                results.append(res)
                if on_result:
                    on_result(res)
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
    return results
