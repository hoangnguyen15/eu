using System.Text.Json;
using System.Text.Json.Nodes;

namespace FlowAgent.Models;

/// <summary>Wire protocol constants (mirrors backend/app/workers/protocol.py).</summary>
public static class MsgType
{
    public const string Register = "REGISTER";
    public const string Heartbeat = "HEARTBEAT";
    public const string TaskOffer = "TASK_OFFER";
    public const string TaskAccept = "TASK_ACCEPT";
    public const string TaskReject = "TASK_REJECT";
    public const string TaskStarted = "TASK_STARTED";
    public const string TaskProgress = "TASK_PROGRESS";
    public const string TaskCompleted = "TASK_COMPLETED";
    public const string TaskFailed = "TASK_FAILED";
    public const string TaskCancel = "TASK_CANCEL";
    public const string UploadRequest = "UPLOAD_REQUEST";
    public const string UploadCompleted = "UPLOAD_COMPLETED";
    public const string SessionStatus = "SESSION_STATUS";
    public const string SessionCheck = "SESSION_CHECK";
    public const string EventAck = "EVENT_ACK";
    public const string Error = "ERROR";
}

public static class Json
{
    public static readonly JsonSerializerOptions Options = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DictionaryKeyPolicy = JsonNamingPolicy.SnakeCaseLower,
        WriteIndented = false,
    };

    public static JsonObject ToObject<T>(T value) =>
        JsonSerializer.SerializeToNode(value, Options)!.AsObject();

    public static string Envelope(string type, JsonObject payload) =>
        new JsonObject
        {
            ["type"] = type,
            ["id"] = Guid.NewGuid().ToString("N"),
            ["ts"] = DateTimeOffset.UtcNow.ToString("O"),
            ["payload"] = payload,
        }.ToJsonString();
}

public sealed record Ack(string EventId, string Status, JsonObject Result, bool Duplicate, bool Retryable)
{
    public bool Ok => Status == "ok";

    public static Ack From(JsonObject payload) => new(
        payload["event_id"]?.GetValue<string>() ?? "",
        payload["status"]?.GetValue<string>() ?? "error",
        payload["result"] as JsonObject ?? new JsonObject(),
        payload["duplicate"]?.GetValue<bool>() ?? false,
        payload["retryable"]?.GetValue<bool>() ?? false);

    public string? Reason => Result["reason"]?.GetValue<string>();
}

public sealed record TaskOffer(
    string TaskId,
    string GenerationId,
    int Attempt,
    int SlotIndex,
    string Provider,
    string Prompt,
    string AspectRatio,
    int NumberOfImages,
    List<string> ReferenceImageUrls,
    string? ProfileRef,
    string? AccountEmail,
    DateTimeOffset OfferExpiresAt)
{
    public static TaskOffer Parse(JsonObject p) => p.Deserialize<TaskOffer>(Json.Options)
        ?? throw new InvalidDataException("invalid TASK_OFFER payload");
}

public sealed record RegisterPayload(string Hostname, string AgentVersion, int Capacity, List<string> Providers,
    List<string> ActiveTasks);

public sealed record SessionReport(string GoogleEmail, string ProfileRef, string Provider, string Status);

public sealed record UploadFileSpec(int Index, string MimeType, long Size, string Sha256);

public sealed record UploadedFile(int Index, string MimeType, long Size, string Sha256, string StorageKey,
    int? Width, int? Height);

public sealed record UploadTarget(int Index, string StorageKey, string Method, string Url,
    Dictionary<string, string> Headers);
