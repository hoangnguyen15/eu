namespace FlowAgent.Providers;

public sealed record ProviderRequest(
    string TaskId, string Prompt, string AspectRatio, int NumberOfImages,
    IReadOnlyList<string> ReferenceImageUrls, string? ProfileRef);

public enum ProviderJobState { Pending, Running, Succeeded, Failed }

public sealed record ProviderJobStatus(string JobId, ProviderJobState State, int Progress = 0,
    string? ErrorCode = null, string? ErrorMessage = null);

public sealed record ProviderImage(byte[] Data, string MimeType);

public sealed record ProviderResult(string JobId, IReadOnlyList<ProviderImage> Images, string? Model,
    Dictionary<string, object?> Metadata);

/// <summary>
/// Error codes map to generation states on the server:
/// AUTH_REQUIRED, PROVIDER_LIMITED, REVIEW_REQUIRED/OUTCOME_UNKNOWN, NOT_STARTED, anything else = FAILED.
/// </summary>
public class ProviderException : Exception
{
    public ProviderException(string code, string message, int? retryAfterSeconds = null) : base(message)
    {
        Code = code;
        RetryAfterSeconds = retryAfterSeconds;
    }

    public string Code { get; }
    public int? RetryAfterSeconds { get; }
}

/// <summary>Provider adapter contract (same shape as backend/app/providers/base.py).</summary>
public interface IImageProvider
{
    string Name { get; }
    bool Enabled { get; }

    Task<string> GenerateAsync(ProviderRequest request, CancellationToken ct);
    Task<ProviderJobStatus> GetStatusAsync(string jobId, CancellationToken ct);
    Task<ProviderResult> CollectResultAsync(string jobId, CancellationToken ct);

    /// <summary>Checks whether the local profile is usable. Must not read or export credentials.</summary>
    Task<string> CheckSessionAsync(string profileRef, CancellationToken ct);
}

public static class AspectSizes
{
    public static (int W, int H) For(string ratio) => ratio switch
    {
        "16:9" => (768, 432),
        "9:16" => (432, 768),
        "4:3" => (640, 480),
        "3:4" => (480, 640),
        _ => (512, 512),
    };
}

public sealed class ProviderRegistry
{
    private readonly Dictionary<string, IImageProvider> _providers;

    public ProviderRegistry(IEnumerable<IImageProvider> providers) =>
        _providers = providers.ToDictionary(p => p.Name, StringComparer.OrdinalIgnoreCase);

    public IImageProvider? Get(string name) =>
        _providers.TryGetValue(name, out var p) && p.Enabled ? p : null;

    public IEnumerable<string> EnabledNames(IEnumerable<string> configured) =>
        configured.Where(n => Get(n) is not null);
}
