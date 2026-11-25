#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Removes the Flow Agent service. With -PurgeData also deletes the agent identity
  (client certificate) and the local outbox. Remember to disable the workstation
  centrally: POST /api/v1/admin/workstations/{id}/disable
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $InstallDir = "$env:ProgramFiles\FlowAgent",
    [string] $DataDir = "$env:ProgramData\FlowAgent",
    [string] $ServiceName = "FlowAgent",
    [switch] $PurgeData
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    if ($svc.Status -ne 'Stopped') {
        Write-Host "Stopping $ServiceName"
        Stop-Service -Name $ServiceName -Force
        $svc.WaitForStatus('Stopped', '00:00:30')
    }
    if ($PSCmdlet.ShouldProcess($ServiceName, "delete service")) {
        & sc.exe delete $ServiceName | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "sc.exe delete failed ($LASTEXITCODE)" }
    }
} else {
    Write-Host "Service $ServiceName not installed"
}

if ((Test-Path $InstallDir) -and $PSCmdlet.ShouldProcess($InstallDir, "remove binaries")) {
    Remove-Item -Recurse -Force $InstallDir
}

if ($PurgeData) {
    $pending = Join-Path $DataDir "agent.db"
    if (Test-Path $pending) {
        Write-Warning "Purging ${DataDir}: unacknowledged outbox events in agent.db will be lost."
    }
    if ((Test-Path $DataDir) -and $PSCmdlet.ShouldProcess($DataDir, "remove identity and outbox")) {
        Remove-Item -Recurse -Force $DataDir
    }
} else {
    Write-Host "Kept $DataDir (identity + outbox). Use -PurgeData to remove it."
}
Write-Host "Flow Agent uninstalled." -ForegroundColor Green
