"""Test the Bright Data Web Unlocker with the user's API key."""
import sys, os, json
sys.path.insert(0, r"C:\Users\SellJ\Desktop\FORM BLASTER")
os.chdir(r"C:\Users\SellJ\Desktop\FORM BLASTER")

# load .env manually if present (dotenv may not be installed)
ENV_CANDIDATES = [
    ".env",
    "config/.env",
    r"C:\Users\SellJ\Documents\deepseek-harness\default-workspace\.secrets\env.txt",
    os.path.join(os.path.expanduser("~"), ".env"),
]
found = None
for c in ENV_CANDIDATES:
    if os.path.exists(c):
        found = c
        break

print("env file found:", found or "NONE")
print("env vars already set:",
      {k: v[:10] + "..." for k, v in os.environ.items()
       if "BRIGHT" in k.upper() or "BRD" in k.upper() or "UNLOCK" in k.upper()})

if found:
    with open(found, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            # the file has a stray ']' from a curl comment glued on
            v = v.rstrip("]").strip()
            if k and v and " " not in k:
                os.environ.setdefault(k, v)
    print("keys loaded:",
          {k: v[:10] + "..." for k, v in os.environ.items()
           if "BRIGHT" in k.upper() or "BRD" in k.upper()})

KEY = (os.environ.get("BRIGHTDATA_API_KEY") or os.environ.get("BRIGHT_DATA_API_KEY")
       or os.environ.get("BRD_API_KEY") or os.environ.get("BRIGHTDATA_TOKEN")
       or os.environ.get("BRIGHT_DATA_KEY") or "")
ZONE = (os.environ.get("BRIGHTDATA_ZONE") or os.environ.get("BRD_ZONE")
        or os.environ.get("BRIGHTDATA_UNLOCKER_ZONE") or "web_unlocker1")

print()
print("KEY  :", (KEY[:14] + "..." + KEY[-4:]) if KEY else "MISSING")
print("ZONE :", ZONE)
print()

if not KEY:
    print("!! no key found. Set BRIGHTDATA_API_KEY in a .env next to run.py")
    sys.exit(0)

import httpx
r = httpx.post(
    "https://api.brightdata.com/request",
    headers={"Content-Type": "application/json",
             "Authorization": f"Bearer {KEY}"},
    json={"zone": ZONE,
          "url": "https://geo.brdtest.com/welcome.txt?product=unlocker&method=api",
          "format": "raw"},
    timeout=60,
)
print("status:", r.status_code)
print("body  :", r.text[:300])
