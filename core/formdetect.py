"""
core/formdetect.py — find the contact form on a page.

Strategy: try common contact paths, then crawl the homepage for contact-ish
links. Returns the best candidate form plus its inputs.
"""

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from core.soup import parse as _parse, safe_parse as _safe_parse

SKIP_HREF = re.compile(
    r"(\.pdf|\.jpg|\.png|\.zip|mailto:|tel:|javascript:|#|"
    r"facebook|instagram|twitter|linkedin|youtube|tiktok|pinterest)",
    re.I,
)

CONTACT_HINT = re.compile(
    r"(contact|get-?in-?touch|enquir|inquir|reach|say-?hello|talk-?to|"
    r"support|help|connect|about)",
    re.I,
)


def _site_key(netloc):
    """Hostname with a leading 'www.' removed, for tolerant same-site compares."""
    n = (netloc or "").lower()
    return n[4:] if n.startswith("www.") else n


def _inputs_from_form(form, soup):
    """Extract a normalised list of fillable inputs from a <form>."""
    inputs = []
    for el in form.find_all(["input", "textarea", "select"]):
        tag = el.name
        itype = (el.get("type") or "").lower()
        if tag == "input" and itype in ("hidden", "submit", "button", "image", "reset"):
            # keep hidden for CSRF, but mark so mapper can skip
            pass

        opts = []
        if tag == "select":
            opts = [o.get_text(strip=True) for o in el.find_all("option")]

        label_txt = ""
        fid = el.get("id")
        if fid:
            lab = soup.find("label", attrs={"for": fid})
            if lab:
                label_txt = lab.get_text(" ", strip=True)
        if not label_txt:
            parent = el.find_parent("label")
            if parent:
                label_txt = parent.get_text(" ", strip=True)

        inputs.append({
            "tag": tag,
            "type": itype or ("text" if tag != "textarea" else "textarea"),
            "name": el.get("name") or "",
            "id": el.get("id") or "",
            "placeholder": el.get("placeholder") or "",
            "aria": el.get("aria-label") or "",
            "label": label_txt,
            "required": el.has_attr("required"),
            "options": opts,
            "value": el.get("value") or "",
            "el": el,
        })
    return inputs


def score_form(form, inputs, cfg):
    """
    Higher is better. Reward an email field and a message box.
    Punish login forms, search boxes and giant forms.

    IMPORTANT: many real forms (especially J2EE/.do apps) have fields with no
    name, no id and no placeholder. Those used to score 0 and get rejected.
    We now credit structural evidence too: a <textarea> means 'message', and
    a lone text input next to a textarea is worth something.
    """
    score = 0
    blob = " ".join(
        (i["name"] + " " + i["id"] + " " + i["placeholder"] + " " + i["aria"] + " " + i["label"])
        for i in inputs
    ).lower()

    fillable = [i for i in inputs if i["type"] not in ("hidden", "submit", "button")]
    if not fillable:
        return -1

    texts = [i for i in fillable if i["tag"] == "textarea"]
    textin = [i for i in fillable if i["tag"] == "input"
              and i["type"] in ("", "text", "email", "tel")]

    # A search box alone is not a contact form.
    is_search = bool(re.search(r"search|keyword|\bq\b|query", blob))
    if is_search and len(fillable) <= 2 and not re.search(r"e-?mail", blob):
        return -1

    # Named signals
    if re.search(r"e-?mail", blob):
        score += 5
    if re.search(r"message|comment|enquir|inquir|question|details", blob):
        score += 4
    if re.search(r"\bname\b", blob):
        score += 2
    if re.search(r"phone|tel|company|business|subject", blob):
        score += 1
    if any(i["tag"] == "select" for i in inputs):
        score += 1

    # Structural fallbacks for unnamed forms
    if not re.search(r"message|comment|enquir|inquir|question|details", blob):
        if texts:
            score += 3          # a textarea is almost always the message box

    if not re.search(r"e-?mail", blob):
        # an input typed email, or a lone text box beside a textarea
        if any(i["type"] == "email" for i in fillable):
            score += 4
        elif len(textin) >= 2 and texts:
            score += 2

    # A form with several text inputs and a textarea is very likely contact.
    if len(fillable) >= 3 and texts:
        score += 2

    # Penalties
    if re.search(r"password|log ?in|sign ?in|username", blob) and \
       not re.search(r"e-?mail", blob) and not texts:
        score -= 3
    if len(fillable) > cfg.get("max_form_fields", 12):
        score -= 2
    if len(fillable) < 2:
        score -= 2

    # Newsletter / comment forms are real but nobody reads them like an enquiry.
    # Reward genuine contact forms so they win when a page has both.
    action = (form.get("action") or "").lower()
    is_newsletter = bool(re.search(
        r"(subscri|newsletter|kmail|mailchimp|mc4wp|feedburner|createsend|"
        r"sendy|convertkit|klaviyo|constantcontact|hubspot.*subscri)", action + " " + blob))
    is_comment = bool(re.search(r"(wp-comments-post|comment-?form|respond)", action))
    if is_newsletter:
        score -= 4
    if is_comment:
        score -= 3

    # A dedicated contact action is a strong positive.
    if re.search(r"(contact|enquir|inquir|get-?in-?touch|send)", action):
        score += 3

    # A form with NO message box is nearly useless for outreach — we would only
    # send an email address and drop the pitch. Shopify footers are like this
    # (contact[email] + subscribe). Heavily reward having somewhere for the text.
    if not texts and not re.search(r"message|comment|enquir|inquir|question|detail", blob):
        score -= 6

    return score


