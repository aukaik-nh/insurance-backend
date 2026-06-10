' export_addr_map.vbs — Dump address1/address2/namethai → CSV เพื่อใช้ match PDFs
Option Explicit
Dim conn, rs, stm
Set conn = CreateObject("ADODB.Connection")
conn.Open "Provider=Microsoft.Jet.OLEDB.4.0;Data Source=D:\tmp\Baby78_NEW.mdb;Jet OLEDB:Database Password=4949;"

Set stm = CreateObject("ADODB.Stream")
stm.Type = 2: stm.Charset = "utf-8": stm.Open
stm.WriteText "app,policy,policytype,license,address1,address2,namethai,datestart" & vbLf

Set rs = conn.Execute("SELECT app, policy, policytype, license, address1, address2, namethai, datestart FROM zzapp WHERE YEAR(datestart) >= 2024 AND YEAR(datestart) <= 2026")
Dim n: n = 0
Do While Not rs.EOF
    Dim line: line = ""
    Dim fields, i
    fields = Array("app","policy","policytype","license","address1","address2","namethai","datestart")
    For i = 0 To UBound(fields)
        If i > 0 Then line = line & ","
        Dim v: v = rs.Fields(fields(i)).Value
        If Not (IsNull(v) Or IsEmpty(v)) Then
            Dim s: s = CStr(v)
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
stm.SaveToFile "D:\tmp\addr_map_67_69.csv", 2
stm.Close
WScript.Echo "Exported " & n & " rows"
