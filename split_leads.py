"""
split_leads.py — separate independent shops from national chains.

Chains (Goodwill, Habitat, Plato's Closet, Value Village...) have corporate
contact forms. Independents are better targets for a personal message.

  python split_leads.py leads.txt
      -> leads_independent.txt
      -> leads_chains.txt
"""

import re
import sys
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from scrape_leads import CHAINS


def is_chain(url):
    p = urlparse(url)
    return bool(CHAINS.search((p.netloc + p.path).lower()))


def main():
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "leads.txt")
    if not src.is_absolute():
        src = BASE / src
    if not src.exists():
        print(f"not found: {src}")
        return

    urls = [l.strip() for l in open(src, encoding="utf-8") if l.strip()]
    ind = [u for u in urls if not is_chain(u)]
    ch = [u for u in urls if is_chain(u)]

    (BASE / "leads_independent.txt").write_text("\n".join(ind) + "\n", encoding="utf-8")
    (BASE / "leads_chains.txt").write_text("\n".join(ch) + "\n", encoding="utf-8")

    print(f"input       : {len(urls)}")
    print(f"independent : {len(ind)}  -> leads_independent.txt")
    print(f"chains      : {len(ch)}  -> leads_chains.txt")


if __name__ == "__main__":
    main()
