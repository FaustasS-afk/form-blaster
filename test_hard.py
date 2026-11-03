"""
Test Bright Data against sites where PLAIN httpx genuinely fails RIGHT NOW.

The previous test used urls that had recovered. This uses live-hard targets:
hCaptcha walls, Cloudflare WAF pages, and 403/429 responders.
"""
import os, sys, time
sys.path.insert(0, r"C:\Users\SellJ\Desktop\FORM BLASTER")
os.chdir(r"C:\Users\SellJ\Desktop\FORM BLASTER")

import httpx
from bs4 import BeautifulSoup
from core import db, formdetect, submitter_http

KEY = ""
with open(r"C:\Users\SellJ\Documents\deepseek-harness\default-workspace\.secrets\env.txt",
          encoding="utf-8", errors="ignore") as f:
    for line in f:
        if line.strip().startswith("BRIGHTDATA_API_KEY"):
            KEY = line.partition("=")[2].strip().strip('"').strip("'").rstrip("]").strip()
ZONE = "web_unlocker1"

cfg = db.load_config()
plain = httpx.Client(follow_redirects=True, timeout=20,
                     headers={"User-Agent": cfg["user_agent"]})


def bright(url):
    t0 = time.time()
    try:
        r = httpx.post("https://api.brightdata.com/request",
                       headers={"Content-Type": "application/json",
                                "Authorization": f"Bearer {KEY}"},
                       json={"zone": ZONE, "url": url, "format": "raw"},
                       timeout=90)
        return r.status_code, r.text, time.time() - t0
    except Exception as e:
        return None, f"{type(e).__name__}", time.time() - t0


# Hard targets: known WAF / captcha publishers
TARGETS = [
    "https://www.watchfinder.co.uk/",          # was WAF blocked
    "https://www.platoscloset.com/locations/dover-de",  # WAF blocked
    "https://www.thejewelleryeditor.com/",     # was 403
    "https://www.gov.uk/contact",              # was 404/429
    "https://www.thehistoryroom.com/",         # was 429
    "https://www.antiques-atlas.com/contact",  # was WAF blocked
]

print("=" * 76)
print("  HARD TARGETS: plain vs bright")
print("=" * 76)

for u in TARGETS:
    try:
        p = plain.get(u)
        pcode, ptext = p.status_code, p.text
    except Exception as e:
        pcode, ptext = 0, ""

    blocked_plain = submitter_http.detect_block(pcode, ptext)
    pform = False
    try:
        pform = formdetect.find_best_form(BeautifulSoup(ptext, "lxml"), u, cfg) is not None
    except Exception:
        pass

    print()
    print(u[:70])
    print(f"   plain  : {pcode:<4} {len(ptext):>7}b  block={blocked_plain}  form={pform}")

    bcode, btext, bt = bright(u)
    if bcode is None:
        print(f"   bright : ERR {btext}")
        continue
    bblocked = submitter_http.detect_block(bcode, btext)
    bform = False
    try:
        bform = formdetect.find_best_form(BeautifulSoup(btext, "lxml"), u, cfg) is not None
    except Exception:
        pass
    print(f"   bright : {bcode:<4} {len(btext):>7}b  block={bblocked}  form={bform}  ({bt:.1f}s)")

    if (blocked_plain and not bblocked) or (bform and not pform):
        print("   >>> BRIGHT WINS")
