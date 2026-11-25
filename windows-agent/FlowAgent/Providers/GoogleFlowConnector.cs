namespace FlowAgent.Providers;

/// <summary>
/// Interface placeholder for Google Flow. DISABLED: Google Flow is not assumed
/// to expose a public automation API. This class deliberately contains no
/// browser automation and no CAPTCHA / MFA / anti-bot / rate-limit bypass, and
/// never reads or exports cookies or session tokens. It may only be replaced by
/// a real implementation after usage rights are confirmed and Google provides
/// or permits the integration method.
/// </summary>
public sealed class GoogleFlowConnector : IImageProvider
{
    public string Name => "google_flow";
    public bool Enabled => false;

    private static ProviderException Disabled() => new("PROVIDER_DISABLED",
        "GoogleFlowConnector is disabled: no confirmed, Google-permitted integration method is configured");

    public Task<string> GenerateAsync(ProviderRequest request, CancellationToken ct) => throw Disabled();
    public Task<ProviderJobStatus> GetStatusAsync(string jobId, CancellationToken ct) => throw Disabled();
    public Task<ProviderResult> CollectResultAsync(string jobId, CancellationToken ct) => throw Disabled();
    public Task<string> CheckSessionAsync(string profileRef, CancellationToken ct) => Task.FromResult("DISABLED");
}
