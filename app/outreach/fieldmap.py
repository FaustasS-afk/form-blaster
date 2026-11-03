"""
core/fieldmap.py — map our data onto a stranger's form fields.

Every site names things differently (your-email / email / field_3 / mail).
We score each input against patterns derived from its name, id, placeholder,
aria-label and associated <label> text, then assign our values.

Conservative on purpose: a wrong 'name' in the 'company' box is harmless,
but dumping the whole message into a 'first name' field is not.
"""

import re

# field kind -> ordered patterns (most specific first)
PATTERNS = {
    "email": [
        r"\be-?mail\b", r"emailaddress", r"yourmail", r"mailaddress",
        r"contactemail", r"useremail", r"customeremail", r"^mail$", r"_mail$",
        r"email_?addr", r"e-?mail-?2",
        r"e mail", r"e mail address", r"email address",
    ],
    "name": [
        r"\bname\b(?!.*\bfirst\b)(?!.*\blast\b)", r"yourname", r"fullname",
        r"full-?name", r"contactname", r"customer_?name", r"sender_?name",
        r"first_?and_?last", r"your-?name", r"display_?name", r"person_?name",
    ],
    "firstname": [
        r"first_?name", r"firstname", r"given_?name", r"fname", r"first-?name",
        r"^first$", r"forename", r"first name", r"givenname",
    ],
    "lastname": [
        r"last_?name", r"lastname", r"surname", r"family_?name",
        r"^lname$", r"last-?name", r"^last$", r"last name", r"family name",
    ],
    "phone": [
        r"\bphone\b", r"\btel\b", r"telephone", r"mobile", r"cell",
        r"contact_?number", r"phonenumber", r"phone_?no", r"daytime_?phone",
    ],
    "company": [
        r"company", r"organisation", r"organization", r"business",
        r"business_?name", r"firm", r"employer", r"shop_?name", r"store_?name",
        r"brand_?name", r"account_?name",
    ],
    "website": [
        r"website", r"web_?site", r"\burl\b", r"web_?address", r"site_?url",
        r"domain", r"homepage", r"your_?site", r"company_?url", r"link",
    ],
    "subject": [
        r"subject", r"^re$", r"enquiry_?type", r"inquiry_?type",
        r"topic", r"department", r"reason_?for", r"how_?can_?we_?help",
        r"nature_?of", r"what_?is_?this", r"service_?interest", r"enquiry_?about",
    ],
    "message": [
        r"message", r"comment", r"comments", r"enquiry", r"inquiry",
        r"\bquestion\b", r"\bdetails\b", r"description", r"body",
        r"your_?message", r"how_?can_?we_?help", r"tell_?us",
        r"what_?can_?we", r"project_?details", r"textarea_?msg",
        r"additional_?info", r"notes", r"^text$", r"content",
        r"your message", r"how can we help", r"tell us", r"tell us more",
        r"how may we help", r"leave a message", r"your enquiry",
        r"your inquiry", r"more info", r"additional info",
    ],
}

# inputs we should never fill
SKIP_TYPES = {
    "hidden", "submit", "button", "image", "reset", "file",
    "checkbox", "radio", "search", "captcha",
}

# newsletter signups often look like contact forms but are just email boxes
NEWSLETTER_HINTS = [
    r"newsletter", r"subscribe", r"mailing_?list", r"signup_?for", r"sign_?up",
]

RADIO = object()   # marker for radio-group needs


