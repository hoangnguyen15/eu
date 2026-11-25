#!/usr/bin/env bash
# Builds the Windows workstation installer package on Linux/macOS with Docker
# (no Windows or .NET SDK needed on the build machine):
#   dist/FlowAgent-<version>-win-x64.zip  (+ .sha256)
# Contents: FlowAgent.exe (self-contained, single file), appsettings.json,
# Install-FlowAgent.cmd, Uninstall-FlowAgent.cmd, install.ps1, uninstall.ps1,
# README-CAI-DAT.txt. Unit tests run first.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
OUT="$ROOT/dist"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$OUT"

EXTRA=()
if [ -n "${EXTRA_CA_FILE:-}" ]; then   # TLS-intercepting proxy support
  EXTRA+=(-v "$EXTRA_CA_FILE:/usr/local/share/ca-certificates/extra.crt:ro" --network host
          -e HTTPS_PROXY="${HTTPS_PROXY:-}" -e NO_PROXY="localhost,127.0.0.1")
fi

docker run --rm "${EXTRA[@]}" -v "$ROOT/windows-agent:/src:ro" -v "$STAGE:/stage" \
  -e DOTNET_CLI_TELEMETRY_OPTOUT=1 -e DOTNET_NOLOGO=1 mcr.microsoft.com/dotnet/sdk:8.0 bash -ec '
    update-ca-certificates >/dev/null 2>&1 || true
    cp -r /src /work && cd /work
    rm -rf FlowAgent/bin FlowAgent/obj FlowAgent.Tests/bin FlowAgent.Tests/obj
    dotnet test FlowAgent.Tests/FlowAgent.Tests.csproj -c Release --nologo -v q
    dotnet publish FlowAgent/FlowAgent.csproj -c Release -r win-x64 --self-contained true \
      -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -p:DebugType=none -o /stage/pkg
    cp installer/* /stage/pkg/
    grep -o "<Version>[^<]*" FlowAgent/FlowAgent.csproj | cut -d">" -f2 > /stage/version'

VERSION="$(cat "$STAGE/version")"
ZIP="$OUT/FlowAgent-${VERSION}-win-x64.zip"
rm -f "$ZIP"
(cd "$STAGE/pkg" && python3 -c 'import sys,zipfile,os
z=zipfile.ZipFile(sys.argv[1],"w",zipfile.ZIP_DEFLATED)
[z.write(f) for f in sorted(os.listdir("."))]' "$ZIP")
(cd "$OUT" && { sha256sum "$(basename "$ZIP")" 2>/dev/null || shasum -a 256 "$(basename "$ZIP")"; } > "$(basename "$ZIP").sha256")
ls -l "$ZIP"; cat "$ZIP.sha256"
