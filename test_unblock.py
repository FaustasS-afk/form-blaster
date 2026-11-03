"""
Does Bright Data Web Unlocker beat the sites that blocked us?

Tests it against the EXACT urls that failed with 403 / WAF / hCaptcha in the
real run, comparing plain httpx vs Bright Data on the same url.
"""
import os
import sys
import time

sys.path.insert(0, r"C:\Users\SellJ\Desktop\FORM BLASTER")
os.chdir(r"C:\Users\SellJ\Desktop\FORM BLASTER")

import httpx
from bs4 import BeautifulSoup

# load key
ENV = r"C:\Users\SellJ\Documents\deepseek-harness\default-workspace\.secrets\env.txt"
KEY = ""
with open(ENV, encoding="utf-8", errors="ignore") as f:
    for line in f:
        if line.strip().startswith("BRIGHTDATA_API_KEY"):
            KEY = line.partition("=")[2].strip().strip('"').strip("'").rstrip("]").strip()
ZONE = "web_unlocker1"

from core import db, formdetect

cfg = db.load_config()

# urls that FAILED in the real run
TARGETS = [
    "http://1404collective.com/",
    "http://2ndchanceoc.wixsite.com/secondchance",
    "https://www.antiques-jewels.net/",
    "http://Cedarcreeksettlement.com/",
    "http://www.applebasketstore.com/",
    "http://1984vintage.com/",
    "http://2canda.com/",
    "http://3-bitgames.com/",
]

plain = httpx.Client(follow_redirects=True, timeout=25,
                     headers={"User-Agent": cfg["user_agent"]})


def via_bright(url):
    t0 = time.time()
    try:
        r = httpx.post(
            "https://api.brightdata.com/request",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {KEY}"},
            json={"zone": ZONE, "url": url, "format": "raw"},
            timeout=90,
        )
    except Exception as e:
        return None, 0, f"{type(e).__name__}: {e}"[:50], time.time() - t0
    return r.status_code, len(r.text), r.text, time.time() - t0


print("=" * 74)
print("  PLAIN httpx  vs  BRIGHT DATA WEB UNLOCKER")
print("=" * 74)
print()

better = 0
for u in TARGETS:
    # plain
    try:
        p = plain.get(u)
        pcode, plen = p.status_code, len(p.text)
        phtml = p.text
    except Exception as e:
        pcode, plen, phtml = 0, 0, ""

    # bright
    bcode, blen, bhtml, bt = via_bright(u)

    print(u[:64])
    print(f"   plain  : {pcode}  {plen:>7} bytes")
    if bcode is None:
        print(f"   bright : ERR {bhtml}")
    else:
        # did bright actually get a usable form?
        got_form = False
        if bhtml and isinstance(bhtml, str):
            try:
                s = BeautifulSoup(bhtml, "lxml")
                got_form = formdetect.find_best_form(s, u, cfg) is not None
            except Exception:
                pass
        print(f"   bright : {bcode}  {blen:>7} bytes  {bt:.1f}s  form={got_form}")
        if bcode == 200 and (pcode != 200 or blen > plen * 1.5):
            better += 1
    print()

print("=" * 74)
print(f"  bright improved: {better}/{len(TARGETS)}")
print("=" * 74)
