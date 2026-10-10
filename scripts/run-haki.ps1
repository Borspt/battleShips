# Запускает только профиль «Хаки Наблюдения», токен хранится локально.
param([ValidateRange(0, 2147483647)][int]$StopAfter = 0)
$ErrorActionPreference = 'Stop'
$repoDirectory = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $repoDirectory '.venv/Scripts/python.exe'
Push-Location -LiteralPath $repoDirectory
try {
    & $pythonPath -u -m battleships --env-file .env.muzrob --token-env BATTLESHIPS_TOKEN_MUZROB `
        --strategy battleships.strategies.haki:HakiObservationBot --mode RANKING `
        --log-dir logs/muzrob --stop-after $StopAfter
    $botExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $botExitCode
