using FlowAgent;
using FlowAgent.Infrastructure;
using FlowAgent.Models;
using FlowAgent.Providers;
using Microsoft.Extensions.Options;

var builder = Host.CreateApplicationBuilder(args);

// Runs as a Windows Service (no-op when started interactively or on Linux,
// which is how the CI end-to-end test runs the very same binary).
builder.Services.AddWindowsService(o => o.ServiceName = "FlowAgent");
builder.Configuration.AddEnvironmentVariables(prefix: "FLOWAGENT_");

builder.Services.AddOptions<AgentOptions>()
    .Bind(builder.Configuration.GetSection(AgentOptions.Section))
    .Validate(o => Uri.TryCreate(o.ServerBaseUrl, UriKind.Absolute, out _), "Agent:ServerBaseUrl must be an absolute URL")
    .Validate(o => o.Capacity is >= 0 and <= 64, "Agent:Capacity must be between 0 and 64")
    .Validate(o => o.HeartbeatSeconds is >= 5 and <= 30, "Agent:HeartbeatSeconds must be between 5 and 30")
    .ValidateOnStart();

builder.Services.AddSingleton(sp => new LocalStore(sp.GetRequiredService<IOptions<AgentOptions>>().Value.ResolvedDataDirectory));
builder.Services.AddSingleton<CertificateManager>();
builder.Services.AddSingleton<ApiClient>();
builder.Services.AddSingleton<AgentLink>();
builder.Services.AddSingleton<IImageProvider, MockImageProvider>();
builder.Services.AddSingleton<IImageProvider, ApprovedImageProvider>();
builder.Services.AddSingleton<IImageProvider, GoogleFlowConnector>();
builder.Services.AddSingleton<ProviderRegistry>();
builder.Services.AddSingleton<SessionManager>();
builder.Services.AddSingleton<ResultUploader>();
builder.Services.AddSingleton<JobExecutor>();
builder.Services.AddHostedService<Worker>();
builder.Services.AddHostedService<HeartbeatService>();

var host = builder.Build();
host.Run();
