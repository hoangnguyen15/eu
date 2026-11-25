using System.Collections.Concurrent;
using System.Security.Cryptography;
using System.Text;
using FlowAgent.Models;
using Microsoft.Extensions.Options;

namespace FlowAgent.Providers;

/// <summary>
/// Local provider producing real, decodable PNGs for development and end-to-end
/// tests. Prompt control tokens exercise failure paths:
/// [mock:fail] [mock:auth] [mock:limit] [mock:review] [mock:slow]
/// </summary>
public sealed class MockImageProvider : IImageProvider
{
    private readonly int _latencyMs;
    private readonly ConcurrentDictionary<string, Task<List<byte[]>>> _jobs = new();
    private readonly ConcurrentDictionary<string, ProviderRequest> _requests = new();

    public MockImageProvider(IOptions<AgentOptions> opt) => _latencyMs = opt.Value.Mock.LatencyMilliseconds;

    public string Name => "mock";
    public bool Enabled => true;
    public const string Model = "mock-image-v1";

    public Task<string> GenerateAsync(ProviderRequest request, CancellationToken ct)
    {
        var p = request.Prompt;
        if (p.Contains("[mock:auth]")) throw new ProviderException("AUTH_REQUIRED", "Mock session requires re-authentication");
        if (p.Contains("[mock:limit]")) throw new ProviderException("PROVIDER_LIMITED", "Mock provider quota reached", 60);
        if (p.Contains("[mock:review]")) throw new ProviderException("REVIEW_REQUIRED", "Mock provider flagged the prompt");
        var jobId = "mock-" + Guid.NewGuid().ToString("N");
        _requests[jobId] = request;
        _jobs[jobId] = RenderAsync(request, ct);
        return Task.FromResult(jobId);
    }

    private async Task<List<byte[]>> RenderAsync(ProviderRequest r, CancellationToken ct)
    {
        var delay = _latencyMs + (r.Prompt.Contains("[mock:slow]") ? 5000 : 0);
        await Task.Delay(delay, ct);
        if (r.Prompt.Contains("[mock:fail]")) throw new ProviderException("PROVIDER_ERROR", "Mock provider failure");
        return Enumerable.Range(0, r.NumberOfImages).Select(i => Render(r.Prompt, r.AspectRatio, i)).ToList();
    }

    public static byte[] Render(string prompt, string aspectRatio, int index)
    {
        var (w, h) = AspectSizes.For(aspectRatio);
        var d = SHA256.HashData(Encoding.UTF8.GetBytes($"{prompt}|{index}"));
        return PngEncoder.EncodeRgb(w, h, (x, y) =>
        {
            var t = (double)y / Math.Max(1, h - 1);
            var s = (double)x / Math.Max(1, w - 1);
            byte Mix(byte a, byte b) => (byte)(a * (1 - t) + b * t);
            var stripe = ((x / 32) + (y / 32)) % 2 == 0 ? 0 : 24;
            return ((byte)Math.Min(255, Mix(d[0], d[3]) + stripe), Mix(d[1], d[4]), (byte)(Mix(d[2], d[5]) * (0.5 + s / 2)));
        });
    }

    public Task<ProviderJobStatus> GetStatusAsync(string jobId, CancellationToken ct)
    {
        if (!_jobs.TryGetValue(jobId, out var task))
            return Task.FromResult(new ProviderJobStatus(jobId, ProviderJobState.Failed, ErrorCode: "UNKNOWN_JOB"));
        if (!task.IsCompleted) return Task.FromResult(new ProviderJobStatus(jobId, ProviderJobState.Running, 50));
        if (task.IsFaulted)
        {
            var ex = task.Exception!.InnerException;
            var code = ex is ProviderException pe ? pe.Code : "PROVIDER_ERROR";
            return Task.FromResult(new ProviderJobStatus(jobId, ProviderJobState.Failed, ErrorCode: code, ErrorMessage: ex?.Message));
        }
        if (task.IsCanceled) return Task.FromResult(new ProviderJobStatus(jobId, ProviderJobState.Failed, ErrorCode: "CANCELLED"));
        return Task.FromResult(new ProviderJobStatus(jobId, ProviderJobState.Succeeded, 100));
    }

    public async Task<ProviderResult> CollectResultAsync(string jobId, CancellationToken ct)
    {
        if (!_jobs.TryRemove(jobId, out var task)) throw new ProviderException("UNKNOWN_JOB", "Unknown mock job");
        _requests.TryRemove(jobId, out var req);
        var images = await task;
        var seed = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(req?.Prompt ?? "")))[..8];
        return new ProviderResult(jobId, images.Select(b => new ProviderImage(b, "image/png")).ToList(), Model,
            new Dictionary<string, object?> { ["seed"] = seed, ["provider_job_id"] = jobId });
    }

    public Task<string> CheckSessionAsync(string profileRef, CancellationToken ct) => Task.FromResult("ACTIVE");
}
