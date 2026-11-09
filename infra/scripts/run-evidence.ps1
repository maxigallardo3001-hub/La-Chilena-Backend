param([int]$LoadSeconds = 60)
$ErrorActionPreference = "Continue"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root
$out = Join-Path $root "evidencias"
New-Item -ItemType Directory -Force $out | Out-Null

function Write-Multiplier($value) {
    $lines = Get-Content .env | Where-Object { $_ -notmatch '^RATE_LIMIT_MULTIPLIER=' }
    $lines + "RATE_LIMIT_MULTIPLIER=$value" | Set-Content -Encoding ascii .env
}

function Set-Multiplier($value) {
    Write-Multiplier $value
    docker compose up -d gateway 2>$null | Out-Null
    Start-Sleep 15
}

function Invoke-Suite($name, [string[]]$command) {
    $file = Join-Path $out "$name.log"
    docker compose --profile tests run --rm tests @command 2>$null | Out-File -Encoding utf8 $file
    "{0,-16} exit={1}" -f $name, $LASTEXITCODE
}

docker compose down -v 2>$null | Out-Null
Write-Multiplier 1
docker compose --profile tests build 2>$null | Out-Null
docker compose up -d --wait 2>$null | Out-Null
docker compose ps -a --format "table {{.Service}}\t{{.Status}}" | Out-File -Encoding utf8 (Join-Path $out "compose-ps.log")

Invoke-Suite "smoke_auth" @("python", "smoke_auth.py")
Start-Sleep 61
Invoke-Suite "smoke_gateway" @("python", "smoke_gateway.py")

Set-Multiplier 200
Invoke-Suite "e2e" @("python", "e2e.py")
Invoke-Suite "contracts" @("python", "contracts.py")

$resilienceLog = Join-Path $out "resilience.log"
$script:resilienceExit = 0
function Invoke-Phase($phase) {
    docker compose --profile tests run --rm --no-deps tests python resilience.py $phase 2>$null | Out-File -Append -Encoding utf8 $resilienceLog
    if ($LASTEXITCODE -ne 0) { $script:resilienceExit = $LASTEXITCODE }
}
Remove-Item -ErrorAction SilentlyContinue $resilienceLog
Invoke-Phase "bordes"
foreach ($service in @("inventory", "payments", "auth")) {
    docker compose stop $service 2>$null | Out-Null
    Invoke-Phase "sin-$service"
    docker compose up -d --wait $service 2>$null | Out-Null
}
Invoke-Phase "recuperado"
Invoke-Phase "antes-reinicio"
docker compose down 2>$null | Out-Null
docker compose up -d --wait 2>$null | Out-Null
Invoke-Phase "tras-reinicio"
"{0,-16} exit={1}" -f "resilience", $script:resilienceExit

$sampler = Start-Job {
    while ($true) {
        docker stats --no-stream --format "{{.Name}};{{.CPUPerc}};{{.MemUsage}}" 2>$null
        Start-Sleep 3
    }
}
Invoke-Suite "loadtest" @("python", "loadtest.py", "$LoadSeconds")
Stop-Job $sampler
Receive-Job $sampler | Out-File -Encoding utf8 (Join-Path $out "loadtest-stats.log")
Remove-Job $sampler

$secrets = Get-Content .env | ForEach-Object { ($_ -split '=', 2)[1] } | Where-Object { $_ -and $_.Length -ge 12 }
docker compose logs --no-color --tail 300 2>$null | ForEach-Object {
    $line = $_
    foreach ($value in $secrets) { $line = $line.Replace($value, "[REDACTADO]") }
    $line
} | Out-File -Encoding utf8 (Join-Path $out "compose-logs.log")
