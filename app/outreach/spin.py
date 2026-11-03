"""
core/spin.py — spintax support.

Turns "{Hi|Hello|Hey} — quick question" into one randomly chosen variant.
Supports nested braces and is safe to call on text with no braces.

The Form Blaster config advertises spintax, so it must actually be applied —
otherwise the raw curly braces get typed into contact forms.
"""

import random
import re

_OUTER = re.compile(r"\{([^{}]*)\}")


def spin(text, max_passes=4):
    """Resolve spintax in `text`, picking one alternative at random."""
    if not text or "{" not in text:
        return text
    out = text
    for _ in range(max_passes):
        if "{" not in out:
            break
        new = _OUTER.sub(
            lambda m: random.choice(m.group(1).split("|")) if "|" in m.group(1)
            else m.group(0),
            out,
        )
        if new == out:
            break
        out = new
    return out


def spin_fields(cfg, keys=("message", "subject"), values=None):
    """
    Return a copy of cfg with spintax resolved and placeholders filled.

    Spintax runs whether or not cfg['spintax'] is true — when it is off the
    pass is simply a no-op on text with no braces — because placeholders such
    as [Name] must always resolve.

    `values` is the per-target dict; its 'name' field (the business name from
    the leads CSV) fills [Name]/[Business].
    """
    out = dict(cfg)
    use_spin = out.get("spintax", True)

    # expose the target's business name under a private key for the subber
    vals = dict(out)
    if values:
        bn = (values.get("name") or "").strip()
        if not bn:
            # 15% of leads have no business name. Fall back to a tidy version
            # of the domain so the greeting is still personalised.
            bn = _name_from_url(values.get("site") or values.get("url") or "")
        if bn:
            vals["_business"] = bn

    for k in keys:
        if not isinstance(out.get(k), str):
            continue
        text = spin(out[k]) if use_spin else out[k]
        out[k] = fill_placeholders(text, vals)
    return out


# ── placeholders ─────────────────────────────────────────────────────────
# [Name] / [Business] / [Site] get replaced per target. Anything we cannot
# resolve is stripped rather than left visible — "Hi [Name]," looks careless.

PLACEHOLDER = re.compile(r"\[(name|business|company|site|shop|store|first_?name)\]", re.I)


def _name_from_url(url):
    """
    'https://www.blackrockgalleries.com/contact' -> 'Blackrock Galleries'.
    Used when a lead has no business name, so the greeting stays personal.
    Splits on hyphens/dots and Title-Cases. Returns '' if nothing usable.
    """
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    if not host:
        return ""
    host = host.split(":")[0]
    for pre in ("www.", "shop.", "store."):
        if host.startswith(pre):
            host = host[len(pre):]
    parts = re.split(r"[.\-]", host)
    if not parts:
        return ""
    # drop the TLD and any short/generic pieces
    pieces = [p for p in parts[:-1] if len(p) > 2 and not p.isdigit()]
    if not pieces:
        return ""
    # drop subdomains that are just platform noise
    drop = {"com", "net", "org", "co", "uk", "us", "shop", "store", "square",
            "wixsite", "blogspot", "weebly", "wordpress", "myshopify", "bigcartel"}
    pieces = [p for p in pieces if p.lower() not in drop]
    if not pieces:
        return ""
    name = " ".join(pieces[-2:] if len(pieces) > 1 else pieces)
    # Split camelCase: '1833Schiersmarket' -> '1833 Schiersmarket'.
    # Only split letter->Upper and Lower->Upper; leave short digit runs alone
    # so '2canda' stays '2Canda' rather than becoming '2 Canda'.
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    if len(name) > 12:
        name = re.sub(r"(?<=[0-9])(?=[A-Za-z])", " ", name)
    name = re.sub(r"(?<=[A-Za-z])(?=[0-9])", " ", name)
    name = re.sub(r"\s{2,}", " ", name).strip()

    # glued lowercase words -> split on a common trade word
    if " " not in name and len(name) > 14:
        low = name.lower()
        for w in ("antiques", "antique", "vintage", "jewelry", "jewellery",
                  "collectibles", "emporium", "mercantile", "treasures",
                  "market", "mall", "shop", "store", "gallery"):
            i = low.find(w)
            if i > 3:
                name = name[:i] + " " + name[i:]
                break
    return name.title()[:26]


def _clean_name(n):
    """Tidy a business name for use in a greeting."""
    if not n:
        return ""
    n = str(n).strip()
    # drop trailing legal suffixes and shop-type words that read oddly
    n = re.sub(r"[,\-]\s*(llc|inc|co|corp|ltd|limited|llp)\.?$", "", n, flags=re.I)
    n = re.sub(r"\s+(llc|inc|co|corp|ltd|limited|llp)\.?$", "", n, flags=re.I)
    n = n.strip(" ,.-")

    # Truncate BEFORE fixing dangling punctuation, so a cut cannot leave "&" last
    if len(n) > 26:
        cut = n[:26]
        # prefer a word boundary, but never end mid-word
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        n = cut

    # if it still ends on a dangling joiner, take the previous word
    n = re.sub(r"[\s,]+(&|and|the|of|on|at|by|for)$", "", n, flags=re.I)
    n = re.sub(r"[\s,]+(&|and|the|of|on|at|by|for)$", "", n, flags=re.I)

    return n.strip(" ,.-&")


def fill_placeholders(text, values):
    """
    Replace [Name]-style tokens from a dict of values.
    Unresolved tokens are removed rather than left literal, then stray commas
    and double spaces are tidied.
    """
    if not text or "[" not in text:
        return text

    biz = _clean_name(values.get("_business") or "")

    def sub(m):
        key = m.group(1).lower()
        if key in ("name", "first_name", "firstname", "business", "company",
                   "shop", "store", "site"):
            return biz
        return ""

    out = PLACEHOLDER.sub(sub, text)

    # tidy artefacts left by an empty substitution
    out = re.sub(r"\s+,", ",", out)
    out = re.sub(r"^(\s*)(Hi|Hello|Hey)\s*,", r"\1\2,", out, flags=re.I | re.M)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out


def has_spintax(text):
    return bool(text and re.search(r"\{[^{}]*\|[^{}]*\}", text))
