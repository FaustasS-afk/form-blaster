"""
core/contact_engine.py — reliable contact-form sender (real browser, one path).

Per site:
  1. DISCOVER  open the homepage like a visitor, follow the site's own
               Contact / Get in touch / Enquiries links (guessed paths only if
               the site links none), homepage form last.
  2. QUALIFY   a form counts only if it has a MESSAGE box and an email or phone
               field, and is not a newsletter / login / search / checkout /
               upload form. No qualifying form -> stop (never "fill something").
  3. FILL      our values via fieldmap; required selects / consent boxes /
               radios handled; values set with real input events.
  4. SUBMIT    click the form's own button, watch the network and the page.
  5. VERIFY    sent  = success URL, NEW thank-you text, or form replaced after
                       a 2xx POST.  A validation error is fixed and retried once.
Every site ends with one outcome and a reason; no-form sites get the email /
phone found on the site so they can be contacted by hand.

Visible captchas are NOT solved or clicked: those leads are reported as
`captcha` with the form URL for manual contact.
"""

import re
import time
from urllib.parse import urljoin, urlparse

from core import fieldmap

CONTACT_LINK = re.compile(
    r"(contact|get[\s-]?in[\s-]?touch|enquir|inquir|reach[\s-]?us|reach[\s-]?out|"
    r"talk[\s-]?to[\s-]?us|say[\s-]?hello|write[\s-]?to[\s-]?us|message[\s-]?us|email[\s-]?us)", re.I)
WEAK_LINK = re.compile(r"(about|location|visit|appointment|customer[\s-]?service|help|support|information|directions|find[\s-]?us|hours)", re.I)
SKIP_LINK = re.compile(r"(\.(pdf|jpe?g|png|gif|zip)$|mailto:|tel:|javascript:|facebook|instagram|"
                       r"twitter|linkedin|youtube|tiktok|pinterest|/cart|/checkout|/account|/login)", re.I)
GUESS_PATHS = ["/contact", "/contact-us", "/pages/contact", "/contact-1", "/contactus",
               "/pages/contact-us", "/contact-us-1", "/get-in-touch"]

SUCCESS_TEXT = re.compile(
    r"(thank(s| you)|message (has been |was )?(sent|received)|we('| wi)ll (be in touch|get back|"
    r"contact you|respond)|successfully (sent|submitted)|submission (received|successful)|"
    r"form (has been )?submitted|got your message|received your (message|enquiry|inquiry))", re.I)
ERROR_TEXT = re.compile(
    r"(this field is required|is required|please (enter|fill|complete|provide|correct|check)|"
    r"(enter|provide) a valid|is (not )?(in)?valid|invalid (email|phone|format)|"
    r"there (was|were) (an |some )?(error|problem)|failed to send|could not be sent|"
    r"try again)", re.I)
SUCCESS_URL = re.compile(r"(thank|success|sent|confirm|received)", re.I)
ANALYTICS = re.compile(r"(google-analytics|googletagmanager|doubleclick|facebook\.com/tr|"
                       r"hotjar|clarity\.ms|segment|sentry|newrelic|stats\.wp|pixel|collect\?)", re.I)

