"""
core/soup.py — one place to build a BeautifulSoup, with automatic parser
selection.

WHY THIS EXISTS
---------------
The whole codebase called `BeautifulSoup(html, "lxml")` directly. lxml is an
optional C extension, and bs4 does NOT fall back when it is missing — it raises
`FeatureNotFound`. Worse, one of those calls sits inside a *generator*, so the
exception escaped the caller's try/except and killed the entire site. Every
site failed.

Never call BeautifulSoup directly again: use `parse()`.
"""

from bs4 import BeautifulSoup

_parser = None


def get_parser():
    """Best available parser, decided once: lxml > html5lib > html.parser."""
    global _parser
    if _parser is not None:
        return _parser
    for cand in ("lxml", "html5lib", "html.parser"):
        try:
            BeautifulSoup("<html><body><p>x</p></body></html>", cand)
            _parser = cand
            return _parser
        except Exception:
            continue
    _parser = "html.parser"
    return _parser


def parse(markup, parser=None):
    """BeautifulSoup with a parser that is guaranteed to exist."""
    return BeautifulSoup(markup, parser or get_parser())


def safe_parse(markup, parser=None):
    """parse(), but returns None instead of raising. For generator contexts
    where an exception would escape and abort the caller."""
    try:
        return parse(markup, parser)
    except Exception:
        return None
