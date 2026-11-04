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


def _extract_inputs(page, cfg):
    """Pull fillable fields straight from the live DOM (handles JS-built forms)."""
    js = """
    () => {
      const out = [];
      const els = document.querySelectorAll('input, textarea, select');
      els.forEach((el, idx) => {
        const t = (el.type || '').toLowerCase();
        if (['hidden','submit','button','image','reset','file','checkbox','radio','search'].includes(t)) return;
        const st = window.getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden') return;
        const r = el.getBoundingClientRect();
        if (r.width < 5 || r.height < 5) return;
        let label = '';
        if (el.id) {
          const l = document.querySelector(`label[for="${el.id}"]`);
          if (l) label = l.innerText || '';
        }
        if (!label && el.closest('label')) label = el.closest('label').innerText || '';
        const opts = [];
        if (el.tagName === 'SELECT') {
          el.querySelectorAll('option').forEach(o => opts.push((o.innerText||'').trim()));
        }
        out.push({
          name: el.name || '', id: el.id || '',
          placeholder: el.placeholder || '', aria: el.getAttribute('aria-label') || '',
          label: label, type: t, tag: el.tagName.toLowerCase(),
          required: el.required || el.getAttribute('aria-required') === 'true',
          options: opts
        });
      });
      return out;
    }
    """
    try:
        data = page.evaluate(js)
    except Exception:
        return []
    return [dict(d, el=None, value="") for d in data]


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
        return 0

    # Find a submit control
    clicked = False
    for sel in ("button[type='submit']",
                "input[type='submit']",
                "button:has-text('Send')",
                "button:has-text('Submit')",
                "button:has-text('Get in touch')",
                "button:has-text('Contact')",
                "button:has-text('Enquire')",
                "button:has-text('Enquiry')",
                "form button"):
        try:
            btn = page.locator(sel).last
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=6000)
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        try:
            page.keyboard.press("Enter")
            clicked = True
        except Exception:
            pass

    return filled if clicked else 0


def attempt(url, cfg, ctx):
    """
    Browser attempt for one URL. Returns the same result dict shape as Tier 1.
    """
    import time as _t
    _T = _t.time()

    def _m(x):
        if cfg.get("_trace"):
            print("      [attempt %.1fs] %s" % (_t.time() - _T, x), flush=True)

    page = ctx.new_page()
    try:
        page.set_default_timeout(cfg.get("browser_timeout", 25000))

        # ── Phase 1: resolve the candidate list up front ────────────────────
        # candidate_urls() calls fetch() lazily while iterating, which used to
        # navigate the very page we were about to fill. Resolve first, fill
        # second. Uses the same page but only during discovery.
        cand_list = []
        try:
            for c in formdetect.candidate_urls(lambda u: _quick(u, page), url, cfg):
                cand_list.append(c)
                if len(cand_list) >= 14:
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
            _m("cand %d %s" % (hit, cand))
            try:
                page.goto(cand, timeout=cfg.get("browser_timeout", 25000),
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

                n = _fill_and_submit(fr, inputs, mapping, cfg)
                _m("  filled+submit -> %d" % n)
                if n:
                    page.wait_for_timeout(2000)
                    body = ""
                    try:
                        body = page.content()
                    except Exception:
                        pass
                    ok = bool(SUCCESS_WORDS.search(body))
                    return dict(
                        ok=ok, status="submitted", tier="browser",
                        detail="success wording found" if ok
                               else "filled + submitted (no confirm text)",
                        form_action=str(cand),
                        fields_hit=",".join(
                            str(fieldmap.classify(inputs[i].get("name", "") +
                                                  inputs[i].get("id", "") +
                                                  inputs[i].get("label", "")))
                            for i in mapping),
                    )
        return dict(ok=False, status="no_form", tier="browser",
                    error="no fillable form found in browser")
    except Exception as e:
        return dict(ok=False, status="failed", tier="browser", error=str(e)[:300])
    finally:
        try:
            page.close()
        except Exception:
            pass


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
            headless=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--test-type", "--no-sandbox", "--disable-dev-shm-usage"],
        )
        _m("browser launched")
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
