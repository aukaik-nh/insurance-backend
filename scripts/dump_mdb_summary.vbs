Option Explicit

Dim mdbPath, conn, rs, fso, out
mdbPath = "D:\tmp\Baby78_NEW.mdb"

Set conn = CreateObject("ADODB.Connection")
On Error Resume Next
conn.Open "Provider=Microsoft.Jet.OLEDB.4.0;Data Source=" & mdbPath & ";Jet OLEDB:Database Password=4949;"
If Err.Number <> 0 Then WScript.Quit 1
On Error GoTo 0

Set fso = CreateObject("Scripting.FileSystemObject")
' Stream UTF-8
Dim stm: Set stm = CreateObject("ADODB.Stream")
stm.Type = 2 ' text
stm.Charset = "utf-8"
stm.Open

' 1. Total
Set rs = conn.Execute("SELECT COUNT(*) AS n FROM zzapp")
stm.WriteText "TOTAL=" & rs("n") & vbLf
rs.Close

' 2. By year (datestart)
stm.WriteText "[BY_YEAR]" & vbLf
Set rs = conn.Execute("SELECT YEAR(datestart) AS y, COUNT(*) AS n FROM zzapp WHERE datestart IS NOT NULL GROUP BY YEAR(datestart) ORDER BY YEAR(datestart) DESC")
Do While Not rs.EOF
    stm.WriteText rs("y") & "," & rs("n") & vbLf
    rs.MoveNext
Loop
rs.Close

' 3. By policytype (all)
stm.WriteText "[BY_TYPE_ALL]" & vbLf
Set rs = conn.Execute("SELECT policytype, COUNT(*) AS n FROM zzapp GROUP BY policytype ORDER BY COUNT(*) DESC")
Do While Not rs.EOF
    stm.WriteText (rs("policytype") & "") & "," & rs("n") & vbLf
    rs.MoveNext
Loop
rs.Close

' 4. By policytype (active = ปี 2024-2026)
stm.WriteText "[BY_TYPE_ACTIVE]" & vbLf
Set rs = conn.Execute("SELECT policytype, COUNT(*) AS n FROM zzapp WHERE YEAR(datestart) >= 2024 AND YEAR(datestart) <= 2026 GROUP BY policytype ORDER BY COUNT(*) DESC")
Do While Not rs.EOF
    stm.WriteText (rs("policytype") & "") & "," & rs("n") & vbLf
    rs.MoveNext
Loop
rs.Close

' 5. Active plates (ทะเบียน) — มี record ปี 2024-2026
stm.WriteText "[ACTIVE_PLATES]" & vbLf
Set rs = conn.Execute("SELECT DISTINCT license FROM zzapp WHERE license IS NOT NULL AND license <> '' AND YEAR(datestart) >= 2024 AND YEAR(datestart) <= 2026")
Dim n_plate: n_plate = 0
Do While Not rs.EOF
    stm.WriteText rs("license") & vbLf
    n_plate = n_plate + 1
    rs.MoveNext
Loop
rs.Close
stm.WriteText "[ACTIVE_PLATES_COUNT]=" & n_plate & vbLf

' 6. Active names (ชื่อลูกค้า) — มี record ปี 2024-2026
stm.WriteText "[ACTIVE_NAMES]" & vbLf
Set rs = conn.Execute("SELECT DISTINCT namethai FROM zzapp WHERE namethai IS NOT NULL AND namethai <> '' AND YEAR(datestart) >= 2024 AND YEAR(datestart) <= 2026")
Dim n_name: n_name = 0
Do While Not rs.EOF
    stm.WriteText rs("namethai") & vbLf
    n_name = n_name + 1
    rs.MoveNext
Loop
rs.Close
stm.WriteText "[ACTIVE_NAMES_COUNT]=" & n_name & vbLf

' 7. Active addresses (ที่อยู่) — fire insurance ใช้ที่อยู่
stm.WriteText "[ACTIVE_ADDRESSES]" & vbLf
Set rs = conn.Execute("SELECT DISTINCT address1 FROM zzapp WHERE address1 IS NOT NULL AND address1 <> '' AND YEAR(datestart) >= 2024 AND YEAR(datestart) <= 2026 AND policytype IN ('FIRE','ASSET','IAR')")
Dim n_addr: n_addr = 0
Do While Not rs.EOF
    stm.WriteText rs("address1") & vbLf
    n_addr = n_addr + 1
    rs.MoveNext
Loop
rs.Close
stm.WriteText "[ACTIVE_ADDRESSES_COUNT]=" & n_addr & vbLf

conn.Close

' Save UTF-8 file
stm.Position = 0
stm.SaveToFile "D:\tmp\mdb_summary.txt", 2 ' overwrite
stm.Close
WScript.Echo "Dumped to D:\tmp\mdb_summary.txt"
