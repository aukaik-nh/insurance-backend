"""merge_summary.py — รวมข้อมูล MDB (จาก vbs dump) + BabyScan folder + present สรุป"""
import sys, re
from pathlib import Path
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8")

MDB_DUMP = Path(r"D:\tmp\mdb_summary.txt")
SCAN_DIR = Path(r"C:\Users\Administrator\Desktop\ใหม่10062026\BabyScan")
ACTIVE_BE = {67, 68, 69}  # พ.ศ. 67-69

# ─── 1. Parse MDB dump ──────────────────────────────────────
sections = defaultdict(list)
current = None
total_mdb = 0
with MDB_DUMP.open(encoding="utf-8") as f:
    for line in f:
        line = line.rstrip("\n").rstrip("\r")
        if line.startswith("TOTAL="):
            total_mdb = int(line.split("=", 1)[1])
            continue
        if line.startswith("[") and "]=" in line:
            current = None
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            continue
        if current and line:
            sections[current].append(line)

# Year stats
by_year = []
for ln in sections.get("BY_YEAR", []):
    try:
        y, n = ln.split(",")
        by_year.append((int(y), int(n)))
    except: pass
by_year.sort(reverse=True)
valid_years = [(y,n) for y,n in by_year if 2000 <= y <= 2030]
bad_years = [(y,n) for y,n in by_year if not (2000 <= y <= 2030)]
active_n_mdb = sum(n for y,n in valid_years if y in (2024,2025,2026))

# Type stats
def parse_type(rows):
    out = []
    for ln in rows:
        parts = ln.split(",")
        if len(parts) >= 2:
            n = parts[-1]
            t = ",".join(parts[:-1])
            try: out.append((t.strip() or "(null)", int(n)))
            except: pass
    return out
type_all = parse_type(sections.get("BY_TYPE_ALL", []))
type_active = parse_type(sections.get("BY_TYPE_ACTIVE", []))

# Active identifiers
active_plates = {p.strip() for p in sections.get("ACTIVE_PLATES", []) if p.strip()}
active_names = {n.strip() for n in sections.get("ACTIVE_NAMES", []) if n.strip()}
active_addrs = {a.strip() for a in sections.get("ACTIVE_ADDRESSES", []) if a.strip()}

print("="*72)
print("  📊 สรุปข้อมูลใหม่ก่อน import")
print("="*72)
print(f"\n[MDB] Baby78_Safety.mdb (ใหม่)")
print(f"   Total: {total_mdb:,} rows")
print(f"\n   By year (ปี พ.ศ.):")
for y, n in valid_years[:13]:
    be = y - 1957
    mark = "  ← ACTIVE" if y in (2024,2025,2026) else ""
    print(f"     พ.ศ. {be} ({y}): {n:>5,}{mark}")
if bad_years:
    print(f"     [outlier ปีผิดปกติ: {len(bad_years)} buckets, {sum(n for _,n in bad_years)} rows]")
print(f"   → Active rows ปี 67-69: {active_n_mdb:,}")

print(f"\n   By policy type (ทั้งหมด):")
for t, n in type_all[:10]:
    print(f"     {t:<10}: {n:>6,}")

print(f"\n   By policy type (active ปี 67-69):")
for t, n in type_active[:10]:
    print(f"     {t:<10}: {n:>5,}")

print(f"\n   Active identifiers (มี record ปี 67-69):")
print(f"     unique plates:    {len(active_plates):,}")
print(f"     unique names:     {len(active_names):,}")
print(f"     unique addresses: {len(active_addrs):,} (fire/asset/IAR)")

# ─── 2. Scan BabyScan folder ─────────────────────────────────
print(f"\n[SCAN] BabyScan folder")
print(f"   path: {SCAN_DIR}")

if not SCAN_DIR.exists():
    print("   ❌ ไม่พบ folder")
    sys.exit(1)

files = [p for p in SCAN_DIR.iterdir() if p.is_file()]
pdfs = [p for p in files if p.suffix.lower() == ".pdf"]
imgs = [p for p in files if p.suffix.lower() in (".jpg",".jpeg",".png")]
xls = [p for p in files if p.suffix.lower() in (".xls",".xlsx")]
total_size = sum(p.stat().st_size for p in files)
print(f"   Total files: {len(files):,}")
print(f"     PDF: {len(pdfs):,}")
print(f"     JPG/PNG: {len(imgs):,}")
print(f"     XLS: {len(xls):,}")
print(f"   Total size: {total_size/1024/1024/1024:.2f} GB")