def _dedupe_token(t):
    """
    'emailemail' -> 'email', 'business_namebusiness_name' -> 'business_name'.
    Handles the common case where name and id are the same attribute value.
    """
    if len(t) < 4:
        return t
    # Try progressively: is the token exactly X repeated twice?
    half = len(t) // 2
    if len(t) % 2 == 0 and t[:half] == t[half:]:
        return t[:half]
    # Also handle 'enquiryenquir'-style near-repeats by longest common prefix
    for cut in range(half + 2, max(2, len(t) // 2 - 3), -1):
        if t[:cut] == t[cut:cut * 2]:
            return t[:cut]
    # Partial overlap: 'enquiryenquir' -> 'enquiry'
    for cut in range(len(t) - 3, 4, -1):
        stem = t[:cut]
        if t[cut:].startswith(stem[:max(3, len(t) - cut)]):
            return stem
        if stem.startswith(t[cut:]) and len(t[cut:]) >= 4:
            return stem
    return t


def is_meaningless(name):
    """True for Field1, q3, field_12, input_4 style attribute names."""
    if not name:
        return True
    return bool(re.fullmatch(
        r"(field|q|input|text|name|element|entry)?[_.\-]?\d+[a-z0-9\-_]*",
        name.strip().lower()))


def ordinal_fallback(inputs, cfg):
    """
    Last resort for forms whose names carry no meaning — Wufoo's Field1..N,
    JotForm's q1..N, generic field_3.

    Uses ORDER plus input TYPE, which is reliable on contact forms:
    first type=email -> email, last textarea -> message,
    first type=tel -> phone, first free text box -> name.
    """
    mapping = {}
    if not inputs:
        return mapping

    def fillable(i):
        return (i.get("type") not in SKIP_TYPES
                and i.get("tag") in ("input", "textarea", "select"))

    idxs = [n for n, i in enumerate(inputs) if fillable(i)]
    if len(idxs) < 2:
        return mapping

    def first_type(t):
        for n in idxs:
            if inputs[n].get("type") == t:
                return n
        return None

    e = first_type("email")
    if e is not None:
        mapping[e] = cfg.get("email", "")

    tareas = [n for n in idxs if inputs[n].get("tag") == "textarea"]
    if tareas:
        mapping[tareas[-1]] = cfg.get("message", "")

    p = first_type("tel")
    if p is not None:
        mapping[p] = cfg.get("phone", "")

    for n in idxs:
        if n in mapping:
            continue
        if inputs[n].get("tag") == "input" and inputs[n].get("type") in ("", "text"):
            mapping[n] = cfg.get("name", "")
            break

    return {k: v for k, v in mapping.items() if v}


def _norm(*parts):
    """
    Join and normalise any available attribute text.

    Crucially: <input name="email" id="email"> would otherwise become    'emailemail', which no word-boundary pattern matches. Collapse any token
    that is exactly the previous token repeated, and de-dupe identical words.
    """
    raw = " ".join(p.lower().strip() for p in parts if p)
    if not raw:
        return ""
    raw = re.sub(r"[^a-z0-9_\- ]+", " ", raw)
    toks = [t for t in raw.split() if t]
    seen = set()
    out = []
    for t in toks:
        t = _dedupe_token(t)
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return " ".join(out)


def _dedupe_text(s):
    """
    Collapse immediately-repeated substrings left by name/id concatenation.
      'email email'                  -> 'email'
      'business namebusiness name'   -> 'business name'
      'enquiry enquir'               -> 'enquiry'
    """
    # exact word-pair repetition: 'email email'
    s = re.sub(r"\b(\w+)\s+\1\b", r"\1", s)
    # 'business namebusiness name' -> collapse the doubled run
    m = re.match(r"^(.*?)\s*\1$", s)
    if m and len(m.group(1)) >= 2:
        s = m.group(1)
    # leftover concatenation with no separator: 'emailemail', 'namename'
    for tok in list(s.split()):
        d = _dedupe_token(tok)
        if d != tok:
            s = s.replace(tok, d)
    return re.sub(r"\s+", " ", s).strip()


def _strip_rubbish(t):
    """Remove bracketed/index noise: field[3], wpforms[fields][1][email]."""
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    # collapse filler words
    for junk in ("wpforms", "gform", "form", "field", "input", "cf7", "elementor",
                 "gravity", "field id", "form id", "wpcf7", "ninja", "fluentform"):
        t = t.replace(junk, " ")
    t = re.sub(r"\s+", " ", t).strip()
    return _dedupe_text(t)


def classify(haystack):
    """
    Return the best field-kind for a text blob, or None.

    Order matters: more specific kinds are tested before the greedy 'name'.
    'business_name' must beat 'name', but a bare 'name' must still match.
    """
    h = _strip_rubbish(haystack)
    if not h:
        return None
    # specific kinds first, greedy 'name' last
    order = ("email", "phone", "company", "website", "firstname", "lastname",
             "subject", "message", "name")
    for kind in order:
        for p in PATTERNS[kind]:
            if re.search(p, h):
                return kind
    return None


def is_newsletter(field_blobs, form_text):
    """Heuristic: is this a subscribe strip rather than a contact form?"""
    blob = " ".join(field_blobs).lower()
    combined = blob + " " + (form_text or "").lower()
    hits = sum(1 for p in NEWSLETTER_HINTS if re.search(p, combined))
    # A newsletter strip normally has <=2 fields and no message box
    has_message = "message" if any(
        classify(b) == "message" for b in field_blobs) else ""
    if hits and not has_message and len(field_blobs) <= 2:
        return True
    return False


def map_fields(inputs, cfg):
    """
    inputs: list of dicts with keys name,id,type,placeholder,aria,label,required,options
    cfg: config dict with name/email/phone/company/website/message/subject

    Returns (mapping, unmapped_required)
      mapping: {input_index: value}  (value may be RADIO for select/radio groups)
    """
    mapping = {}
    used = set()

    # Pass 1 — exact/strong matches against patterns
    for i, inp in enumerate(inputs):
        if inp.get("type") in SKIP_TYPES:
            continue
        blob = _norm(inp.get("name"), inp.get("id"), inp.get("placeholder"),
                     inp.get("aria"), inp.get("label"))

        kind = classify(blob)
        if not kind:
            # option text can disambiguate a select
            if inp.get("options"):
                kind = classify(" ".join(inp["options"][:6]))
        if not kind or kind in used:
            # allow firstname/lastname to coexist with a generic 'name'
            if not kind:
                continue
            if kind in used and kind not in ("firstname", "lastname"):
                continue

        value = cfg.get(kind, "")
        if kind == "firstname" and not cfg.get("firstname"):
            value = (cfg.get("name", "") or "").split(" ")[0]
        if kind == "lastname" and not cfg.get("lastname"):
            parts = (cfg.get("name", "") or "").split(" ")
            value = parts[-1] if len(parts) > 1 else ""

        if not value:
            continue

        mapping[i] = value
        used.add(kind)

    # Pass 2 — a form with exactly one free text box gets the message
    if "message" not in used:
        text_idxs = [i for i, inp in enumerate(inputs)
                     if inp.get("tag") == "textarea"
                     and inp.get("type") not in SKIP_TYPES
                     and i not in mapping]
        if text_idxs:
            mapping[text_idxs[0]] = cfg.get("message", "")
            used.add("message")
        else:
            # single-line box with no other match -> it's the body
            free = [i for i, inp in enumerate(inputs)
                    if inp.get("type") in (None, "", "text")
                    and i not in mapping]
            if len(free) == 1 and cfg.get("message"):
                mapping[free[0]] = cfg.get("message", "")
                used.add("message")

    # Pass 3 — subject into any remaining select if unused
    if "subject" not in used:
        for i, inp in enumerate(inputs):
            if i in mapping:
                continue
            if inp.get("tag") == "select" and inp.get("options"):
                opts = [o for o in inp["options"] if o and len(o) < 60]
                pick = choose_option(opts, ["general", "other", "enquiry",
                                            "inquiry", "sales", "new business",
                                            "question", "general enquiry"])
                if pick:
                    mapping[i] = pick
                    used.add("subject")
                    break

    unmapped_required = [
        i for i, inp in enumerate(inputs)
        if inp.get("required") and i not in mapping
        and inp.get("type") not in SKIP_TYPES
    ]

    # Pass 4 — nothing meaningful matched (Wufoo Field1..N, JotForm q1..N).
    # Fall back to positional/typed mapping rather than submitting nothing.
    if len(mapping) < 2:
        weak = all(is_meaningless((inp.get("name") or inp.get("id") or ""))
                   for inp in inputs if inp.get("name") or inp.get("id"))
        if weak or not mapping:
            fb = ordinal_fallback(inputs, cfg)
            for k, v in fb.items():
                mapping.setdefault(k, v)

    return mapping, unmapped_required


def choose_option(options, prefs):
    if not options:
        return None
    low = [(o, o.lower()) for o in options if o]
    for want in prefs:
        for original, l in low:
            if want in l:
                return original
    # skip obvious placeholders
    for original, l in low:
        if not any(k in l for k in ("select", "choose", "--", "pick", "please")):
            return original
    return None
