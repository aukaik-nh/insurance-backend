$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    throw 'Project Python is missing. Run: python -m venv .venv, then .\.venv\Scripts\python.exe -m pip install -r requirements.txt'
}
$env:PYTHONIOENCODING = 'utf-8'
& $projectPython -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
exit $LASTEXITCODE
