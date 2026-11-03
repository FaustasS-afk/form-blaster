"""
Show EXACTLY what gets typed into a real contact form.

Builds a realistic form, runs the real field-mapper and spinner against it,
then prints the literal field names and values that would be POSTed.
"""
import sys, os
sys.path.insert(0, r"C:\Users\SellJ\Desktop\FORM BLASTER")
os.chdir(r"C:\Users\SellJ\Desktop\FORM BLASTER")

from core import db, spin, fieldmap
from bs4 import BeautifulSoup

cfg = db.load_config()

# --- a realistic small-business contact form -------------------------------
HTML = """
<html><body>
<form action="/contact/submit" method="post">
  <input type="hidden" name="form_id" value="abc123">
  <input type="hidden" name="_csrf" value="tok_9f8e7d">
  <label for="fullname">Your Name*</label>
  <input type="text" name="fullname" id="fullname" required placeholder="Jane Smith">
  <label for="email">Email Address*</label>
  <input type="email" name="email" id="email" required placeholder="you@company.com">
  <label for="phone">Phone</label>
  <input type="tel" name="phone" id="phone">
  <label for="company">Business Name</label>
  <input type="text" name="company" id="company">
  <label for="subject">Subject</label>
  <select name="subject" id="subject">
    <option>-- Please choose --</option>
    <option>General enquiry</option>
    <option>Sales</option>
  </select>
  <label for="message">Message*</label>
  <textarea name="message" id="message" required></textarea>
  <button type="submit">Send</button>
</form>
</body></html>
"""

soup = BeautifulSoup(HTML, "lxml")
form = soup.find("form")
inputs = [dict(
    tag=el.name,
    type=(el.get("type") or "").lower() or ("textarea" if el.name == "textarea" else "text"),
    name=el.get("name") or "",
    id=el.get("id") or "",
    placeholder=el.get("placeholder") or "",
    aria=el.get("aria-label") or "",
    label=(soup.find("label", attrs={"for": el.get("id")}).get_text(strip=True)
           if el.get("id") and soup.find("label", attrs={"for": el.get("id")}) else ""),
    required=el.has_attr("required"),
    options=[o.get_text(strip=True) for o in el.find_all("option")],
    value=el.get("value") or "",
) for el in form.find_all(["input", "textarea", "select"])]

site_cfg = spin.spin_fields(cfg)          # what a real run does per site
mapping, unmapped = fieldmap.map_fields(inputs, site_cfg)

print("=" * 66)
print("  WHAT ACTUALLY GETS TYPED INTO A FORM")
print("=" * 66)
print()
print("  FORM ACTION : /contact/submit  (POST)")
print()

# hidden fields carried through
print("  --- hidden / carried through ---")
for el in form.find_all("input"):
    if (el.get("type") or "").lower() == "hidden":
        print(f"      {el.get('name'):<14} = {el.get('value')}")
print()

print("  --- fields WE fill ---")
for idx, val in sorted(mapping.items()):
    inp = inputs[idx]
    kind = fieldmap.classify(inp["name"] + inp["id"] + inp["label"]) or "?"
    label = inp["label"] or inp["name"]
    val_disp = val.replace("\n", "\\n")
    if len(val_disp) > 54:
        print(f"      {label:<22} [{kind}] = {val_disp[:54]}...")
    else:
        print(f"      {label:<22} [{kind}] = {val_disp}")

print()
print("  --- fields we DON'T fill ---")
for idx, inp in enumerate(inputs):
    if idx in mapping:
        continue
    if (inp.get("type") or "").lower() in ("hidden", "submit", "button"):
        continue
    why = "no matching value in config" if not inp["name"] else "unmapped"
    print(f"      {inp['label'] or inp['name']:<22} -> {why}")

print()
print("  unmatched REQUIRED fields:", unmapped or "none")

print()
print("=" * 66)
print("  THE FULL MESSAGE AS POSTED (real newlines)")
print("=" * 66)
for idx, val in sorted(mapping.items()):
    if "\n" in val:
        print()
        for line in val.split("\n"):
            print("    " + line)
        print()
