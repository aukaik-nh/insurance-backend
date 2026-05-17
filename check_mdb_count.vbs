Option Explicit

Dim conn, rs, mdbPath, mdbPwd
Dim args, i
Set args = WScript.Arguments

If args.Count < 1 Then
    WScript.Echo "USAGE: cscript check_mdb_count.vbs <mdb_path>"
    WScript.Quit 1
End If

mdbPath = args(0)
mdbPwd  = "4949"

Set conn = CreateObject("ADODB.Connection")
On Error Resume Next
conn.Open "Provider=Microsoft.Jet.OLEDB.4.0;Data Source=" & mdbPath & ";Jet OLEDB:Database Password=" & mdbPwd & ";"
If Err.Number <> 0 Then
    Err.Clear
    conn.Open "Provider=Microsoft.ACE.OLEDB.12.0;Data Source=" & mdbPath & ";Jet OLEDB:Database Password=" & mdbPwd & ";"
End If
If Err.Number <> 0 Then
    Err.Clear
    conn.Open "Provider=Microsoft.ACE.OLEDB.16.0;Data Source=" & mdbPath & ";Jet OLEDB:Database Password=" & mdbPwd & ";"
End If
If Err.Number <> 0 Then
    WScript.Echo "ERROR: " & Err.Description
    WScript.Quit 1
End If
On Error GoTo 0

Set rs = conn.Execute("SELECT COUNT(*) FROM zzapp")
WScript.Echo mdbPath & " : " & rs.Fields(0).Value & " rows"
rs.Close
conn.Close
