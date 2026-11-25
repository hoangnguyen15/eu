using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json.Nodes;
using FlowAgent.Models;
using Microsoft.Extensions.Options;

namespace FlowAgent.Providers;

/// <summary>
/// Adapter template for an image API you are licensed to use. The neutral HTTP
/// contract below must be mapped to the real vendor API before enabling:
///   POST {base}/v1/images/jobs {prompt, aspect_ratio, n} -> {id}
///   GET  {base}/v1/images/jobs/{id} -> {status, progress, images:[{url}], model, error}
/// 401/403 -> AUTH_REQUIRED; 429 -> PROVIDER_LIMITED (surfaced, never evaded).
/// The API key is read from an environment variable, never from the server.
/// </summary>
public sealed class ApprovedImageProvider : IImageProvider
{
    private readonly string _baseUrl;
    private readonly string? _apiKey;
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(60) };

    public ApprovedImageProvider(IOptions<AgentOptions> opt)
    {
        _baseUrl = opt.Value.Approved.BaseUrl.TrimEnd('/');
        _apiKey = Environment.GetEnvironmentVariable(opt.Value.Approved.ApiKeyEnvironmentVariable);
    }

    public string Name => "approved";
    public bool Enabled => !string.IsNullOrEmpty(_baseUrl) && !string.IsNullOrEmpty(_apiKey);

    private HttpRequestMessage Req(HttpMethod m, string path, object? body = null)
    {
        if (!Enabled) throw new ProviderException("PROVIDER_DISABLED", "ApprovedImageProvider is not configured");
        var r = new HttpRequestMessage(m, _baseUrl + path);
        r.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _apiKey);
        if (body is not null) r.Content = JsonContent.Create(body);
        return r;
    }

    private static void Check(HttpResponseMessage resp)
    {
        if (resp.StatusCode is HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden)
            throw new ProviderException("AUTH_REQUIRED", $"Provider rejected credentials ({(int)resp.StatusCode})");
        if (resp.StatusCode == HttpStatusCode.TooManyRequests)
            throw new ProviderException("PROVIDER_LIMITED", "Provider rate limit",
                (int?)resp.Headers.RetryAfter?.Delta?.TotalSeconds);
        if (!resp.IsSuccessStatusCode)
            throw new ProviderException("PROVIDER_ERROR", $"Provider HTTP {(int)resp.StatusCode}");
    }

    public async Task<string> GenerateAsync(ProviderRequest request, CancellationToken ct)
    {
        using var resp = await _http.SendAsync(Req(HttpMethod.Post, "/v1/images/jobs",
            new { prompt = request.Prompt, aspect_ratio = request.AspectRatio, n = request.NumberOfImages }), ct);
        Check(resp);
        var body = JsonNode.Parse(await resp.Content.ReadAsStringAsync(ct))!;
        return body["id"]!.ToString();
    }

    public async Task<ProviderJobStatus> GetStatusAsync(string jobId, CancellationToken ct)
    {
        using var resp = await _http.SendAsync(Req(HttpMethod.Get, $"/v1/images/jobs/{Uri.EscapeDataString(jobId)}"), ct);
        Check(resp);
        var body = JsonNode.Parse(await resp.Content.ReadAsStringAsync(ct))!;
        var state = body["status"]?.ToString() switch
        {
            "pending" => ProviderJobState.Pending,
            "running" => ProviderJobState.Running,
            "succeeded" => ProviderJobState.Succeeded,
            _ => ProviderJobState.Failed,
        };
        return new ProviderJobStatus(jobId, state, body["progress"]?.GetValue<int>() ?? 0,
            body["error"]?["code"]?.ToString(), body["error"]?["message"]?.ToString());
    }

    public async Task<ProviderResult> CollectResultAsync(string jobId, CancellationToken ct)
    {
        using var resp = await _http.SendAsync(Req(HttpMethod.Get, $"/v1/images/jobs/{Uri.EscapeDataString(jobId)}"), ct);
        Check(resp);
        var body = JsonNode.Parse(await resp.Content.ReadAsStringAsync(ct))!;
        var images = new List<ProviderImage>();
        foreach (var item in body["images"]?.AsArray() ?? new JsonArray())
        {
            var url = item?["url"]?.ToString()
                ?? throw new ProviderException("PROVIDER_ERROR", "Provider returned an image without url");
            // Result URLs are typically pre-signed: do not forward the API key to them.
            using var img = await _http.GetAsync(url, ct);
            Check(img);
            images.Add(new ProviderImage(await img.Content.ReadAsByteArrayAsync(ct),
                img.Content.Headers.ContentType?.MediaType ?? "image/png"));
        }
        return new ProviderResult(jobId, images, body["model"]?.ToString(),
            new Dictionary<string, object?> { ["provider_job_id"] = jobId });
    }

    public Task<string> CheckSessionAsync(string profileRef, CancellationToken ct) =>
        Task.FromResult(Enabled ? "ACTIVE" : "DISABLED");
}
