# setup_backup_schedule.ps1
# ─────────────────────────────────────────────────────────────────
# ตั้ง Windows Task Scheduler ให้รัน backup_neon.py ทุกวัน 02:00
#
# วิธีใช้ (รัน PowerShell as Administrator):
#     .\setup_backup_schedule.ps1
#
# ลบ schedule:
#     Unregister-ScheduledTask -TaskName "InsuranceBackupDaily" -Confirm:$false

$TaskName  = "InsuranceBackupDaily"
$PythonExe = "D:\insurance-backend\venv\Scripts\python.exe"
$Script    = "D:\insurance-backend\scripts\backup_all.py"
$WorkDir   = "D:\insurance-backend"
$LogFile   = "D:\insurance-backend\backups\backup_log.txt"

# ลบของเก่าก่อน (ถ้ามี)
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# Action — เรียก python + redirect log
$action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"$PythonExe`" `"$Script`" >> `"$LogFile`" 2>&1" `
    -WorkingDirectory $WorkDir

# Trigger — ทุกวัน 02:00
$trigger = New-ScheduledTaskTrigger -Daily -At 2:00AM

# Settings — รันแม้ไม่ login, retry ถ้า fail
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 15)

# Principal — รันเป็น user ปัจจุบัน, ระดับ Highest
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Daily backup ของ Neon insurance DB → local + Google Drive" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal | Out-Null

Write-Host ""
Write-Host "✓ ตั้ง schedule สำเร็จ:" -ForegroundColor Green
Write-Host "    Task name: $TaskName"
Write-Host "    Schedule:  ทุกวัน 02:00"
Write-Host "    Log:       $LogFile"
Write-Host ""
Write-Host "ทดสอบรันทันที (ไม่รอ 02:00):"
Write-Host "    Start-ScheduledTask -TaskName $TaskName"
Write-Host ""
Write-Host "ดูสถานะ:"
Write-Host "    Get-ScheduledTask -TaskName $TaskName"
Write-Host ""
Write-Host "ดู log ล่าสุด:"
Write-Host "    Get-Content $LogFile -Tail 50"
