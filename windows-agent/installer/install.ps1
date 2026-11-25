#Requires -Version 5.1
<#
.SYNOPSIS
  Installs (or upgrades) the Flow Agent Windows service on Windows Server 2022.

.DESCRIPTION
  * copies the published agent to -InstallDir
  * writes appsettings.json (gateway URL, capacity, accounts, enrollment token)
  * stores the platform CA certificate used to verify the gateway
  * restricts the data directory (identity key, SQLite outbox) to SYSTEM,
    Administrators and the service's virtual account
  * registers the service under the least-privileged virtual account
    "NT SERVICE\FlowAgent", delayed auto-start, restart-on-failure
  On first start the agent enrolls with the single-use token, stores its
  client certificate (DPAPI-protected) and connects outbound over mTLS WSS.

  Easiest path: create the workstation in the admin console ("Thêm máy trạm"),
  put the downloaded flowagent-config.json next to this script and run
  Install-FlowAgent.cmd as administrator.

.EXAMPLE
  .\install.ps1 -ConfigFile .\flowagent-config.json

.EXAMPLE
  .\install.ps1 -ServerBaseUrl https://192.168.1.10:8443 -EnrollmentToken <token> `
      -CaCertificate .\ca.crt -Capacity 2 -Account "acct@example.com=profile-1=mock"

.EXAMPLE
  .\install.ps1 -ConfigFile .\flowagent-config.json -DryRun   # validate + print, change nothing
#>
[CmdletBinding()]
param(
    [string] $SourceDir = $PSScriptRoot,
    [string] $ConfigFile = "",
    [string] $ServerBaseUrl = "",
    [string] $EnrollmentToken = "",
    [string] $CaCertificate = "",
    [ValidateRange(0, 64)] [int] $Capacity = 0,
    [string[]] $Providers = @(),
    # "email=profileRef=provider"; only an opaque local profile reference, never credentials
    [string[]] $Account = @(),
    [string] $InstallDir = "$env:ProgramFiles\FlowAgent",
    [string] $DataDir = "$env:ProgramData\FlowAgent",
    [string] $ServiceName = "FlowAgent",
    [switch] $DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step([string] $msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# --- resolve settings: config file first, explicit parameters override -------
$caPem = ""
$accounts = @()
if ($ConfigFile) {
    if (-not (Test-Path $ConfigFile)) { throw "Config file not found: $ConfigFile" }
    $cfg = Get-Content $ConfigFile -Raw | ConvertFrom-Json
    function Get-Prop($obj, [string] $name) { if ($obj.PSObject.Properties[$name]) { $obj.$name } else { $null } }
    if (-not $ServerBaseUrl) { $ServerBaseUrl = [string](Get-Prop $cfg 'ServerBaseUrl') }
    if (-not $EnrollmentToken) { $EnrollmentToken = [string](Get-Prop $cfg 'EnrollmentToken') }
    if ($Capacity -eq 0 -and (Get-Prop $cfg 'Capacity')) { $Capacity = [int](Get-Prop $cfg 'Capacity') }
    if ($Providers.Count -eq 0 -and (Get-Prop $cfg 'Providers')) { $Providers = @(Get-Prop $cfg 'Providers') }
    if (Get-Prop $cfg 'CaCertificatePem') { $caPem = [string](Get-Prop $cfg 'CaCertificatePem') }
    if (Get-Prop $cfg 'Accounts') {
        $accounts = @((Get-Prop $cfg 'Accounts') | ForEach-Object {
            [pscustomobject]@{ GoogleEmail = $_.GoogleEmail; ProfileRef = $_.ProfileRef; Provider = $_.Provider } })
    }
    $expires = Get-Prop $cfg 'EnrollmentTokenExpiresAt'
    if ($expires -and ([datetime]$expires) -lt (Get-Date)) {
        Write-Warning "The enrollment token in $ConfigFile has expired; create a new configuration in the admin console."
    }
}
if ($Account.Count -gt 0) {
    $accounts = @($Account | ForEach-Object {
        $p = $_.Split('=')
        if ($p.Count -ne 3) { throw "Account must be 'email=profileRef=provider': $_" }
        [pscustomobject]@{ GoogleEmail = $p[0]; ProfileRef = $p[1]; Provider = $p[2] }
    })
}
if ($CaCertificate) {
    if (-not (Test-Path $CaCertificate)) { throw "CA certificate not found: $CaCertificate" }
    $caPem = Get-Content $CaCertificate -Raw
}
if ($Capacity -eq 0) { $Capacity = 2 }
if ($Providers.Count -eq 0) { $Providers = @("mock") }

if ($ServerBaseUrl -notmatch '^https://[^/]+') { throw "ServerBaseUrl must be an https:// URL (got '$ServerBaseUrl')" }
if ($caPem -and $caPem -notmatch '-----BEGIN CERTIFICATE-----') { throw "CA certificate is not PEM" }
foreach ($a in $accounts) {
    if ($a.ProfileRef -notmatch '^[A-Za-z0-9._-]+$') { throw "Invalid ProfileRef '$($a.ProfileRef)'" }
}

$exe = Join-Path $SourceDir "FlowAgent.exe"
if (-not $DryRun -and -not (Test-Path $exe)) { throw "FlowAgent.exe not found in $SourceDir" }

# --- build appsettings.json ---------------------------------------------------
$settingsPath = [System.IO.Path]::Combine($InstallDir, "appsettings.json")
$templatePath = if ((-not $DryRun) -and (Test-Path $settingsPath)) { $settingsPath } else { Join-Path $SourceDir "appsettings.json" }
$template = Get-Content $templatePath -Raw | ConvertFrom-Json
$agent = $template.Agent
$caTarget = [System.IO.Path]::Combine($DataDir, "ca.crt")
$agent.ServerBaseUrl = $ServerBaseUrl
$agent.DataDirectory = $DataDir
$agent.CaCertificatePath = $(if ($caPem) { $caTarget } else { "" })
$agent.Capacity = $Capacity
$agent.Providers = @($Providers)
if ($EnrollmentToken) { $agent.EnrollmentToken = $EnrollmentToken }
if ($accounts.Count -gt 0) { $agent.Accounts = @($accounts) }
$json = $template | ConvertTo-Json -Depth 10

if ($DryRun) {
    Write-Step "Dry run: configuration is valid; nothing was changed"
    $shown = $json -replace '("EnrollmentToken":\s*")[^"]+(")', '$1***$2'
    Write-Output $shown
    return
}

# --- elevation (only needed for the real install) ---------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script as Administrator (or use Install-FlowAgent.cmd)."
}

# --- stop an existing installation (upgrade) ------------------------------
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Step "Stopping existing service $ServiceName"
    if ($existing.Status -ne 'Stopped') { Stop-Service -Name $ServiceName -Force; $existing.WaitForStatus('Stopped', '00:00:30') }
}

