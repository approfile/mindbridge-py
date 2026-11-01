param([switch]$RealModels, [switch]$RealGenerationOnly, [switch]$WithSelfhost)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$savedProvider = $env:MINDBRIDGE_LOCAL_PROVIDER
$savedVector = $env:MINDBRIDGE_LOCAL_VECTOR
Push-Location $projectRoot
try {
    if (-not (Test-Path -LiteralPath '.env')) {
        throw 'Create .env from .env.example and fill in the local configuration first.'
    }
    $env:MINDBRIDGE_LOCAL_PROVIDER = 'mock'
    $env:MINDBRIDGE_LOCAL_VECTOR = 'false'
    if ($RealModels -or $RealGenerationOnly) {
        # Only inspect credential presence; never echo the values.
        $values = @{}
        foreach ($line in Get-Content -LiteralPath '.env') {
            if ($line -match '^([A-Z_]+)=(.*)$') { $values[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'") }
        }
        $requiredKeys = @('OPENAI_API_KEY')
        if ($RealModels) { $requiredKeys += 'OPENAI_EMBEDDING_API_KEY' }
        foreach ($key in $requiredKeys) {
            if (-not $values[$key]) { throw "Fill $key in .env first." }
        }
        $env:MINDBRIDGE_LOCAL_PROVIDER = 'openai'
        if ($RealModels) { $env:MINDBRIDGE_LOCAL_VECTOR = 'true' }
    }
    docker info --format '{{.ServerVersion}}' *> $null
    if ($LASTEXITCODE -ne 0) {
        docker desktop start --timeout 120
        if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop with its Linux engine, then retry.' }
    }
    $composeFiles = @('-f', 'docker-compose.local.yml')
    if ($WithSelfhost) { $composeFiles += @('-f', 'docker-compose.selfhost.yml') }
    docker compose @composeFiles up -d --build --wait --wait-timeout 240
    if ($LASTEXITCODE -ne 0) { throw 'Local stack startup failed; inspect docker compose logs.' }
    Write-Output 'MindBridge: http://127.0.0.1:8080'
    Write-Output 'Mailpit:    http://127.0.0.1:8025'
    Write-Output 'Student: student / student123; Admin: admin / admin123'
}
finally {
    $env:MINDBRIDGE_LOCAL_PROVIDER = $savedProvider
    $env:MINDBRIDGE_LOCAL_VECTOR = $savedVector
    Pop-Location
}
