param([ValidateRange(1024, 65535)][int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$repoDirectory = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $repoDirectory
try {
    & (Join-Path $repoDirectory '.venv/Scripts/python.exe') -m battleships.haki_panel --port $Port
} finally {
    Pop-Location
}
