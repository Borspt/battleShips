# Запускает AchievementBot (сбор ачивок, бонусы включены). Первый запуск сам готовит окружение и спрашивает токен.
# Токен хранится только локально в .env.achiever (файл в .gitignore) и не попадает в командную строку.
param(
    [ValidateRange(0, 2147483647)][int]$StopAfter = 0,
    [ValidateSet('TRAINING', 'RANKING')][string]$Mode = 'RANKING',
    [ValidateSet('EASY', 'MEDIUM', 'HARD')][string]$Opponent = 'EASY',
    [switch]$NewToken
)
$ErrorActionPreference = 'Stop'
$repoDirectory = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $repoDirectory
try {
    $pythonPath = Join-Path $repoDirectory '.venv/Scripts/python.exe'
    if (-not (Test-Path $pythonPath)) {
        Write-Host 'Создаю окружение Python (.venv)...'
        if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv .venv }
        elseif (Get-Command python -ErrorAction SilentlyContinue) { & python -m venv .venv }
        else { throw 'Python 3.12+ не найден. Установите его с python.org (галочка "Add to PATH") и запустите снова.' }
        & $pythonPath -m pip install --disable-pip-version-check -q -r requirements.txt
        if ($LASTEXITCODE -ne 0) { & $pythonPath -m pip install --disable-pip-version-check -q websockets }
    }
    $envFile = Join-Path $repoDirectory '.env.achiever'
    if ($NewToken -or -not (Test-Path $envFile)) {
        $secure = Read-Host 'Вставьте токен бота из Кабинета команды (ввод скрыт)' -AsSecureString
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
        if (-not $plain.Trim()) { throw 'Пустой токен.' }
        Set-Content -LiteralPath $envFile -Value "BATTLESHIPS_TOKEN_ACHIEVER=$($plain.Trim())" -Encoding UTF8
        $plain = $null
    }
    Write-Host "Запуск AchievementBot, режим $Mode. Остановить: Ctrl+C."
    & $pythonPath -u -m battleships --env-file .env.achiever --token-env BATTLESHIPS_TOKEN_ACHIEVER `
        --strategy battleships.strategies.achiever:AchievementBot --allow-bonuses `
        --mode $Mode --opponent $Opponent --log-dir logs/achiever --stop-after $StopAfter
    $botExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $botExitCode
