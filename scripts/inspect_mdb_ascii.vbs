Option Explicit
Dim mdbPath, conn, rs
mdbPath = "D:\tmp\Baby78_NEW.mdb"

Set conn = CreateObject("ADODB.Connection")
On Error Resume Next
conn.Open "Provider=Microsoft.Jet.OLEDB.4.0;Data Source=" & mdbPath & ";Jet OLEDB:Database Password=4949;"
If Err.Number <> 0 Then
    WScript.Echo "Jet failed: " & Err.Description
    Err.Clear
    conn.Open "Provider=Microsoft.ACE.OLEDB.12.0;Data Source=" & mdbPath & ";Jet OLEDB:Database Password=4949;"
End If
If Err.Number <> 0 Then
    WScript.Echo "ACE12 failed: " & Err.Description
    Err.Clear
    conn.Open "Provider=Microsoft.ACE.OLEDB.16.0;Data Source=" & mdbPath & ";Jet OLEDB:Database Password=4949;"
End If
If Err.Number <> 0 Then
    WScript.Echo "ACE16 failed: " & Err.Description
    WScript.Quit 1
End If
On Error GoTo 0

WScript.Echo "Connected OK"

Set rs = conn.Execute("SELECT COUNT(*) AS n FROM zzapp")
WScript.Echo "Total zzapp: " & rs("n")
rs.Close

WScript.Echo ""
WScript.Echo "=== By year (datestart) ==="
Set rs = conn.Execute("SELECT YEAR(datestart) AS y, COUNT(*) AS n FROM zzapp WHERE datestart IS NOT NULL GROUP BY YEAR(datestart) ORDER BY YEAR(datestart) DESC")
Dim grandTotal: grandTotal = 0
Dim recent3: recent3 = 0
Do While Not rs.EOF
    Dim y, n
    y = rs("y"): n = rs("n")
    WScript.Echo "  " & y & " (พ.ศ." & (y - 1957) & "): " & n
    grandTotal = grandTotal + n
    If y >= 2024 Then recent3 = recent3 + n
    rs.MoveNext
Loop
rs.Close
WScript.Echo "  GRAND TOTAL: " & grandTotal
WScript.Echo "  ปี 67-69 (2024-2026): " & recent3

WScript.Echo ""
WScript.Echo "=== By policytype ==="
Set rs = conn.Execute("SELECT policytype, COUNT(*) AS n FROM zzapp GROUP BY policytype ORDER BY COUNT(*) DESC")
Do While Not rs.EOF
    WScript.Echo "  " & rs("policytype") & ": " & rs("n")
    rs.MoveNext
Loop
rs.Close

WScript.Echo ""
Set rs = conn.Execute("SELECT COUNT(DISTINCT license) AS n FROM zzapp WHERE license IS NOT NULL AND license <> ''")
WScript.Echo "Unique plates (ทุกปี): " & rs("n")
rs.Close

Set rs = conn.Execute("SELECT COUNT(DISTINCT license) AS n FROM zzapp WHERE license IS NOT NULL AND license <> '' AND YEAR(datestart) >= 2024")
WScript.Echo "Unique plates (active ปี67-69): " & rs("n")
rs.Close

Set rs = conn.Execute("SELECT COUNT(DISTINCT namethai) AS n FROM zzapp WHERE namethai IS NOT NULL AND namethai <> ''")
WScript.Echo "Unique customer names: " & rs("n")
rs.Close

conn.Close
