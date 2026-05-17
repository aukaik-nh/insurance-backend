"""ทดสอบ plate match บนไฟล์ที่ขึ้นต้นด้วยตัวอักษรไทย"""
import sys, os, re
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\insurance-backend")

from match_and_upload import parse_filename, find_matches, load_all_policies, get_sb

PDF_FOLDER = r"C:\Users\Administrator\Desktop\New folder\BabyPreechar"

THAI = "กขฃคฅฆงจฉชซฌญฎฏฐฑฒณดตถทธนบปผฝพฟภมยรลวศษสหฬอฮ"

files = [f for f in os.listdir(PDF_FOLDER) if f.lower().endswith(".pdf")]
# กรองที่ขึ้นต้นด้วยตัวอักษรไทย หรือ "0-9 + Thai letter" (เช่น 1ฒน1479)
plate_like = [f for f in files if re.match(rf"^[\d]?[{THAI}]", f)][:50]

print(f"Sample plate-like files: {len(plate_like)}\n")

sb = get_sb()
policies = load_all_policies(sb)
print(f"Loaded {len(policies)} policies\n")

kind_count = {"plate":0,"address":0,"name":0,"unknown":0}
match_count = {"plate":0,"address":0,"name":0,"unknown":0}

for f in plate_like:
    p = parse_filename(f)
    kind_count[p["kind"]] += 1
    matches = find_matches(p, policies)
    if matches: match_count[p["kind"]] += 1
    yr = f"y{p['year_be']}" if p['year_be'] else "y?  "
    print(f"  {f[:45]:45s} → {p['kind']:7s} {yr} key={p['key'][:20]:20s} match={len(matches)}")

print("\nSummary:")
for k in kind_count:
    if kind_count[k]:
        print(f"  {k:8s}: {kind_count[k]:3d} files, {match_count[k]:3d} matched ({match_count[k]/kind_count[k]*100:.0f}%)")
