"""
Test Bright Data WITH THE CAPTCHA SOLVER ON.

Now that the zone has CAPTCHA Solver enabled, does it return pages that
plain httpx cannot reach?

Critical: Bright Data returns HTTP 200 with an EMPTY BODY on failure, so we
must check body length AND the x-brd-error header.
"""
import sys, os, time
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


def bd(url, timeout=120):
    t0 = time.time()
    try:
        r = httpx.post("https://api.brightdata.com/request",
                       headers={"Content-Type": "application/json",
                                "Authorization": f"Bearer {KEY}"},
                       json={"zone": ZONE, "url": url, "format": "raw"},
                       timeout=timeout)
        return r.status_code, r.text, r.headers.get("x-brd-error", ""), time.time() - t0
    except Exception as e:
        return None, "", f"{type(e).__name__}", time.time() - t0


# sites that were hCaptcha-blocked or WAF-blocked in real runs
TARGETS = [
    "http://1984vintage.com/",
    "http://2canda.com/",
    "http://3-bitgames.com/",
    "http://4thstreetmercantile.com/",
    "http://2ndchanceotr.com/",
    "https://www.antiques-atlas.com/contact",
    "https://www.platoscloset.com/locations/dover-de",
]

print("=" * 78)
print("  BRIGHT DATA  (captcha solver ON)   vs   PLAIN httpx")
print("=" * 78)

wins = 0
for u in TARGETS:
    try:
        p = plain.get(u)
        pcode, ptext = p.status_code, p.text
    except Exception:
        pcode, ptext = 0, ""
    pblock = submitter_http.detect_block(pcode, ptext)
    pform = False
    try:
        pform = formdetect.find_best_form(BeautifulSoup(ptext, "lxml"), u, cfg) is not None
    except Exception:
        pass

    bcode, btext, berr, bt = bd(u)
    bform = False
    if btext:
        try:
            bform = formdetect.find_best_form(BeautifulSoup(btext, "lxml"), u, cfg) is not None
        except Exception:
            pass

    print()
    print(u[:68])
    print(f"   plain  : {pcode:<4} {len(ptext):>7}b  block={str(pblock):<8} form={pform}")
    if bcode is None:
        print(f"   bright : ERR {berr}")
    else:
        print(f"   bright : {bcode:<4} {len(btext):>7}b  err={berr[:34]:<34} form={bform} ({bt:.0f}s)")

    if bform and not pform:
        print("   >>> BRIGHT WINS (got a form plain could not)")
        wins += 1
    elif pblock and not berr and btext:
        print("   >>> BRIGHT WINS (cleared a block)")
        wins += 1

print()
print("=" * 78)
print(f"  wins: {wins}/{len(TARGETS)}")
print("=" * 78)