# JS: find every candidate form container in a frame, score it, tag the
# winner's fields with data-fb-idx so we fill exactly those elements.
SCAN_JS = r"""
() => {
  const BAD = /(subscri|newsletter|mailing[\s-]?list|sign[\s-]?up for|join our (list|mailing)|mc4wp|klaviyo|mailchimp|log[\s-]?in|sign[\s-]?in|password|search|checkout|add to cart|coupon|track (your )?order)/i;
  const vis = el => { if (!el) return false; const s = getComputedStyle(el);
    if (s.display==='none'||s.visibility==='hidden'||+s.opacity===0) return false;
    const r = el.getBoundingClientRect(); return r.width>4 && r.height>4; };
  const labelOf = el => { let t='';
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) t = l.innerText; }
    if (!t && el.closest('label')) t = el.closest('label').innerText;
    if (!t) { const p = el.closest('div,p,li,fieldset'); if (p) { const l = p.querySelector('label,legend'); if (l) t = l.innerText; } }
    return (t||'').trim().slice(0,120); };
  const fieldsIn = root => [...root.querySelectorAll('input,textarea,select')].filter(el => {
      const t=(el.type||'').toLowerCase();
      if (['hidden','submit','button','image','reset'].includes(t)) return false;
      if (t==='checkbox'||t==='radio') return true;
      return vis(el); });
  // candidate containers: real <form>s, plus (form-less builders like Wix)
  // the smallest ancestor of a visible textarea that also holds a button.
  const conts = new Set([...document.querySelectorAll('form')].filter(vis));
  document.querySelectorAll('textarea').forEach(ta => { if (!vis(ta) || ta.closest('form')) return;
    let n = ta.parentElement;
    for (let i=0; n && i<10; i++, n=n.parentElement) {
      if (n.querySelector('button,[role=button],input[type=submit]') && n.querySelectorAll('input').length) { conts.add(n); break; } } });
  let best=null, bestScore=-1e9, reasons=[];
  for (const c of conts) {
    const flds = fieldsIn(c);
    const typed = flds.filter(e => !['checkbox','radio'].includes((e.type||'').toLowerCase()));
    const blob = (c.id+' '+c.className+' '+(c.getAttribute('action')||'')+' '+
                  typed.map(e => e.name+' '+e.id+' '+(e.placeholder||'')+' '+labelOf(e)).join(' ')).toLowerCase();
    const head = (c.innerText||'').slice(0,400);
    const hasMsg = typed.some(e => e.tagName==='TEXTAREA') || /message|enquir|inquir|comment|question|how can we help/.test(blob);
    const hasEmail = typed.some(e => (e.type||'')==='email') || /e-?mail/.test(blob);
    const hasPhone = typed.some(e => (e.type||'')==='tel') || /phone|tel\b|mobile/.test(blob);
    const reqFile = flds.some(e => (e.type||'')==='file' && (e.required||e.getAttribute('aria-required')==='true'));
    const bad = BAD.test(blob) || (BAD.test(head) && !/contact|enquir|inquir|message/i.test(head)) ||
                typed.some(e => (e.type||'')==='password');
    let why = [];
    if (!hasMsg) why.push('no message box');
    if (!hasEmail && !hasPhone) why.push('no email/phone field');
    if (bad) why.push('newsletter/login/search/checkout form');
    if (reqFile) why.push('requires file upload');
    if (typed.length > 18) why.push('too many fields');
    if (why.length) { reasons.push(why.join(', ')); continue; }
    let s = 10 + Math.min(typed.length, 8);
    if (/contact|enquir|inquir|message|get-in-touch/i.test(blob)) s += 5;
    if (typed.some(e => e.tagName==='TEXTAREA')) s += 5;
    if (s > bestScore) { bestScore = s; best = c; }
  }
  if (!best) return {found:false, reasons, formCount: conts.size};
  document.querySelectorAll('[data-fb-idx]').forEach(e => e.removeAttribute('data-fb-idx'));
  document.querySelectorAll('[data-fb-form]').forEach(e => e.removeAttribute('data-fb-form'));
  best.setAttribute('data-fb-form','1');
  const out = [];
  fieldsIn(best).forEach((el, i) => {
    el.setAttribute('data-fb-idx', String(i));
    out.push({idx:i, tag: el.tagName.toLowerCase(), type: (el.type||'').toLowerCase(),
      name: el.name||'', id: el.id||'', placeholder: el.placeholder||'',
      aria: el.getAttribute('aria-label')||'', label: labelOf(el),
      required: !!(el.required || el.getAttribute('aria-required')==='true'),
      checked: !!el.checked,
      options: el.tagName==='SELECT' ? [...el.options].map(o => o.text.trim()) : []});
  });
  const cap = best.querySelector('.g-recaptcha,[data-sitekey],.h-captcha,.cf-turnstile,iframe[src*="recaptcha/api2/anchor"],iframe[src*="hcaptcha"],iframe[src*="challenges.cloudflare"]');
  const capVisible = !!cap && vis(cap) && !/invisible/.test(cap.getAttribute('data-size')||'');
  return {found:true, fields: out, score: bestScore, captcha: capVisible, formCount: conts.size};
}
"""

STATE_JS = r"""
() => {
  const f = document.querySelector('[data-fb-form]');
  const vis = el => { if (!el || !el.isConnected) return false; const s = getComputedStyle(el);
    if (s.display==='none'||s.visibility==='hidden') return false;
    const r = el.getBoundingClientRect(); return r.width>4 && r.height>4; };
  const invalid = f ? [...f.querySelectorAll('input,textarea,select')].filter(e => vis(e) && (
      e.getAttribute('aria-invalid')==='true' || (e.willValidate && !e.checkValidity()))).map(e => e.name||e.id||e.type) : [];
  return {text: (document.body ? document.body.innerText : '').slice(0, 60000),
          formVisible: vis(f), invalid};
}
"""