def needs_body(soup):
    """
    True if the page has no usable form but does have plenty of links,
    i.e. it is a contact HUB pointing at real forms further in.
    """
    if soup.find("form") and find_best_form(soup, "", {}) is not None:
        return False
    links = [a for a in soup.find_all("a", href=True) if not SKIP_HREF.search(a["href"])]
    return len(links) >= 8


def _action_url(form, base):
    """
    Resolve a form's action safely.

    Handles three real-world traps:
      - `base` may be an httpx.URL, so coerce to str before urljoin.
      - J2EE sites emit action="x.do;jsessionid=ABC;jsessionid=ABC", where the
        session id is duplicated and breaks the path. Keep it once.
      - Actions with a #fragment (e.g. "/contact#contact_form") are extremely
        common for WordPress/Elementor forms. A fragment must NOT be sent to
        the server, and posting to it verbatim can 404.
    """
    base = str(base)
    action = (form.get("action") or "").strip()
    if not action or action.startswith("#"):
        return base

    # Collapse a duplicated ;jsessionid=...;jsessionid=... suffix
    if ";jsessionid=" in action:
        head, _, tail = action.partition(";jsessionid=")
        sid = tail.split(";")[0]
        rest = tail.split(";", 1)[1] if ";" in tail else ""
        if rest.lower().startswith("jsessionid="):
            rest = ""
        action = head + ";jsessionid=" + sid + rest

    if action.startswith(("http://", "https://")):
        return action
    return urljoin(base, action)


def strip_fragment(u):
    """Remove a #fragment so the URL is safe to POST/GET against."""
    return str(u).split("#", 1)[0] if u else u


def action_variants(form, base):
    """
    Candidate submit URLs for a form, best first, fragment-free.

    Order:
      1. the action as written, fragment stripped
      2. absolute form of the action
      3. the page URL itself (many CMS forms post to their own page)
    """
    base = str(base)
    base_nf = strip_fragment(base)
    out = []

    action = (form.get("action") or "").strip()
    if action and not action.startswith("#"):
        primary = strip_fragment(_action_url(form, base))
        if primary:
            out.append(primary)
        if action and not action.startswith(("http", "#")):
            p = urlparse(base_nf)
            root = f"{p.scheme}://{p.netloc}"
            alt = root + "/" + action.lstrip("/")
            alt = strip_fragment(alt)
            if alt not in out:
                out.append(alt)

    if base_nf and base_nf not in out:
        out.append(base_nf)
    return out


