using System.Net.Http.Json;
using System.Text.Json.Nodes;
using FlowAgent.Infrastructure;
using FlowAgent.Models;
using Microsoft.Extensions.Options;

namespace FlowAgent;

/// <summary>HTTP calls: enrollment against the gateway, and direct uploads to
/// object storage via pre-signed URLs (storage uses its own public TLS chain).</summary>
public sealed class ApiClient
{
    private readonly AgentOptions _opt;
    private readonly CertificateManager _certs;
    private readonly ILogger<ApiClient> _log;
    private readonly HttpClient _storage;

    public ApiClient(IOptions<AgentOptions> opt, CertificateManager certs, ILogger<ApiClient> log)
    {
        _opt = opt.Value;
        _certs = certs;
        _log = log;
        _storage = new HttpClient { Timeout = TimeSpan.FromMinutes(5) };
    }

    private HttpClient GatewayClient()
    {
        var handler = new SocketsHttpHandler();
        handler.SslOptions.RemoteCertificateValidationCallback = _certs.ValidateServer;
        return new HttpClient(handler) { BaseAddress = new Uri(_opt.ServerBaseUrl), Timeout = TimeSpan.FromSeconds(30) };
    }

    /// <summary>Exchanges the single-use enrollment token + a locally generated CSR for a client certificate.</summary>
    public async Task EnrollAsync(CancellationToken ct)
    {
        if (string.IsNullOrWhiteSpace(_opt.EnrollmentToken))
            throw new InvalidOperationException(
                "Agent is not enrolled and Agent:EnrollmentToken is empty. Create a token with the admin API.");
        var (csr, key) = _certs.CreateCsr();
        using (key)
        {
            using var http = GatewayClient();
            var resp = await http.PostAsJsonAsync("/api/v1/agent/enroll", new
            {
                token = _opt.EnrollmentToken,
                csr_pem = csr,
                hostname = Environment.MachineName,
            }, ct);
            var body = await resp.Content.ReadAsStringAsync(ct);
            if (!resp.IsSuccessStatusCode)
                throw new InvalidOperationException($"Enrollment failed: HTTP {(int)resp.StatusCode} {body}");
            var data = JsonNode.Parse(body)!["data"]!;
            _certs.SaveIdentity(
                data["certificate_pem"]!.GetValue<string>(), key,
                data["workstation_id"]!.GetValue<string>(),
                data["ca_certificate_pem"]?.GetValue<string>());
        }
        _log.LogInformation("Enrollment completed; the enrollment token is now consumed and can be removed from config");
    }

    public async Task UploadAsync(UploadTarget target, string filePath, CancellationToken ct)
    {
        await using var fs = File.OpenRead(filePath);
        using var req = new HttpRequestMessage(new HttpMethod(target.Method), target.Url)
        {
            Content = new StreamContent(fs),
        };
        foreach (var (k, v) in target.Headers)
        {
            if (k.Equals("Content-Type", StringComparison.OrdinalIgnoreCase))
                req.Content.Headers.ContentType = new System.Net.Http.Headers.MediaTypeHeaderValue(v);
            else
                req.Headers.TryAddWithoutValidation(k, v);
        }
        using var resp = await _storage.SendAsync(req, ct);
        if (!resp.IsSuccessStatusCode)
            throw new HttpRequestException(
                $"Upload of {target.StorageKey} failed: HTTP {(int)resp.StatusCode} {await resp.Content.ReadAsStringAsync(ct)}");
    }
}