def _same_site(a, b):
    n = lambda h: (h or "").lower().split(":")[0].removeprefix("www.")
    return n(urlparse(a).netloc) == n(urlparse(b).netloc)


def _emails_phones(text, html):
    emails = set(re.findall(r"mailto:([^\"'?>\s]+)", html, re.I))
    emails |= set(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text))
    emails = {e.strip(".").lower() for e in emails
              if not re.search(r"\.(png|jpe?g|gif|webp|svg)$|sentry|wixpress|example\.|domain\.com", e, re.I)}
    phones = set(re.findall(r"tel:([+\d][\d\s().-]{6,})", html, re.I))
    return sorted(emails)[:3], sorted(p.strip() for p in phones)[:2]


def _goto(page, url, cfg):
    try:
        r = page.goto(url, timeout=cfg.get("nav_timeout_ms", 25000), wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        page.wait_for_timeout(800)
        return r.status if r else 200
    except Exception:
        return 0


def _contact_links(page, base):
    try:
        links = page.eval_on_selector_all(
            "a[href]", "els => els.map(a => [a.href, (a.innerText||a.getAttribute('aria-label')||'').trim().slice(0,60)])")
    except Exception:
        return []
    strong, weak = [], []
    for href, text in links:
        if not href or SKIP_LINK.search(href) or not _same_site(href, base):
            continue
        href = href.split("#")[0]
        if href.rstrip("/") == base.rstrip("/"):
            continue
        hay = f"{text} {urlparse(href).path}"
        if CONTACT_LINK.search(hay):
            strong.append(href)
        elif WEAK_LINK.search(hay):
            weak.append(href)
    return list(dict.fromkeys(strong))[:4] + list(dict.fromkeys(weak))[:2]


def _scan(page):
    """Best qualifying form across the page and its iframes -> (frame, info)."""
    best = None
    reasons = []
    for fr in [page.main_frame] + [f for f in page.frames if f != page.main_frame]:
        try:
            info = fr.evaluate(SCAN_JS)
        except Exception:
            continue
        if info.get("found"):
            if best is None or info["score"] > best[1]["score"]:
                best = (fr, info)
        else:
            reasons += info.get("reasons", [])
    return best, reasons


def _fill(frame, fields, cfg):
    mapping, _ = fieldmap.map_fields(fields, cfg)
    msg_idx = [i for i, v in mapping.items() if v == cfg.get("message")]
    if not msg_idx:
        return 0, "message could not be placed in the form"
    filled = 0
    for f in fields:
        i = f["idx"]
        loc = frame.locator(f'[data-fb-form] [data-fb-idx="{i}"], [data-fb-idx="{i}"]').first
        try:
            if f["type"] == "email" and i not in mapping:
                mapping[i] = cfg.get("email", "")
            if i in mapping and mapping[i] is not fieldmap.RADIO:
                val = str(mapping[i])
                if f["tag"] == "select":
                    try:
                        loc.select_option(label=val, timeout=3000)
                    except Exception:
                        pick = fieldmap.choose_option(f["options"][1:] or f["options"], ["general", "other", "enquiry", "inquiry"])
                        if pick:
                            loc.select_option(label=pick, timeout=3000)
                else:
                    loc.fill(val, timeout=5000)
                filled += 1
            elif f["type"] == "checkbox" and f["required"] and not f["checked"]:
                loc.check(timeout=3000, force=True)          # consent / privacy box
            elif f["type"] == "radio" and f["required"]:
                if not frame.locator(f'[data-fb-form] input[type=radio][name="{f["name"]}"]:checked').count():
                    loc.check(timeout=3000, force=True)
            elif f["required"] and f["tag"] == "select":
                pick = fieldmap.choose_option(f["options"][1:] or f["options"], ["general", "other", "enquiry", "inquiry"])
                if pick:
                    loc.select_option(label=pick, timeout=3000)
            elif f["required"] and f["tag"] in ("input", "textarea") and f["type"] not in ("checkbox", "radio", "file"):
                filler = {"number": "1", "url": cfg.get("website_url", "https://www.easylistings.co.uk"),
                          "date": time.strftime("%Y-%m-%d")}.get(f["type"], cfg.get("company", "Easy Listings"))
                loc.fill(filler, timeout=5000)
        except Exception:
            continue
    return filled, ""


SUBMIT_SELS = (
    "[data-fb-form] button[type=submit]", "[data-fb-form] input[type=submit]",
    "[data-fb-form] button:not([type=button]):not([type=reset])",
    "[data-fb-form] [role=button]", "[data-fb-form] button",
    # links / divs styled as buttons ("Send" as an <a>)
    r"[data-fb-form] >> text=/^\s*(send|submit|send message|send enquiry|send inquiry|"
    r"enquire|inquire|contact us|get in touch)\s*$/i")


def _find_submit(frame):
    """The form's own visible submit control, or None."""
    for sel in SUBMIT_SELS:
        loc = frame.locator(sel)
        try:
            for k in range(loc.count()):
                b = loc.nth(k)
                if b.is_visible():
                    return b
        except Exception:
            continue
    return None


def _submit_label(frame):
    b = _find_submit(frame)
    if b is None:
        try:
            if frame.locator("[data-fb-form] button[type=submit], [data-fb-form] input[type=submit]").count()                     or frame.locator("form[data-fb-form]").count():
                return "hidden submit (form submits directly)"
        except Exception:
            pass
        return ""
    try:
        return (b.inner_text(timeout=2000) or b.get_attribute("value") or "button").strip()[:30]
    except Exception:
        return "button"


def _click_submit(frame):
    b = _find_submit(frame)
    if b is not None:
        try:
            b.click(timeout=6000)
            return True
        except Exception:
            pass
    try:   # submit control exists but is hidden/styled away
        hb = frame.locator("[data-fb-form] button[type=submit], [data-fb-form] input[type=submit]")
        if hb.count():
            hb.first.click(timeout=4000, force=True)
            return True
        if frame.evaluate("(() => { const f = document.querySelector('form[data-fb-form]');"
                          " if (f && f.requestSubmit) { f.requestSubmit(); return true } return false })()"):
            return True
    except Exception:
        pass
    try:   # last resort: Enter in a single-line field (never in the textarea)
        frame.locator('[data-fb-form] input[data-fb-idx]:not([type=checkbox]):not([type=radio])').first.press("Enter")
        return True
    except Exception:
        return False


def _verify(page, frame, before, posts, cfg, start_url):
    """Poll up to verify_secs for proof the message was accepted."""
    end = time.time() + cfg.get("verify_secs", 12)
    state = {}
    while time.time() < end:
        page.wait_for_timeout(1000)
        try:
            state = frame.evaluate(STATE_JS)
        except Exception:
            state = {"text": "", "formVisible": False, "invalid": []}   # frame navigated away
        url = page.url or ""
        new_text = state.get("text", "")
        # success wording that was NOT already on the page before submitting
        new_success = len(SUCCESS_TEXT.findall(new_text)) > len(SUCCESS_TEXT.findall(before["text"]))
        ok_post = any(200 <= s < 400 for s in posts)
        if url != start_url and SUCCESS_URL.search(urlparse(url).path + urlparse(url).query):
            return "sent", "redirected to " + urlparse(url).path
        if new_success:
            return "sent", "thank-you message appeared"
        if not state.get("formVisible") and ok_post:
            return "sent", "form replaced after site accepted it"
        if state.get("invalid"):
            return "rejected", "site flagged fields: " + ", ".join(map(str, state["invalid"][:4]))
        new_err = len(ERROR_TEXT.findall(new_text)) > len(ERROR_TEXT.findall(before["text"]))
        if new_err and time.time() > end - cfg.get("verify_secs", 12) + 4:
            return "rejected", "site showed an error message"
    if any(s >= 400 for s in posts) and not any(200 <= s < 400 for s in posts):
        return "rejected", "site refused the submission (HTTP %d)" % max(posts)
    if any(200 <= s < 400 for s in posts):
        return "likely_sent", "site accepted the data, showed no message"
    return "unconfirmed", "no confirmation and no network response"


def contact_site(ctx, url, cfg, dry_run=False, shot_dir=None, log=lambda m: None):
    t0 = time.time()
    budget = cfg.get("site_budget_secs", 90)
    res = dict(url=url, outcome="no_form", detail="", form_page="", emails="", phones="", shot="")
    page = ctx.new_page()
    seen_html = []
    try:
        base = url if "://" in url else "https://" + url
        status = _goto(page, base, cfg)
        if not status or status >= 500:
            # some sites only answer on the other host form, or on plain http
            host = urlparse(base).netloc
            alt_host = host[4:] if host.startswith("www.") else "www." + host
            for alt in (f"https://{alt_host}/", f"http://{host}/"):
                s2 = _goto(page, alt, cfg)
                if s2 and s2 < 500:
                    base, status = alt, s2
                    break
        if not status:
            res.update(outcome="unreachable", detail="site did not load")
            return res
        if status >= 400:
            res.update(outcome="unreachable", detail=f"site returned HTTP {status}")
            return res
        base = page.url
        if re.search(r"(forsale\.godaddy|sedo\.com|dan\.com|hugedomains|afternic|parkingcrew|bodis\.com|"
                     r"domain(s)? (is )?for sale|this domain (is|may be) for sale)", base + " " + page.title(), re.I):
            res.update(outcome="unreachable", detail="parked domain (for sale), no business here")
            return res
        try:
            seen_html.append((page.inner_text("body")[:40000], page.content()[:400000]))
        except Exception:
            pass
        home_best, _ = _scan(page)

        cands = _contact_links(page, base)
        if not cands:
            cands = [urljoin(base, p) for p in GUESS_PATHS]
        log(f"candidates: {cands[:6]}")

        chosen, why_not = None, []
        for c in cands:
            if time.time() - t0 > budget:
                break
            st = _goto(page, c, cfg)
            if not st or st >=400:
                continue
            try:
                seen_html.append((page.inner_text("body")[:40000], page.content()[:400000]))
            except Exception:
                pass
            best, reasons = _scan(page)
            why_not += reasons
            if best:
                chosen = best
                break
        if not chosen and home_best and time.time() - t0 < budget:
            _goto(page, base, cfg)
            chosen, _ = _scan(page)

        em, ph = set(), set()
        for text, html in seen_html:
            e, p = _emails_phones(text, html)
            em |= set(e)
            ph |= set(p)
        res.update(emails=";".join(sorted(em)[:3]), phones=";".join(sorted(ph)[:2]))

        if not chosen:
            res.update(outcome="no_form",
                       detail=("forms seen but rejected: " + "; ".join(dict.fromkeys(why_not))[:200])
                       if why_not else "no contact form on the site")
            return res

        frame, info = chosen
        res["form_page"] = page.url
        if info.get("captcha"):
            res.update(outcome="captcha", detail="visible captcha on the form: contact by hand")
            return res

        filled, err = _fill(frame, info["fields"], cfg)
        if err or filled < 2:
            res.update(outcome="no_form", detail=err or "could not fill the form")
            return res

        if shot_dir:
            try:
                p = shot_dir / (re.sub(r"[^a-z0-9]+", "_", urlparse(base).netloc.lower()) + ".png")
                frame.locator("[data-fb-form]").first.screenshot(path=str(p), timeout=8000)
                res["shot"] = str(p)
            except Exception:
                pass
        if dry_run:
            btn = _submit_label(frame)
            res.update(outcome="ready" if btn else "failed",
                       detail=(f"contact form filled ({filled} fields), send button '{btn}' found, not sent"
                               if btn else "form filled but no send button found"))
            return res

        before = frame.evaluate(STATE_JS)
        posts = []
        start_url = page.url

        def on_resp(r):
            try:
                if r.request.method in ("POST", "PUT") and not ANALYTICS.search(r.url):
                    posts.append(r.status)
            except Exception:
                pass
        page.on("response", on_resp)

        if not _click_submit(frame):
            res.update(outcome="failed", detail="could not find the form's submit button")
            return res
        outcome, detail = _verify(page, frame, before, posts, cfg, start_url)

        if outcome == "rejected" and "flagged" in detail:          # one repair pass
            alt = cfg.get("phone_alt") or re.sub(r"^\+44", "0", cfg.get("phone", ""))
            for f in info["fields"]:
                if f["type"] == "tel" or re.search(r"phone|tel|mobile", f["name"] + f["label"], re.I):
                    try:
                        frame.locator(f'[data-fb-idx="{f["idx"]}"]').fill(alt, timeout=3000)
                    except Exception:
                        pass
            before = frame.evaluate(STATE_JS)
            posts.clear()
            if _click_submit(frame):
                outcome, detail = _verify(page, frame, before, posts, cfg, start_url)
                detail = "after fixing fields: " + detail
        res.update(outcome=outcome, detail=detail)
        return res
    except Exception as e:
        res.update(outcome="failed", detail=f"{type(e).__name__}: {str(e)[:150]}")
        return res
    finally:
        res["secs"] = round(time.time() - t0, 1)
        try:
            page.close()
        except Exception:
            pass
