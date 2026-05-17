Option Explicit

Dim conn, rs, mdbPath, mdbPwd
mdbPath = "C:\Users\Administrator\Desktop\New folder\Baby78\Baby78_Safety.mdb"
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

' List all tables using ADOX
Dim cat, tbl
Set cat = CreateObject("ADOX.Catalog")
cat.ActiveConnection = conn

WScript.Echo "=== TABLES ==="
For Each tbl In cat.Tables
    If tbl.Type = "TABLE" Then
        WScript.Echo tbl.Name
    End If
Next

WScript.Echo ""
WScript.Echo "=== ALL COLUMNS PER TABLE ==="
For Each tbl In cat.Tables
    If tbl.Type = "TABLE" Then
        WScript.Echo ""
        WScript.Echo "[" & tbl.Name & "]"
        Dim col
        For Each col In tbl.Columns
            WScript.Echo "  - " & col.Name
        Next
    End If
Next

conn.Close