# --- files ------------------------------------------------------------------
Write-Step "Copying binaries to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Get-ChildItem -Path $SourceDir -File |
    Where-Object { $_.Name -notin @('appsettings.json', 'flowagent-config.json') -and $_.Extension -ne '.ps1' -and $_.Extension -ne '.cmd' } |
    Copy-Item -Destination $InstallDir -Force
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
if ($caPem) { Set-Content -Path $caTarget -Value $caPem -Encoding ascii }

Write-Step "Writing configuration"
$json | Set-Content -Path $settingsPath -Encoding UTF8

# --- service ----------------------------------------------------------------
$svcAccount = "NT SERVICE\$ServiceName"
if (-not $existing) {
    Write-Step "Registering service $ServiceName ($svcAccount)"
    & sc.exe create $ServiceName binPath= "`"$(Join-Path $InstallDir 'FlowAgent.exe')`"" start= delayed-auto obj= "$svcAccount" DisplayName= "Flow Automation Agent" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "sc.exe create failed ($LASTEXITCODE)" }
    & sc.exe description $ServiceName "Executes image generation tasks for the Flow platform (outbound mTLS only)." | Out-Null
}
# restart after 10s, 30s, 60s; reset failure counter after 1 day
& sc.exe failure $ServiceName reset= 86400 actions= restart/10000/restart/30000/restart/60000 | Out-Null
& sc.exe failureflag $ServiceName 1 | Out-Null

# --- ACLs: identity + outbox are only readable by SYSTEM, Administrators, service --
Write-Step "Restricting ACLs"
foreach ($path in @($DataDir, $settingsPath)) {
    $isFile = (Get-Item $path) -is [System.IO.FileInfo]
    $acl = if ($isFile) { New-Object System.Security.AccessControl.FileSecurity } else { New-Object System.Security.AccessControl.DirectorySecurity }
    $acl.SetAccessRuleProtection($true, $false)
    $inherit = if ($isFile) { "None" } else { "ContainerInherit,ObjectInherit" }
    foreach ($id in @("NT AUTHORITY\SYSTEM", "BUILTIN\Administrators", $svcAccount)) {
        $rights = if ($id -eq $svcAccount -and $isFile) { "Read" } else { "FullControl" }
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($id, $rights, $inherit, "None", "Allow")))
    }
    Set-Acl -Path $path -AclObject $acl
}

# Event Log source (the virtual service account may not create it itself).
if (-not [System.Diagnostics.EventLog]::SourceExists($ServiceName)) {
    [System.Diagnostics.EventLog]::CreateEventSource($ServiceName, "Application")
}

Write-Step "Starting service"
Start-Service -Name $ServiceName
(Get-Service -Name $ServiceName).WaitForStatus('Running', '00:00:30')
Write-Host "Flow Agent installed. Logs: Event Viewer > Windows Logs > Application (source: $ServiceName)." -ForegroundColor Green
Write-Host "The workstation should appear ONLINE in the admin console within a few seconds." -ForegroundColor Green
if ($EnrollmentToken) {
    Write-Host "After the first successful start the enrollment token is consumed; you may delete flowagent-config.json." -ForegroundColor Yellow
}
