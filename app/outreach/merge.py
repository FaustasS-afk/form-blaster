"""
merge.py — combine all lead sources into final lists.

Reads leads_all.csv (main harvest) + leads_atlas.csv (jewellery/theme slice),
dedupes by domain, and writes:

  leads_master.txt       everything, deduped
  leads_independent.txt  no national chains  <- best outreach target
  leads_chains.txt       national chains
  leads_master.csv       full metadata

  python merge.py
  python merge.py --jewellery-only
"""

import argparse
import csv
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from scrape_leads import CHAINS

SOURCES = ["leads_all.csv", "leads_atlas.csv"]

# chain noise, plus corporate retail that is not a local shop
NOISE = re.compile(
    r"(goodwill|habitatncc|habitat\.org|valuevillage|platoscloset|"
    r"onceuponachild|salvationarmy|satruck|2ndswing|primethrift|"
    r"thesecondchanceshoppe|savers|buffaloexchange|crossroadstrading|"
    r"uptowncheapskate|clothesmentor|walmart|target|amazon|ebay|etsy|"
    r"facebook|tripadvisor|yelp|yellowpages|mapquest|bizapedia|"
    r"manta\.com|bbb\.org|chamberofcommerce|instagram|pinterest)",
    re.I,
)

JEWEL = re.compile(r"(jewel|gold|silver|diamond|gem|watch|horolog|antique|"
                   r"vintage|estate|pawn)", re.I)


def norm_domain(u):
    """
    Canonical domain for dedupe: lowercase, strip www., strip port.
    Without this, www.foo.com and foo.com are treated as different leads.
    """
    try:
        d = urlparse(u).netloc.lower().split(":")[0]
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jewellery-only", action="store_true")
    args = ap.parse_args()

    rows = {}
    for name in SOURCES:
        p = BASE / name
        if not p.exists():
            continue
        with open(p, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                site = (r.get("site") or "").strip()
                if not site.startswith("http"):
                    continue
                d = norm_domain(r.get("domain") or site) or norm_domain(site)
                if not d:
                    continue
                if d not in rows:
                    rows[d] = r
                else:
                    # prefer the entry that carries a name/phone
                    old = rows[d]
                    if (not old.get("name") and r.get("name")) or \
                       (not old.get("phone") and r.get("phone")):
                        rows[d] = r

    print(f"merged unique domains: {len(rows)}")

    if args.jewellery_only:
        rows = {d: r for d, r in rows.items()
                if JEWEL.search(d) or JEWEL.search(r.get("category", ""))
                or JEWEL.search(r.get("name", ""))}
        print(f"jewellery/antique/watch filtered: {len(rows)}")

    urls = {d: r["site"] for d, r in rows.items()}

    ind, ch = {}, {}
    for d, r in rows.items():
        if CHAINS.search(d) or NOISE.search(d):
            ch[d] = r["site"]
        else:
            ind[d] = r["site"]

    (BASE / "leads_master.txt").write_text(
        "\n".join(sorted(urls.values())) + "\n", encoding="utf-8")
    (BASE / "leads_independent.txt").write_text(
        "\n".join(sorted(ind.values())) + "\n", encoding="utf-8")
    (BASE / "leads_chains.txt").write_text(
        "\n".join(sorted(ch.values())) + "\n", encoding="utf-8")

    with open(BASE / "leads_master.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["domain", "site", "name", "phone",
                                          "category", "source"])
        w.writeheader()
        for d in sorted(rows):
            w.writerow(rows[d])

    print()
    print(f"  leads_master.txt      {len(urls)}")
    print(f"  leads_independent.txt {len(ind)}   <- best targets")
    print(f"  leads_chains.txt      {len(ch)}")


if __name__ == "__main__":
    main()
