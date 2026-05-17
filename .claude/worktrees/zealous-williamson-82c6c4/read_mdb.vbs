Option Explicit

Dim conn, rs, fso, ts, mdbPath, outPath, i, line
Dim cols()

mdbPath = "C:\Users\Administrator\Desktop\Baby78_nopwd.mdb"
outPath = "D:\insurance-backend\zzapp_export.csv"

Set fso = CreateObject("Scripting.FileSystemObject")
Set ts = fso.CreateTextFile(outPath, True, True)  ' True = Unicode

Set conn = CreateObject("ADODB.Connection")
On Error Resume Next

' ลอง Jet 4.0 ก่อน
conn.Open "Provider=Microsoft.Jet.OLEDB.4.0;Data Source=" & mdbPath & ";"
If Err.Number <> 0 Then
    Err.Clear
    ' ลอง ACE 12.0
    conn.Open "Provider=Microsoft.ACE.OLEDB.12.0;Data Source=" & mdbPath & ";"
End If

If Err.Number <> 0 Then
    WScript.Echo "ERROR connecting: " & Err.Description
    WScript.Quit 1
End If
On Error GoTo 0

WScript.Echo "Connected! Exporting zzapp..."

Set rs = CreateObject("ADODB.Recordset")
rs.Open "SELECT * FROM zzapp ORDER BY app", conn

' Header row
Dim headers : headers = ""
For i = 0 To rs.Fields.Count - 1
    If i > 0 Then headers = headers & ","
    headers = headers & """" & rs.Fields(i).Name & """"
Next
ts.WriteLine headers

' Data rows
Dim rowCount : rowCount = 0
Do While Not rs.EOF
    line = ""
    For i = 0 To rs.Fields.Count - 1
        If i > 0 Then line = line & ","
        Dim v : v = rs.Fields(i).Value
        If IsNull(v) Then
            line = line & ""
        ElseIf VarType(v) = 8 Then  ' String
            v = Replace(CStr(v), """", """""")
            v = Replace(CStr(v), Chr(13), " ")
            v = Replace(CStr(v), Chr(10), " ")
            line = line & """" & v & """"
        ElseIf VarType(v) = 7 Then  ' Date
            line = line & """" & CStr(v) & """"
        Else
            line = line & CStr(v)
        End If
    Next
    ts.WriteLine line
    rowCount = rowCount + 1
    rs.MoveNext
Loop

rs.Close
conn.Close
ts.Close

WScript.Echo "Done! " & rowCount & " rows exported to: " & outPath
