Option Explicit
Dim conn, cat, tbl, col
Set conn = CreateObject("ADODB.Connection")
conn.Open "Provider=Microsoft.Jet.OLEDB.4.0;Data Source=D:\tmp\Baby78_NEW.mdb;Jet OLEDB:Database Password=4949;"

Set cat = CreateObject("ADOX.Catalog")
cat.ActiveConnection = conn

Dim stm: Set stm = CreateObject("ADODB.Stream")
stm.Type = 2: stm.Charset = "utf-8": stm.Open
For Each tbl In cat.Tables
    If tbl.Name = "zzapp" Then
        stm.WriteText "[zzapp columns]" & vbLf
        For Each col In tbl.Columns
            stm.WriteText col.Name & vbLf
        Next
    End If
Next
stm.Position = 0
stm.SaveToFile "D:\tmp\new_zzapp_cols.txt", 2
stm.Close
conn.Close
WScript.Echo "saved"
