#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Installs (or upgrades) the Flow Agent Windows service on Windows Server 2022.

.DESCRIPTION
  * copies the published agent to -InstallDir
  * writes appsettings.json with the gateway URL, capacity and enrollment token
  * restricts the data directory (identity key, SQLite outbox) to SYSTEM,
    Administrators and the service's virtual account
  * registers the service under the least-privileged virtual account
    "NT SERVICE\FlowAgent", delayed auto-start, restart-on-failure
  On first start the agent enrolls with the single-use token, stores its
  client certificate (DPAPI-protected) and connects outbound over mTLS WSS.

.EXAMPLE
  .\install.ps1 -SourceDir ..\publish\win-x64 -ServerBaseUrl https://gw.example.com:8443 `
      -EnrollmentToken <token> -CaCertificate .\ca.crt -Capacity 2 `
      -Account "mock-account@example.com=profile-1=mock"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $SourceDir,
    [Parameter(Mandatory)] [ValidatePattern('^https://')] [string] $ServerBaseUrl,
    [string] $EnrollmentToken = "",
    [string] $CaCertificate = "",
    [ValidateRange(0, 64)] [int] $Capacity = 2,
    [string[]] $Providers = @("mock"),
    # "email=profileRef=provider"; only an opaque local profile reference, never credentials
    [string[]] $Account = @(),
    [string] $InstallDir = "$env:ProgramFiles\FlowAgent",
    [string] $DataDir = "$env:ProgramData\FlowAgent",
    [string] $ServiceName = "FlowAgent"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step([string] $msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

$exe = Join-Path $SourceDir "FlowAgent.exe"
if (-not (Test-Path $exe)) { throw "FlowAgent.exe not found in $SourceDir (run publish.ps1 first)" }

# --- stop an existing installation (upgrade) ------------------------------
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Step "Stopping existing service $ServiceName"
    if ($existing.Status -ne 'Stopped') { Stop-Service -Name $ServiceName -Force; $existing.WaitForStatus('Stopped', '00:00:30') }
}

# --- files ------------------------------------------------------------------
Write-Step "Copying binaries to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$settingsPath = Join-Path $InstallDir "appsettings.json"
$keepSettings = (Test-Path $settingsPath)
Get-ChildItem -Path $SourceDir -File | Where-Object { -not ($keepSettings -and $_.Name -eq 'appsettings.json') } |
    Copy-Item -Destination $InstallDir -Force

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$caTarget = Join-Path $DataDir "ca.crt"
if ($CaCertificate) {
    if (-not (Test-Path $CaCertificate)) { throw "CA certificate not found: $CaCertificate" }
    Copy-Item $CaCertificate $caTarget -Force
}

# --- configuration ----------------------------------------------------------
Write-Step "Writing configuration"
$template = Get-Content (Join-Path $SourceDir "appsettings.json") -Raw | ConvertFrom-Json
if ($keepSettings) { $template = Get-Content $settingsPath -Raw | ConvertFrom-Json }
$agent = $template.Agent
$agent.ServerBaseUrl = $ServerBaseUrl
$agent.DataDirectory = $DataDir
$agent.CaCertificatePath = $(if (Test-Path $caTarget) { $caTarget } else { "" })
$agent.Capacity = $Capacity
$agent.Providers = @($Providers)
if ($EnrollmentToken) { $agent.EnrollmentToken = $EnrollmentToken }
if ($Account.Count -gt 0) {
    $agent.Accounts = @($Account | ForEach-Object {
        $p = $_.Split('=')
        if ($p.Count -ne 3) { throw "Account must be 'email=profileRef=provider': $_" }
        [pscustomobject]@{ GoogleEmail = $p[0]; ProfileRef = $p[1]; Provider = $p[2] }
    })
}
$template | ConvertTo-Json -Depth 10 | Set-Content -Path $settingsPath -Encoding UTF8

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
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    if ((Get-Item $path) -is [System.IO.FileInfo]) { $acl = New-Object System.Security.AccessControl.FileSecurity }
    $acl.SetAccessRuleProtection($true, $false)
    $inherit = if ($path -eq $DataDir) { "ContainerInherit,ObjectInherit" } else { "None" }
    foreach ($id in @("NT AUTHORITY\SYSTEM", "BUILTIN\Administrators", $svcAccount)) {
        $rights = if ($id -eq $svcAccount -and $path -eq $settingsPath) { "Read" } else { "FullControl" }
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
Write-Host "Flow Agent installed. Logs: Event Viewer > Windows Logs > Application (source: FlowAgent)." -ForegroundColor Green
if ($EnrollmentToken) {
    Write-Host "After the first successful start, the enrollment token is consumed; you may remove it from $settingsPath." -ForegroundColor Yellow
}
