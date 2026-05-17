"""ตัวอย่าง: ดูข้อมูลในตาราง zzapp ของลูกค้าสุจิตต์ ปาริฉัตรกานนท์
เพื่อเข้าใจว่าฟิลด์ไหนใช้ match กับ PDF filename
"""
import win32com.client
mdb  = r"C:\Users\Administrator\Desktop\New folder\Baby78\Baby78_Safety.mdb"
pwd  = "4949"

conn = win32com.client.Dispatch("ADODB.Connection")
conn.Open(f"Provider=Microsoft.Jet.OLEDB.4.0;Data Source='{mdb}';Jet OLEDB:Database Password='{pwd}'")

# ดูทุก record ที่ address มี "481/175" หรือ "ปาริฉัตร"
rs = win32com.client.Dispatch("ADODB.Recordset")
rs.Open("""
SELECT app, policy, namethai, namethai1, address1, address2,
       address1d, address2d, postcode, license, datestart, dateend
FROM zzapp
WHERE address1 LIKE '%481/175%'
   OR address1 LIKE '%ปาริฉัตร%'
   OR namethai LIKE '%ปาริฉัตร%'
   OR namethai LIKE '%สุจิตต์%'
ORDER BY app
""", conn)

cols = [rs.Fields(i).Name for i in range(rs.Fields.Count)]
print("Columns:", cols)
print("=" * 80)

i = 0
while not rs.EOF:
    print(f"\n--- Record {i+1} ---")
    for c in cols:
        v = rs.Fields(c).Value
        if v is not None:
            print(f"  {c:20s} = {v!r}")
    i += 1
    rs.MoveNext()

print(f"\nTotal: {i} records")
rs.Close()
conn.Close()
