"""
core/embeds.py — detect forms hidden inside iframes and known form widgets.

Many small-business contact pages have ZERO <form> tags in the main document:
the form lives in a cross-origin iframe (Wufoo, JotForm, Typeform, Google
Forms, Formstack, HubSpot) or is injected by a WordPress form plugin.

Tier 1 (plain HTTP) can fetch the iframe's own URL directly and treat it as a
standalone form page. That converts a whole class of 'no_form' into real
submissions without needing a browser at all.
"""

import re

# host pattern -> how to handle
IFRAME_FORM_HOSTS = [
    (r"wufoo\.com", "wufoo"),
    (r"jotform\.(com|us|eu)", "jotform"),
    (r"typeform\.com", "typeform"),
    (r"google\.com/forms", "google"),
    (r"docs\.google\.com/forms", "google"),
    (r"formstack\.com", "formstack"),
    (r"hsforms\.(com|net)", "hubspot"),
    (r"hubspot", "hubspot"),
    (r"formspree\.io", "formspree"),
    (r"getform\.io", "getform"),
    (r"cognitoforms\.com", "cognito"),
    (r"123formbuilder\.com", "123form"),
    (r"formsite\.com", "formsite"),
]

# WordPress / site-builder form plugins that render inline (no iframe needed,
# but the form may be present yet hidden until JS runs)
PLUGIN_HINTS = re.compile(
    r"(wpforms|wpcf7|contact-form-7|gform_|gravity|forminator|"
    r"fluentform|ninja_forms|cf7|elementor-form|et_pb_contact)",
    re.I,
)


def iframe_srcs(html):
    """All candidate iframe form URLs found in a page."""
    from bs4 import BeautifulSoup
    out = []
    try:
        s = BeautifulSoup(html, "lxml")
    except Exception:
        return out
    for f in s.find_all(["iframe", "embed", "object"]):
        src = (f.get("src") or f.get("data-src") or f.get("data-lazy-src")
               or f.get("data-original") or "")
        if src:
            out.append(src.strip())
    # data-* attributes sometimes hold it
    for m in re.finditer(r'(?:src|data-src)=["\']([^"\']*(?:wufoo|jotform|typeform|google\.com/forms|formstack|hsforms|formspree)[^"\']*)["\']',
                         html or "", re.I):
        out.append(m.group(1))
    return list(dict.fromkeys(out))


def is_form_iframe(src):
    low = (src or "").lower()
    for pat, kind in IFRAME_FORM_HOSTS:
        if re.search(pat, low):
            return kind
    return None


def plugin_form_present(html):
    """True if the page references a known inline form plugin."""
    return bool(html and PLUGIN_HINTS.search(html))


def google_form_url(src):
    """
    Normalise a Google Forms embed into its fillable URL.
    https://docs.google.com/forms/d/e/XXXX/viewform?embedded=true -> keep as is.
    """
    if not src:
        return src
    if "embedded=true" not in src and "viewform" in src:
        return src.split("?")[0] + "?embedded=true"
    return src
