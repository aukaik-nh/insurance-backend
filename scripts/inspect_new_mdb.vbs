' inspect_new_mdb.vbs — สแกน MDB ใหม่ดูจำนวน, ปี, distribution
Option Explicit

Dim mdbPath, pwd, conn, rs
mdbPath = "C:\Users\Administrator\Desktop\ใหม่10062026\BabyFolder\Baby78_Safety.mdb"
pwd = "4949"

Set conn = CreateObject("ADODB.Connection")
On Error Resume Next
conn.Open "Provider=Microsoft.Jet.OLEDB.4.0;Data Source=" & mdbPath & ";Jet OLEDB:Database Password=" & pwd & ";"
If Err.Number <> 0 Then
    Err.Clear
    conn.Open "Provider=Microsoft.ACE.OLEDB.12.0;Data Source=" & mdbPath & ";Jet OLEDB:Database Password=" & pwd & ";"
End If
If Err.Number <> 0 Then
    WScript.Echo "ERROR opening MDB: " & Err.Description
    WScript.Quit 1
End If
On Error GoTo 0

' Total records
Set rs = conn.Execute("SELECT COUNT(*) AS n FROM zzapp")
WScript.Echo "=== zzapp total: " & rs("n") & " rows ==="
rs.Close

' Date range
Set rs = conn.Execute("SELECT MIN(datestart) AS mn, MAX(datestart) AS mx, MIN(dateend) AS emn, MAX(dateend) AS emx FROM zzapp WHERE datestart IS NOT NULL")
WScript.Echo "datestart range: " & rs("mn") & " ถึง " & rs("mx")
WScript.Echo "dateend   range: " & rs("emn") & " ถึง " & rs("emx")
rs.Close

' Records by year (datestart year)
WScript.Echo ""
WScript.Echo "=== Records by datestart year ==="
Set rs = conn.Execute("SELECT YEAR(datestart) AS y, COUNT(*) AS n FROM zzapp WHERE datestart IS NOT NULL GROUP BY YEAR(datestart) ORDER BY YEAR(datestart) DESC")
Do While Not rs.EOF
    WScript.Echo "  " & rs("y") & ": " & rs("n")
    rs.MoveNext
Loop
rs.Close

' Last 3 years (พ.ศ. 67-69 = ค.ศ. 2024-2026)
WScript.Echo ""
WScript.Echo "=== Records ปี 2024-2026 (พ.ศ. 67-69) ==="
Set rs = conn.Execute("SELECT YEAR(datestart) AS y, COUNT(*) AS n FROM zzapp WHERE YEAR(datestart) >= 2024 GROUP BY YEAR(datestart)")
Dim totalRecent : totalRecent = 0
Do While Not rs.EOF
    WScript.Echo "  " & rs("y") & " (พ.ศ." & (rs("y") - 1957) & "): " & rs("n")
    totalRecent = totalRecent + rs("n")
    rs.MoveNext
Loop
WScript.Echo "  รวม: " & totalRecent
rs.Close

' By policy type
WScript.Echo ""
WScript.Echo "=== Records by policytype (ทุกปี) ==="
Set rs = conn.Execute("SELECT policytype, COUNT(*) AS n FROM zzapp GROUP BY policytype ORDER BY COUNT(*) DESC")
Do While Not rs.EOF
    WScript.Echo "  " & rs("policytype") & ": " & rs("n")
    rs.MoveNext
Loop
rs.Close

' Unique customers (license plates)
WScript.Echo ""
Set rs = conn.Execute("SELECT COUNT(DISTINCT license) AS n FROM zzapp WHERE license IS NOT NULL AND license <> ''")
WScript.Echo "=== Unique license plates (ทั้งหมด): " & rs("n")
rs.Close

Set rs = conn.Execute("SELECT COUNT(DISTINCT license) AS n FROM zzapp WHERE license IS NOT NULL AND license <> '' AND YEAR(datestart) >= 2024")
WScript.Echo "=== Unique license plates (active = ปี67-69): " & rs("n")
rs.Close

Set rs = conn.Execute("SELECT COUNT(DISTINCT namethai) AS n FROM zzapp WHERE namethai IS NOT NULL AND namethai <> ''")
WScript.Echo "=== Unique customer names (ทั้งหมด): " & rs("n")
rs.Close

conn.Close
