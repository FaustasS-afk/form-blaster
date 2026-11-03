"""
core/formdetect.py — find the contact form on a page.

Strategy: try common contact paths, then crawl the homepage for contact-ish
links. Returns the best candidate form plus its inputs.
"""

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

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


def find_best_form(soup, page_url, cfg, min_score=0):
    """
    Return (form, inputs, score) for the best form on the page, or None.

    min_score=0 (was 3) because a form with only an email box still gets our
    address in front of a human — better to send something than nothing. The
    -6 'no message box' penalty in score_form still ranks real contact forms
    above these, so they only win when nothing better exists.
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
    if CONTACT_HINT.search(parsed.path or "") or parsed.path in ("", "/"):
        r = emit(start_url)
        if r:
            yield r

    # Then common contact paths
    for p in cfg.get("contact_paths", []):
        r = emit(urljoin(root, p))
        if r:
            yield r

    # Then homepage link crawl
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
            soup = BeautifulSoup(html, "lxml")
            kws = cfg.get("homepage_link_keywords", [])

            for a in soup.find_all("a", href=True):
                href = a["href"]
                if SKIP_HREF.search(href):
                    continue
                text = a.get_text(" ", strip=True).lower()
                full = urljoin(final or root, href)
                if urlparse(full).netloc != parsed.netloc:
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

    # Finally: common second-level paths under a contact section
    for extra in ("/contact/contact-us", "/contact-us/form", "/contact/form",
                  "/customer-service", "/contact-us/", "/enquiry-form"):
        r = emit(urljoin(root, extra))
        if r:
            yield r
