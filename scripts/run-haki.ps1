# Запускает только профиль «Хаки Наблюдения», токен хранится локально.
param(
    [ValidateRange(0, 2147483647)][int]$StopAfter = 0,
    [ValidateSet('TRAINING', 'RANKING')][string]$Mode = 'RANKING',
    [ValidateRange(1, 10)][int]$LossLimit = 2,
    [ValidateSet('EASY', 'MEDIUM', 'HARD')][string]$Opponent = 'EASY'
)
$ErrorActionPreference = 'Stop'
$repoDirectory = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $repoDirectory '.venv/Scripts/python.exe'
$previousHakiLossLimit = $env:HAKI_LOSS_LIMIT
Push-Location -LiteralPath $repoDirectory
try {
    $env:HAKI_LOSS_LIMIT = [string]$LossLimit
    & $pythonPath scripts/refresh_haki_memory.py
    & $pythonPath -u -m battleships --env-file .env.muzrob --token-env BATTLESHIPS_TOKEN_MUZROB `
        --strategy battleships.strategies.haki:HakiObservationBot --mode $Mode --opponent $Opponent `
        --log-dir logs/muzrob --stop-after $StopAfter --stop-on-loss --allow-bonuses
    $botExitCode = $LASTEXITCODE
    & $pythonPath scripts/refresh_haki_memory.py
} finally {
    $env:HAKI_LOSS_LIMIT = $previousHakiLossLimit
    Pop-Location
}
exit $botExitCode
