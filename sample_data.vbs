Option Explicit

Dim conn, rs, mdbPath, mdbPwd
mdbPath = "C:\Users\Administrator\Desktop\New folder\Baby78\Baby78_Safety.mdb"
mdbPwd  = "4949"

Set conn = CreateObject("ADODB.Connection")
conn.Open "Provider=Microsoft.Jet.OLEDB.4.0;Data Source=" & mdbPath & ";Jet OLEDB:Database Password=" & mdbPwd & ";"

Set rs = CreateObject("ADODB.Recordset")
rs.Open "SELECT app, policy, namethai, namethai1, address1, address2, address1d, address2d, postcode, license, datestart, dateend FROM zzapp WHERE namethai LIKE '%สุจิตต์%' OR address1 LIKE '%481/175%' ORDER BY app", conn

Dim i, c
i = 0
Do While Not rs.EOF
    i = i + 1
    WScript.Echo ""
    WScript.Echo "--- Record " & i & " ---"
    For c = 0 To rs.Fields.Count - 1
        If Not IsNull(rs.Fields(c).Value) Then
            WScript.Echo "  " & rs.Fields(c).Name & " = " & rs.Fields(c).Value
        End If
    Next
    rs.MoveNext
Loop

WScript.Echo ""
WScript.Echo "Total: " & i

rs.Close
conn.Close
