"""
photo_score.py — rank leads that HAVE a contact form by how weak their photos are.

Free (no API). For each lead the probe marked form_found it reads the homepage,
takes up to 8 content images (largest srcset entry, logos/icons skipped) and
reads each image's pixel size from its first 64 KB only.

  python photo_score.py leads_apify          (reads leads_apify.csv + leads_apify_probe.csv)

Writes:
  <stem>_final.csv        every form lead, poorest photos first, with the numbers
  <stem>_send.txt         form pages of the WEAK-photo leads, for run.py --input

Weak = few product photos on the homepage, or most images small (under 800 px
on the long side). A modern catalogue site serves 1000-2000 px images.
"""

import csv
import re
import statistics
import struct
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from core import db, submitter_http
from core.soup import safe_parse

SKIP_IMG = re.compile(r"(logo|icon|sprite|favicon|badge|payment|banner-ad|pixel|spacer|"
                      r"\.svg|data:image|facebook|instagram|google|paypal|visa|mastercard)", re.I)
SMALL = 800


def dims(b):
    """(w, h) from the first bytes of a PNG / GIF / WebP / JPEG, else None."""
    try:
        if b[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", b[16:24])
        if b[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", b[6:10])
        if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
            kind = b[12:16]
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", b[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if kind == b"VP8L":
                v = struct.unpack("<I", b[21:25])[0]
                return (v & 0x3FFF) + 1, ((v >> 14) & 0x3FFF) + 1
            if kind == b"VP8X":
                return (int.from_bytes(b[24:27], "little") + 1,
                        int.from_bytes(b[27:30], "little") + 1)
        if b[:2] == b"\xff\xd8":
            i = 2
            while i < len(b) - 9:
                if b[i] != 0xFF:
                    i += 1
                    continue
                m = b[i + 1]
                if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", b[i + 5:i + 9])
                    return w, h
                if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                    i += 2
                    continue
                i += 2 + struct.unpack(">H", b[i + 2:i + 4])[0]
    except Exception:
        return None
    return None


def image_urls(html, page):
    soup = safe_parse(html)
    if soup is None:
        return []
    out = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        srcset = img.get("srcset") or img.get("data-srcset") or ""
        if srcset:   # largest candidate
            best, bw = None, -1
            for part in srcset.split(","):
                bits = part.strip().split()
                if not bits:
                    continue
                wv = int(re.sub(r"\D", "", bits[1]) or 0) if len(bits) > 1 and bits[1].endswith("w") else 0
                if wv > bw:
                    best, bw = bits[0], wv
            src = best or src
        if not src or SKIP_IMG.search(src):
            continue
        out.append(urljoin(page, src))
    return list(dict.fromkeys(out))[:8]


def score(client, url):
    try:
        r = client.get(url)
        html = r.text if "html" in r.headers.get("content-type", "").lower() else ""
    except Exception:
        return None
    sizes = []
    for u in image_urls(html, str(r.url)):
        try:
            ir = client.get(u, headers={"Range": "bytes=0-65535"})
            d = dims(ir.content[:65536])
        except Exception:
            d = None
        if d and min(d) >= 60:          # ignore bullets / spacers
            sizes.append(max(d))
    n = len(sizes)
    med = int(statistics.median(sizes)) if sizes else 0
    small_share = sum(s < SMALL for s in sizes) / n if n else 1.0
    # 0 = strong catalogue photography, 100 = weak / missing photos
    poor = round(100 * (0.6 * small_share + 0.4 * (1 if n < 3 else 0)))
    # No <img> found usually means photos load via JavaScript or CSS
    # backgrounds (Wix, React, Next...), so they cannot be judged here.
    weak = "unknown" if n == 0 else ("yes" if poor >= 50 else "no")
    return dict(n_imgs=n, median_px=med, small_share=round(small_share, 2),
                photo_score=poor, weak=weak)


def main():
    stem = sys.argv[1] if len(sys.argv) > 1 else "leads_apify"
    leads = {r["site"]: r for r in csv.DictReader(open(BASE / f"{stem}.csv", encoding="utf-8"))}
    probe = [r for r in csv.DictReader(open(BASE / f"{stem}_probe.csv", encoding="utf-8"))
             if r["status"] == "form_found"]
    print(f"{len(probe)} leads with a form to score")

    cfg = db.load_config()
    local = threading.local()

    def one(p):
        if not hasattr(local, "c"):
            local.c = submitter_http._client(cfg)
        root = "{0.scheme}://{0.netloc}/".format(urlparse(p["url"]))
        s = score(local.c, root) or dict(n_imgs=0, median_px=0, small_share=1.0,
                                         photo_score=100, weak="unknown")
        meta = leads.get(p["url"], {})
        return dict(url=p["url"], form_page=p["form_page"], name=meta.get("name", ""),
                    phone=meta.get("phone", ""), category=meta.get("category", ""), **s)

    with ThreadPoolExecutor(16) as ex:
        rows = list(ex.map(one, probe))
    rows.sort(key=lambda r: -r["photo_score"])

    out = BASE / f"{stem}_final.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["url"])
        w.writeheader()
        w.writerows(rows)
    weak = [r["form_page"] for r in rows if r["weak"] == "yes"]
    (BASE / f"{stem}_send.txt").write_text("\n".join(weak) + "\n", encoding="utf-8")
    print(f"done: {len(rows)} scored, {len(weak)} with weak photos -> {stem}_send.txt")


if __name__ == "__main__":
    main()
