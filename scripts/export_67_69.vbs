' export_67_69.vbs — Export zzapp rows ปี พ.ศ. 67-69 (AD 2024-2026) → CSV UTF-8
Option Explicit

Dim mdbPath, conn, rs, stm
mdbPath = "D:\tmp\Baby78_NEW.mdb"

Set conn = CreateObject("ADODB.Connection")
On Error Resume Next
conn.Open "Provider=Microsoft.Jet.OLEDB.4.0;Data Source=" & mdbPath & ";Jet OLEDB:Database Password=4949;"
If Err.Number <> 0 Then
    WScript.Echo "ERROR: " & Err.Description
    WScript.Quit 1
End If
On Error GoTo 0

' UTF-8 stream
Set stm = CreateObject("ADODB.Stream")
stm.Type = 2
stm.Charset = "utf-8"
stm.Open

' Columns to export (subset that we map to insurance_policies)
Dim cols, colList
colList = "app,policy,insurance,policytype,newrenew,namethai,telephone,address1,address2,province,postcode,license,licenseprovince,chasis,model,modelyear,datestart,dateend,datenotify,datecancel,datereceive,netpremium,stamp,vat,totalpremium,damage,agent,remark1"
cols = Split(colList, ",")

' Header
Dim i, line
line = ""
For i = 0 To UBound(cols)
    If i > 0 Then line = line & ","
    line = line & cols(i)
Next
stm.WriteText line & vbLf

' Query — ปี 2024-2026
Set rs = conn.Execute("SELECT " & colList & " FROM zzapp WHERE YEAR(datestart) >= 2024 AND YEAR(datestart) <= 2026")

Dim n: n = 0
Do While Not rs.EOF
    line = ""
    For i = 0 To UBound(cols)
        If i > 0 Then line = line & ","
        Dim v
        v = rs.Fields(cols(i)).Value
        If IsNull(v) Or IsEmpty(v) Then
            ' empty
        Else
            Dim s
            s = CStr(v)
            ' CSV escape
            If InStr(s, ",") > 0 Or InStr(s, """") > 0 Or InStr(s, vbCr) > 0 Or InStr(s, vbLf) > 0 Then
                s = """" & Replace(s, """", """""") & """"
            End If
            line = line & s
        End If
    Next
    stm.WriteText line & vbLf
    n = n + 1
    rs.MoveNext
Loop
rs.Close
conn.Close

stm.Position = 0
stm.SaveToFile "D:\tmp\zzapp_67_69.csv", 2
stm.Close
WScript.Echo "Exported " & n & " rows → D:\tmp\zzapp_67_69.csv"
