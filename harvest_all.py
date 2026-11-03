"""Full 50-state harvest. Resumable: re-running continues from where it stopped."""
import sys, os, json, time, re
sys.path.insert(0, r"C:\Users\SellJ\Desktop\FORM BLASTER")
os.chdir(r"C:\Users\SellJ\Desktop\FORM BLASTER")

from scrape_leads import crawl, STATES, clean_site

STATE_FILE = "data/_states_done.json"
LEAD_FILE = "leads.txt"
CSV_FILE = "leads_all.csv"

done = set()
if os.path.exists(STATE_FILE):
    done = set(json.load(open(STATE_FILE)))
    print(f"resuming — {len(done)} state(s) already done")

all_found = {}
if os.path.exists(CSV_FILE):
    import csv
    with open(CSV_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            all_found[row["domain"]] = row
    print(f"loaded {len(all_found)} existing leads")

todo = [s for s in STATES if s not in done]
print(f"{len(todo)} state(s) to scrape\n")

for st in todo:
    try:
        got = crawl([st], None, 6)
    except Exception as e:
        print(f"  !! {st} failed: {type(e).__name__}: {e}")
        continue
    for dom, r in got.items():
        if dom not in all_found:
            all_found[dom] = {
                "domain": dom, "site": r["site"], "name": r.get("name") or "",
                "phone": r.get("phone") or "", "category": r.get("category") or "",
                "source": r.get("source") or "",
            }
    done.add(st)
    json.dump(sorted(done), open(STATE_FILE, "w"))
    print(f"  [{st}] +{len(got)}  => {len(all_found)} total\n", flush=True)

    # checkpoint the files every state so a crash loses nothing
    import csv
    with open(LEAD_FILE, "w", encoding="utf-8") as f:
        for d in sorted(all_found):
            f.write(all_found[d]["site"] + "\n")
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["domain", "site", "name", "phone",
                                          "category", "source"])
        w.writeheader()
        for d in sorted(all_found):
            w.writerow(all_found[d])

print(f"ALL DONE — {len(all_found)} unique business websites in {LEAD_FILE}")
