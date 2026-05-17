"""Dry-run บนไฟล์สุ่ม 300 ไฟล์ — ดู match rate แยกประเภท"""
import sys, os, random
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\insurance-backend")

from match_and_upload import parse_filename, find_matches, load_all_policies, get_sb

PDF_FOLDER = r"C:\Users\Administrator\Desktop\New folder\BabyPreechar"

random.seed(42)
all_files = [f for f in os.listdir(PDF_FOLDER) if f.lower().endswith(".pdf")]
sample = random.sample(all_files, 300)

sb = get_sb()
policies = load_all_policies(sb)
print(f"Loaded {len(policies)} policies\n")

kind_count = {"plate":0,"address":0,"name":0,"unknown":0}
match_count = {"plate":0,"address":0,"name":0,"unknown":0}
match_record_total = {"plate":0,"address":0,"name":0,"unknown":0}
unmatched_examples = {"plate":[],"address":[],"name":[],"unknown":[]}
over_match = []  # too many matches (>20)

for f in sample:
    p = parse_filename(f)
    kind_count[p["kind"]] += 1
    matches = find_matches(p, policies)
    n = len(matches)
    if n > 0:
        match_count[p["kind"]] += 1
        match_record_total[p["kind"]] += n
    if n > 20:
        over_match.append((f, p["kind"], p["key"], n))
    if n == 0 and p["kind"] != "unknown":
        if len(unmatched_examples[p["kind"]]) < 8:
            unmatched_examples[p["kind"]].append((f, p["key"]))

print("="*70)
print(f"{'kind':10s} {'files':>6s} {'matched':>8s} {'rate':>6s} {'records_linked':>15s}")
print("="*70)
for k in ["plate","address","name","unknown"]:
    if kind_count[k]:
        c, m, r = kind_count[k], match_count[k], match_record_total[k]
        print(f"{k:10s} {c:>6d} {m:>8d} {m/c*100:5.1f}% {r:>15d}")
print(f"{'TOTAL':10s} {sum(kind_count.values()):>6d} {sum(match_count.values()):>8d}")
print(f"  match rate: {sum(match_count.values())/300*100:.1f}%")
print(f"  records linked: {sum(match_record_total.values())}")

print(f"\nOver-match (>20 records, may be too generic):")
for f, k, key, n in over_match[:10]:
    print(f"  [{k}] {f[:50]:50s} key={key!r} → {n} records")

print(f"\nUnmatched examples per kind:")
for k, exs in unmatched_examples.items():
    if exs:
        print(f"\n  --- {k} ---")
        for f, key in exs:
            print(f"    {f[:50]:50s} key={key!r}")
