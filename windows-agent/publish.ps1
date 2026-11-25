#Requires -Version 5.1
<#
.SYNOPSIS
  Builds, tests and publishes the Flow Agent as a self-contained single-file
  win-x64 executable, then bundles it with the installer scripts into a zip.
#>
[CmdletBinding()]
param(
    [string] $Configuration = "Release",
    [string] $Runtime = "win-x64",
    [string] $OutputRoot = (Join-Path $PSScriptRoot "publish"),
    [switch] $SkipTests
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$project = Join-Path $PSScriptRoot "FlowAgent\FlowAgent.csproj"
$tests = Join-Path $PSScriptRoot "FlowAgent.Tests\FlowAgent.Tests.csproj"
$out = Join-Path $OutputRoot $Runtime

if (-not $SkipTests) {
    dotnet test $tests -c $Configuration
    if ($LASTEXITCODE -ne 0) { throw "tests failed" }
}

dotnet publish $project -c $Configuration -r $Runtime --self-contained true `
    -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -o $out
if ($LASTEXITCODE -ne 0) { throw "publish failed" }

Copy-Item (Join-Path $PSScriptRoot "installer\*.ps1") $out -Force
$version = (Get-Item (Join-Path $out "FlowAgent.exe")).VersionInfo.ProductVersion
$zip = Join-Path $OutputRoot "FlowAgent-$version-$Runtime.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path (Join-Path $out "*") -DestinationPath $zip
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -Path "$zip.sha256" -Value "$hash  $(Split-Path $zip -Leaf)"
Write-Host "Published $zip (sha256 $hash)" -ForegroundColor Green
