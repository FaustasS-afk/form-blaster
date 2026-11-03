"""
Find the right Bright Data request shape for captcha-protected pages.

The plain call returns x-brd-error: 'captcha or protection page found' with an
empty body. This tries the documented variations to see which one returns HTML.
"""
import httpx, json

KEY = ""
with open(r"C:\Users\SellJ\Documents\deepseek-harness\default-workspace\.secrets\env.txt",
          encoding="utf-8", errors="ignore") as f:
    for line in f:
        if line.strip().startswith("BRIGHTDATA_API_KEY"):
            KEY = line.partition("=")[2].strip().strip('"').strip("'").rstrip("]").strip()

URL = "https://www.antiques-atlas.com/contact"
H = {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"}

VARIANTS = [
    ("baseline raw",        {"zone": "web_unlocker1", "url": URL, "format": "raw"}),
    ("+ country us",        {"zone": "web_unlocker1", "url": URL, "format": "raw", "country": "us"}),
    ("+ render true",       {"zone": "web_unlocker1", "url": URL, "format": "raw", "render": True}),
    ("format=json",         {"zone": "web_unlocker1", "url": URL}),
    ("+ data_format html",  {"zone": "web_unlocker1", "url": URL, "format": "raw", "data_format": "html"}),
    ("+ brd_retry",         {"zone": "web_unlocker1", "url": URL, "format": "raw", "brd_retry": "1"}),
]

for name, payload in VARIANTS:
    try:
        r = httpx.post("https://api.brightdata.com/request", headers=H,
                       json=payload, timeout=90)
        err = r.headers.get("x-brd-error", "")
        print(f"{name:<22} status={r.status_code:<4} len={len(r.text):>7} "
              f"{'ERR: ' + err[:46] if err else 'OK'}")
        if r.status_code == 200 and len(r.text) > 5000 and not err:
            open(f"bd_ok_{name.replace(' ','_').replace('+','p')}.html", "w",
                 encoding="utf-8").write(r.text)
            print(f"    -> saved, looks like real HTML")
    except Exception as e:
        print(f"{name:<22} EXCEPTION {type(e).__name__}: {str(e)[:50]}")

print()
print("=== also retry the same url a few times on baseline (transient?) ===")
for i in range(3):
    r = httpx.post("https://api.brightdata.com/request", headers=H,
                   json={"zone": "web_unlocker1", "url": URL, "format": "raw"},
                   timeout=90)
    print(f"  try {i+1}: status={r.status_code} len={len(r.text)} "
          f"err={r.headers.get('x-brd-error','')[:40]}")
