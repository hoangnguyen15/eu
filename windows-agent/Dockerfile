# syntax=docker/dockerfile:1.7
# Linux container of the *same* FlowAgent source, used for local development and
# the automated end-to-end test. Production runs FlowAgent.exe as a Windows
# Service on Windows Server 2022 (see installer/install.ps1).

FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY FlowAgent/FlowAgent.csproj FlowAgent/
COPY FlowAgent.Tests/FlowAgent.Tests.csproj FlowAgent.Tests/
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then cp /run/secrets/extra_ca /usr/local/share/ca-certificates/extra.crt && update-ca-certificates; fi; \
    dotnet restore FlowAgent.Tests/FlowAgent.Tests.csproj
COPY . .
RUN dotnet test FlowAgent.Tests/FlowAgent.Tests.csproj -c Release --no-restore \
 && dotnet publish FlowAgent/FlowAgent.csproj -c Release --no-restore -o /out

FROM mcr.microsoft.com/dotnet/runtime:8.0
RUN useradd --uid 10002 --create-home agent && mkdir -p /var/lib/flowagent && chown agent /var/lib/flowagent
WORKDIR /opt/flowagent
COPY --from=build /out .
USER agent
ENV FLOWAGENT_Agent__DataDirectory=/var/lib/flowagent \
    DOTNET_CLI_TELEMETRY_OPTOUT=1
ENTRYPOINT ["dotnet", "FlowAgent.dll"]
