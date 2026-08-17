# Builds a standalone Windows distributable of childdiary_backup.py: a
# folder with the .exe + Python runtime + Playwright's Node driver bundled
# in, so it runs on a PC with no Python/pip install step. Chromium itself
# is NOT bundled (that was ~300MB) -- the exe downloads it automatically
# on first run on the target PC instead (needs internet once, then cached).
#
# Run this only on the dev machine (this one). Ship the resulting
# dist\ChildDiaryBackup folder as-is to the target PC.

$ErrorActionPreference = "Stop"

$driverDir = python -c "import playwright, os; print(os.path.join(os.path.dirname(playwright.__file__), 'driver'))"
if (-not (Test-Path $driverDir)) {
    throw "Driver do Playwright nao encontrado em $driverDir"
}
Write-Host "A usar driver Playwright de: $driverDir"

pip install --only-binary=:all: pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install pyinstaller falhou" }

pyinstaller --onedir --noconfirm --name ChildDiaryBackup `
    --add-data "$driverDir;playwright\driver" `
    childdiary_backup.py

Write-Host ""
Write-Host "Build pronto em: dist\ChildDiaryBackup\"
Write-Host "Copia essa pasta inteira (nao so o .exe) para o outro PC e corre ChildDiaryBackup.exe."
Write-Host "1a corrida no PC destino descarrega o Chromium (~150MB, precisa internet); corridas seguintes usam a cache."