# Parse filename → year + plate/address
plate_re = re.compile(r'^(\d{1,2}[ก-ฮ]{1,2}\d{1,5})')  # ทะเบียน 1กก5226
year_in_name_re = re.compile(r'(?:กธ|พรบ|พรล|PA|ภ\.ค)\.?\s*(\d{2})(?:\D|$)')
year_alt_re = re.compile(r'\s(\d{2})(?:\.|\s|$|_|\))')

by_year_files = Counter()
plate_to_files = defaultdict(list)
addr_to_files = defaultdict(list)  # ไฟล์ที่ไม่ใช่ทะเบียน → assume address

for p in files:
    nm = p.stem
    # year
    yr = None
    m = year_in_name_re.search(nm)
    if not m: m = year_alt_re.search(nm)
    if m:
        try:
            yr = int(m.group(1))
            if 50 <= yr <= 75: by_year_files[yr] += 1
        except: pass
    # plate vs address
    pm = plate_re.match(nm)
    if pm:
        plate_to_files[pm.group(1)].append(p)
    else:
        # first ~30 chars as address key
        key = nm.split(" ")[0][:40]
        addr_to_files[key].append(p)

print(f"\n   Files by ปี:")
for yr in sorted(by_year_files.keys(), reverse=True)[:13]:
    mark = "  ← ACTIVE" if yr in ACTIVE_BE else ""
    print(f"     พ.ศ. {yr}: {by_year_files[yr]:>5,}{mark}")
print(f"   → Active files ปี 67-69: {sum(by_year_files[y] for y in ACTIVE_BE):,}")

print(f"\n   Unique IDs in filenames:")
print(f"     plates:    {len(plate_to_files):,}")
print(f"     non-plate keys (assume address/name): {len(addr_to_files):,}")

# ─── 3. Match active plates → files (ทุกปี) ──────────────────
def normalize_plate(s):
    return re.sub(r'\s+', '', s)

active_plate_keys_compact = {normalize_plate(p) for p in active_plates}
matched = 0
files_for_active = 0
size_for_active = 0
matched_plate_list = []
for plate_file, files_list in plate_to_files.items():
    # MDB plate: '1กก 8803 กท' → compact '1กก8803กท'
    # File plate: '1กก8803'
    for mp in active_plate_keys_compact:
        if plate_file in mp or mp.startswith(plate_file):
            matched += 1
            files_for_active += len(files_list)
            size_for_active += sum(f.stat().st_size for f in files_list)
            matched_plate_list.append(plate_file)
            break

print(f"\n[MATCH] Active customers → PDF files")
print(f"   Active plates ใน MDB:                 {len(active_plates):,}")
print(f"   ใน BabyScan filename จับคู่ได้:        {matched:,}")
print(f"   → ไฟล์ทั้งหมดของ active plates (ทุกปี): {files_for_active:,}")
print(f"   → ขนาด:                                 {size_for_active/1024/1024:.1f} MB")
print(f"\n   ตัวอย่าง matched plates (10 ชื่อแรก):")
for p in sorted(matched_plate_list)[:10]:
    print(f"     {p} ({len(plate_to_files[p])} files)")

# ─── 4. สรุปการ import ที่จะทำ ─────────────────────────────────
print(f"\n{'='*72}")
print(f"  💡 แผน Import ที่เสนอ (รอ user confirm)")
print(f"{'='*72}")
plan_rows = active_n_mdb  # เริ่มจาก active records
# + records เก่าของ active plates (จำลอง)
print(f"\n   1. ลบของเก่าใน Neon: 4,017 rows (insurance_policies)")
print(f"      ลบของเก่าใน R2:   16 PDFs")
print(f"\n   2. Import จาก MDB ใหม่:")
print(f"      เลือกเฉพาะ active customers ({len(active_plates):,} plates + {len(active_names):,} names + {len(active_addrs):,} addresses)")
print(f"      → คาดว่าจะ import: ~{plan_rows:,} – {total_mdb:,} rows")
print(f"      (ขึ้นกับว่า include เฉพาะปี 67-69 หรือทุกปีของ active customers)")
print(f"\n   3. Upload PDFs:")
print(f"      → ไฟล์ active plates: {files_for_active:,} ({size_for_active/1024/1024:.1f} MB)")
print(f"      → + active addresses (fire) — ยังต้องคำนวณแยก")
print(f"\n   ⚠️  ยังไม่ลบ/ยังไม่ import — รอ confirm จาก user")