def find_best_form(soup, page_url, cfg, min_score=3):
    """
    Return (form, inputs, score) for the best form on the page, or None.

    min_score=2 rejects bare search boxes and login-only stubs (which score
    0 or negative). A real contact form needs at least an email + something.
    """
    best = None
    for form in soup.find_all("form"):
        inputs = _inputs_from_form(form, soup)
        s = score_form(form, inputs, cfg)
        if s < min_score:
            continue
        if best is None or s > best[2]:
            best = (form, inputs, s)
    return best


def candidate_urls(fetch, start_url, cfg):
    """
    Yield URLs to try for a given start URL.
    fetch(url) -> (final_url, html) or (final_url, None)
    """
    seen = set()
    parsed = urlparse(start_url)
    root = f"{parsed.scheme}://{parsed.netloc}"

    def emit(u):
        if u not in seen:
            seen.add(u)
            return u
        return None

    # The URL itself first — the user may have given the contact page directly
    # Always first: a probed lead points straight at its form page, whatever
    # that page's path is called.
    if True:
        r = emit(start_url)
        if r:
            yield r

    # Then homepage link crawl. Done BEFORE guessing paths: the homepage is
    # already fetched (cached by the caller), and its real contact links exist,
    # whereas most guessed paths are 404 round-trips.
    if cfg.get("crawl_homepage"):
        home = root + "/"
        seeds = [start_url]
        if start_url.rstrip("/") != root:
            seeds.append(home)

        found = []
        for seed in seeds[:2]:
            final, html = fetch(seed)
            if not html:
                continue
            # safe_parse: this generator is consumed by the caller's for-loop,
            # so an exception here escapes their try/except and aborts the
            # whole site. Never let a parse failure propagate.
            soup = _safe_parse(html)
            if soup is None:
                continue
            kws = cfg.get("homepage_link_keywords", [])

            for a in soup.find_all("a", href=True):
                href = a["href"]
                if SKIP_HREF.search(href):
                    continue
                text = a.get_text(" ", strip=True).lower()
                full = urljoin(final or root, href)
                # Same-site check that tolerates a www/non-www mismatch.
                # Sites link to `domain.com/pages/contact` from `www.domain.com`
                # constantly; a strict netloc compare silently threw those away,
                # which is exactly the page holding the real contact form.
                if _site_key(urlparse(full).netloc) != _site_key(parsed.netloc):
                    continue
                # Strong signal: the link text or href says contact/enquiry
                if any(k in text for k in kws) or CONTACT_HINT.search(href):
                    found.append(full)

        # De-dupe, keep order, try the most contact-ish first
        ranked = sorted(
            dict.fromkeys(found),
            key=lambda u: 0 if CONTACT_HINT.search(urlparse(u).path or "") else 1
        )
        for u in ranked[:20]:
            r = emit(u)
            if r:
                yield r

    # Then common contact paths, most-likely-first (fallback when the
    # homepage links did not lead to a form).
    # Measured: the configured order made a typical Shopify site waste 4
    # guaranteed 404s (/contact, /contact-us, /contactus, /contacts) before
    # reaching the /pages/contact that actually works. Every 404 costs a
    # round-trip. Reorder so the high-hit-rate platform paths come first.
    paths = cfg.get("contact_paths", [])
    priority = ["/pages/contact", "/pages/contact-us", "/contact",
                "/contact-us", "/pages/contact-us-1", "/contact-1"]
    ordered = [p for p in priority if p in paths]
    ordered += [p for p in paths if p not in ordered]
    for p in ordered:
        r = emit(urljoin(root, p))
        if r:
            yield r

    # Finally: common second-level paths under a contact section
    for extra in ("/contact/contact-us", "/contact-us/form", "/contact/form",
                  "/customer-service", "/contact-us/", "/enquiry-form"):
        r = emit(urljoin(root, extra))
        if r:
            yield r
