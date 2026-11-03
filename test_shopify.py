"""
Shopify contact forms: are we missing fields?

Shopify uses contact[Name] / contact[Comment] style names. If the mapper
cannot see them, the message is dropped and only an email is sent.
"""
import sys, os
sys.path.insert(0, r"C:\Users\SellJ\Desktop\FORM BLASTER")
os.chdir(r"C:\Users\SellJ\Desktop\FORM BLASTER")

import httpx
from bs4 import BeautifulSoup
from core import db, formdetect, fieldmap

cfg = db.load_config()
plain = httpx.Client(follow_redirects=True, timeout=25,
                     headers={"User-Agent": cfg["user_agent"]})

for u in ["https://1984vintage.com/contact", "https://2ndchanceotr.com/",
          "https://4thstreetmercantile.com/"]:
    print("=" * 70)
    print(u)
    try:
        r = plain.get(u)
    except Exception as e:
        print("  ERR", e); continue
    s = BeautifulSoup(r.text, "lxml")
    forms = s.find_all("form")
    print("  forms on page:", len(forms))
    for fi, f in enumerate(forms):
        ins = f.find_all(["input", "textarea", "select"])
        if not ins:
            continue
        print(f"  --- form {fi}: action={f.get('action')} ---")
        for el in ins:
            t = (el.get("type") or "").lower()
            nm = el.get("name") or ""
            if not nm:
                continue
            vis = "HIDDEN" if t == "hidden" else t or el.name
            print(f"      {el.name:<9} type={vis:<8} name={nm[:44]:<44} "
                  f"id={(el.get('id') or '')[:24]}")
        # what does the mapper see?
        inputs = formdetect._inputs_from_form(f, s)
        m, un = fieldmap.map_fields(inputs, cfg)
        print("      >> mapper maps:", {fieldmap.classify(
            (inputs[i]['name']+' '+inputs[i]['id']+' '+inputs[i]['label'])): inputs[i]['name'][:28]
            for i in m})
        print("      >> unmapped required:", un)
    print()
