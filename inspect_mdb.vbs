Option Explicit
' inspect_mdb.vbs <mdb_path>
' List tables, columns of zzapp, and count of records by year

Dim conn, rs, mdbPath, mdbPwd, args
Set args = WScript.Arguments
If args.Count < 1 Then
    WScript.Echo "USAGE: cscript inspect_mdb.vbs <mdb_path>"
    WScript.Quit 1
End If
mdbPath = args(0)
mdbPwd  = "4949"

Set conn = CreateObject("ADODB.Connection")
On Error Resume Next
conn.Open "Provider=Microsoft.ACE.OLEDB.16.0;Data Source=" & mdbPath & ";Jet OLEDB:Database Password=" & mdbPwd & ";"
If Err.Number <> 0 Then
    Err.Clear
    conn.Open "Provider=Microsoft.ACE.OLEDB.12.0;Data Source=" & mdbPath & ";Jet OLEDB:Database Password=" & mdbPwd & ";"
End If
If Err.Number <> 0 Then
    Err.Clear
    conn.Open "Provider=Microsoft.Jet.OLEDB.4.0;Data Source=" & mdbPath & ";Jet OLEDB:Database Password=" & mdbPwd & ";"
End If
If Err.Number <> 0 Then
    WScript.Echo "ERROR: " & Err.Description
    WScript.Quit 1
End If
On Error GoTo 0

' === zzapp columns ===
WScript.Echo "=== zzapp COLUMNS ==="
Set rs = conn.Execute("SELECT TOP 1 * FROM zzapp")
Dim c
For c = 0 To rs.Fields.Count - 1
    WScript.Echo "  " & rs.Fields(c).Name & "  [" & rs.Fields(c).Type & "]"
Next
rs.Close

' === counts ===
WScript.Echo ""
WScript.Echo "=== COUNTS ==="
Set rs = conn.Execute("SELECT COUNT(*) FROM zzapp")
WScript.Echo "Total zzapp           : " & rs.Fields(0).Value
rs.Close

Set rs = conn.Execute("SELECT COUNT(*) FROM zzapp WHERE policy IS NOT NULL")
WScript.Echo "With policy_number    : " & rs.Fields(0).Value
rs.Close

Set rs = conn.Execute("SELECT COUNT(*) FROM zzapp WHERE datestart >= #2024-01-01#")
WScript.Echo "datestart >= 2024     : " & rs.Fields(0).Value
rs.Close

Set rs = conn.Execute("SELECT COUNT(*) FROM zzapp WHERE datestart >= #2023-01-01#")
WScript.Echo "datestart >= 2023     : " & rs.Fields(0).Value
rs.Close

Set rs = conn.Execute("SELECT COUNT(*) FROM zzapp WHERE datestart >= #2024-01-01# AND policy IS NOT NULL")
WScript.Echo "2024+ with policy     : " & rs.Fields(0).Value
rs.Close

' === year breakdown ===
WScript.Echo ""
WScript.Echo "=== YEAR BREAKDOWN (by datestart) ==="
Set rs = conn.Execute("SELECT Year(datestart) AS yr, Count(*) AS cnt FROM zzapp WHERE datestart >= #2020-01-01# GROUP BY Year(datestart) ORDER BY Year(datestart)")
Do While Not rs.EOF
    WScript.Echo "  " & rs.Fields("yr").Value & "  :  " & rs.Fields("cnt").Value
    rs.MoveNext
Loop
rs.Close

' === policy_type breakdown for 2024+ ===
WScript.Echo ""
WScript.Echo "=== policy_type for 2024+ ==="
Set rs = conn.Execute("SELECT policytype, Count(*) AS cnt FROM zzapp WHERE datestart >= #2024-01-01# GROUP BY policytype ORDER BY Count(*) DESC")
Do While Not rs.EOF
    WScript.Echo "  " & rs.Fields("policytype").Value & "  :  " & rs.Fields("cnt").Value
    rs.MoveNext
Loop
rs.Close

conn.Close
WScript.Echo ""
WScript.Echo "DONE"
