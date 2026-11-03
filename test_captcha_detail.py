"""
Does submitting through Bright Data actually get past hCaptcha?

Detection already works on these. The question is whether the POST succeeds.
Bright Data only supports GET via the /request endpoint, so a POST-submitting
flow needs either:
  (a) the form to accept GET, or
  (b) a session/cookie handoff

This tests whether the page Bright Data returns differs from plain in a way
that matters for submission.
"""
import sys, os, re, time
sys.path.insert(0, r"C:\Users\SellJ\Desktop\FORM BLASTER")
os.chdir(r"C:\Users\SellJ\Desktop\FORM BLASTER")

import httpx
from bs4 import BeautifulSoup
from core import db, formdetect, fieldmap, submitter_http

KEY = ""
with open(r"C:\Users\SellJ\Documents\deepseek-harness\default-workspace\.secrets\env.txt",
          encoding="utf-8", errors="ignore") as f:
    for line in f:
        if line.strip().startswith("BRIGHTDATA_API_KEY"):
            KEY = line.partition("=")[2].strip().strip('"').strip("'").rstrip("]").strip()

cfg = db.load_config()
plain = httpx.Client(follow_redirects=True, timeout=25,
                     headers={"User-Agent": cfg["user_agent"]})

TARGETS = ["http://1984vintage.com/", "http://4thstreetmercantile.com/",
           "http://2ndchanceotr.com/"]

print("=== what does the hCaptcha actually look like on these pages? ===")
for u in TARGETS:
    try:
        r = plain.get(u)
        html = r.text
    except Exception as e:
        print(u, "ERR", e); continue
    s = BeautifulSoup(html, "lxml")
    found = formdetect.find_best_form(s, u, cfg)
    print()
    print(u)
    print("   final url:", r.url)
    if not found:
        print("   no form"); continue
    form, inputs, score = found
    print("   form action:", form.get("action"), "| method:", form.get("method"))
    print("   fields:", [i["name"] or i["id"] for i in inputs if i["name"] or i["id"]])
    # captcha indicators
    for pat in ("hcaptcha", "g-recaptcha", "cf-turnstile", "sitekey", "data-sitekey"):
        if re.search(pat, html, re.I):
            m = re.search(r'(data-sitekey|data-site-key)\s*=\s*["\']([^"\']+)', html, re.I)
            print(f"   contains: {pat}", ("sitekey=" + m.group(2)[:24]) if m and "sitekey" in pat else "")
    # is the captcha IN the form or a separate widget?
    for el in s.find_all(attrs={"data-sitekey": True}):
        print("   widget:", el.name, el.get("class"), "sitekey:", el.get("data-sitekey")[:26])
    # hidden token fields we'd need to fill
    hidden = [i["name"] for i in inputs
              if (i.get("type") or "").lower() == "hidden" and i.get("name")]
    print("   hidden fields:", hidden[:8])
