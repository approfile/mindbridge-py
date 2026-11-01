param(
    [Parameter(Mandatory=$true)][ValidatePattern('^[A-Za-z0-9.-]+$')][string]$HostName,
    [Parameter(Mandatory=$true)][ValidateRange(1,65535)][int]$Port,
    [ValidatePattern('^[a-zA-Z0-9_-]+$')][string]$UserName = 'root'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimePath = Join-Path $projectRoot '.local-runtime/autodl'
$identityPath = Join-Path $runtimePath 'id_ed25519'
$knownHostsPath = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.ssh/known_hosts'
$remoteLogin = "$UserName@$HostName"
New-Item -ItemType Directory -Force -Path $runtimePath | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.env'))) { throw 'Create the project .env first.' }
foreach ($command in @('ssh', 'scp', 'ssh-keygen')) { Get-Command $command -ErrorAction Stop | Out-Null }
if (-not (Test-Path -LiteralPath $identityPath)) {
    # Windows PowerShell 5 drops empty native arguments. Pass -N "" directly
    # through CreateProcess so the unattended tunnel key has no passphrase.
    $keygenInfo = [Diagnostics.ProcessStartInfo]::new()
    $keygenInfo.FileName = (Get-Command ssh-keygen).Source
    $keygenInfo.UseShellExecute = $false
    $keygenInfo.CreateNoWindow = $true
    $keygenInfo.Arguments = '-q -t ed25519 -f "' + $identityPath + '" -N "" -C mindbridge-model-tunnel'
    $keygenProcess = [Diagnostics.Process]::Start($keygenInfo)
    $keygenProcess.WaitForExit()
    if ($keygenProcess.ExitCode -ne 0) { throw 'SSH key generation failed.' }
}
# Interactive OpenSSH verifies the host fingerprint and asks for the AutoDL
# password locally. The password never enters .env or a command argument.
Write-Output 'Verify the SSH host fingerprint, then enter the AutoDL SSH password when prompted.'
Get-Content -LiteralPath "$identityPath.pub" | ssh -p $Port $remoteLogin 'umask 077; mkdir -p ~/.ssh; cat >> ~/.ssh/authorized_keys'
if ($LASTEXITCODE -ne 0) { throw 'SSH public key installation failed.' }
Copy-Item -LiteralPath $knownHostsPath -Destination (Join-Path $runtimePath 'known_hosts') -Force
scp -q -i $identityPath -P $Port "${remoteLogin}:/root/autodl-tmp/mindbridge-models/service.key" (Join-Path $runtimePath 'service.key')
if ($LASTEXITCODE -ne 0) { throw 'Service key retrieval failed. Run install.sh on AutoDL first.' }
scp -q -i $identityPath -P $Port "${remoteLogin}:/root/autodl-tmp/mindbridge-models/models.lock.json" (Join-Path $runtimePath 'models.lock.json')
if ($LASTEXITCODE -ne 0) { throw 'Model manifest retrieval failed.' }
$serviceKey = (Get-Content -LiteralPath (Join-Path $runtimePath 'service.key') -Raw).Trim()
if ($serviceKey -notmatch '^[A-Za-z0-9_-]{24,128}$') { throw 'Invalid service key file.' }
$manifest = Get-Content -LiteralPath (Join-Path $runtimePath 'models.lock.json') -Raw | ConvertFrom-Json
$modelRevision = "$($manifest.models.chat.revision)-$($manifest.models.embedding.revision)"
if ($modelRevision -notmatch '^[a-f0-9]{40}-[a-f0-9]{40}$') { throw 'Invalid model revisions.' }
$envPath = Join-Path $projectRoot '.env'
$envContent = Get-Content -LiteralPath $envPath -Raw
foreach ($entry in @{
    SELFHOST_API_KEY=$serviceKey
    SELFHOST_MODEL_REVISION=$modelRevision
    SELFHOST_CHAT_BASE_URL='http://model-tunnel:8000/v1'
    SELFHOST_EMBEDDING_BASE_URL='http://model-tunnel:8001/v1'
}.GetEnumerator()) {
    $line = "$($entry.Key)=$($entry.Value)"
    $pattern = "(?m)^$($entry.Key)=.*$"
    if ([regex]::IsMatch($envContent, $pattern)) { $envContent = [regex]::Replace($envContent, $pattern, $line) }
    else { $envContent = $envContent.TrimEnd() + "`n$line`n" }
}
[IO.File]::WriteAllText($envPath, $envContent, [Text.UTF8Encoding]::new($false))
[IO.File]::WriteAllText((Join-Path $runtimePath 'tunnel.env'), "SSH_HOST=$HostName`nSSH_PORT=$Port`nSSH_USER=$UserName`n", [Text.UTF8Encoding]::new($false))
Write-Output 'SSH tunnel configuration and private service key saved. Existing cloud keys preserved; no secrets printed.'
Write-Output 'Start with: .\scripts\start-local.ps1 -RealModels -WithSelfhost'
