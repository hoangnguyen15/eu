using System.Collections.Concurrent;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;
using FlowAgent.Infrastructure;
using FlowAgent.Models;
using FlowAgent.Providers;
using Microsoft.Extensions.Options;

namespace FlowAgent;

/// <summary>
/// Executes offered tasks on a fixed number of slots.
///
/// Retry policy (deliberately conservative):
///  * before the provider is invoked (ACCEPTING/ACCEPTED) a task may be handed
///    back (TASK_FAILED NOT_STARTED) and the server re-queues it;
///  * once the provider was invoked (STARTED) and the outcome is unknown
///    (crash, cancellation, unexpected error) the task is reported as
///    OUTCOME_UNKNOWN and is NEVER retried automatically;
///  * results already persisted locally (PROVIDER_DONE) are uploaded after a
///    restart instead of re-generating them.
/// </summary>
public sealed class JobExecutor
{
    private readonly AgentOptions _opt;
    private readonly AgentLink _link;
    private readonly LocalStore _store;
    private readonly ProviderRegistry _providers;
    private readonly ResultUploader _uploader;
    private readonly SessionManager _sessions;
    private readonly ILogger<JobExecutor> _log;
    private readonly SemaphoreSlim _slots;
    private readonly ConcurrentDictionary<string, RunningTask> _active = new();

    private sealed class RunningTask(string generationId, CancellationTokenSource cts)
    {
        public string GenerationId { get; } = generationId;
        public CancellationTokenSource Cts { get; } = cts;
        public volatile bool ProviderInvoked;
        public volatile bool ServerCancelled;
    }

    public JobExecutor(IOptions<AgentOptions> opt, AgentLink link, LocalStore store, ProviderRegistry providers,
        ResultUploader uploader, SessionManager sessions, ILogger<JobExecutor> log)
    {
        _opt = opt.Value;
        _link = link;
        _store = store;
        _providers = providers;
        _uploader = uploader;
        _sessions = sessions;
        _log = log;
        _slots = new SemaphoreSlim(Math.Max(0, _opt.Capacity), Math.Max(1, _opt.Capacity));
    }

    public IEnumerable<string> ActiveTaskIds() => _active.Keys;
    public int FreeSlots => _slots.CurrentCount;

    public async Task OnOfferAsync(JsonObject payload, CancellationToken ct)
    {
        TaskOffer offer;
        try { offer = TaskOffer.Parse(payload); }
        catch (Exception ex) { _log.LogWarning(ex, "Invalid TASK_OFFER"); return; }

        if (_active.ContainsKey(offer.TaskId)) return; // duplicate offer (re-sent after reconnect)
        var provider = _providers.Get(offer.Provider);
        if (provider is null)
        {
            await RejectAsync(offer, "PROVIDER_UNAVAILABLE", ct);
            return;
        }
        if (!await _slots.WaitAsync(0, ct))
        {
            await RejectAsync(offer, "NO_FREE_SLOT", ct);
            return;
        }

        var running = new RunningTask(offer.GenerationId, CancellationTokenSource.CreateLinkedTokenSource(ct));
        if (!_active.TryAdd(offer.TaskId, running))
        {
            _slots.Release();
            return;
        }
        try
        {
            await _store.SetTaskStateAsync(offer.TaskId, offer.GenerationId, TaskState.Accepting);
            var ack = await _link.RequestAsync(MsgType.TaskAccept, new JsonObject { ["task_id"] = offer.TaskId },
                persist: false, TimeSpan.FromSeconds(_opt.AcceptTimeoutSeconds), ct);
            if (ack is null || !ack.Ok)
            {
                _log.LogInformation("Task {Task} not confirmed ({Reason}); not executing", offer.TaskId,
                    ack?.Reason ?? "ack timeout");
                if (ack is null)
                    await FailAsync(offer.TaskId, offer.GenerationId, "NOT_STARTED", "accept not confirmed", null);
                Finish(offer.TaskId);
                await _store.DeleteTaskAsync(offer.TaskId);
                return;
            }
            var deadline = ack.Result["lease_expires_at"]?.GetValue<string>() is { } s
                ? DateTimeOffset.Parse(s) : DateTimeOffset.UtcNow.AddMinutes(10);
            await _store.SetTaskStateAsync(offer.TaskId, offer.GenerationId, TaskState.Accepted);
            _ = Task.Run(() => ExecuteAsync(offer, provider, running, deadline), CancellationToken.None);
        }
        catch (Exception ex)
        {
            _log.LogError(ex, "Failed to accept task {Task}", offer.TaskId);
            Finish(offer.TaskId);
        }
    }

    private async Task RejectAsync(TaskOffer offer, string reason, CancellationToken ct)
    {
        _log.LogInformation("Rejecting task {Task}: {Reason}", offer.TaskId, reason);
        await _link.SendEventAsync(MsgType.TaskReject,
            new JsonObject { ["task_id"] = offer.TaskId, ["reason"] = reason }, persist: true, ct);
    }

    public void OnCancel(JsonObject payload)
    {
        var taskId = payload["task_id"]?.GetValue<string>();
        if (taskId is not null && _active.TryGetValue(taskId, out var t))
        {
            _log.LogInformation("Server cancelled task {Task} ({Reason})", taskId, payload["reason"]?.ToString());
            t.ServerCancelled = true;
            t.Cts.Cancel();
        }
    }

    private void Finish(string taskId)
    {
        if (_active.TryRemove(taskId, out var t))
        {
            t.Cts.Dispose();
            _slots.Release();
        }
    }

    private string ResultDir(string taskId) => Path.Combine(_opt.ResolvedDataDirectory, "results", taskId);

    private async Task ExecuteAsync(TaskOffer offer, IImageProvider provider, RunningTask running, DateTimeOffset deadline)
    {
        var ct = running.Cts.Token;
        var taskId = offer.TaskId;
        try
        {
            // Record STARTED locally *before* invoking the provider: after a crash
            // this tells us the outcome is unknown and must not be retried.
            await _store.SetTaskStateAsync(taskId, offer.GenerationId, TaskState.Started);
            running.ProviderInvoked = true;
            var startedAck = await _link.SendEventAsync(MsgType.TaskStarted, new JsonObject { ["task_id"] = taskId },
                persist: true, ct);

            var request = new ProviderRequest(taskId, offer.Prompt, offer.AspectRatio, offer.NumberOfImages,
                offer.ReferenceImageUrls, offer.ProfileRef);
            var jobId = await provider.GenerateAsync(request, ct);
            ProviderJobStatus status;
            var lastProgress = -1;
            while (true)
            {
                status = await provider.GetStatusAsync(jobId, ct);
                if (status.State is ProviderJobState.Succeeded or ProviderJobState.Failed) break;
                if (status.Progress != lastProgress)
                {
                    lastProgress = status.Progress;
                    await _link.SendEphemeralAsync(MsgType.TaskProgress,
                        new JsonObject { ["task_id"] = taskId, ["progress"] = Math.Min(95, status.Progress) }, ct);
                }
                await Task.Delay(500, ct);
            }
            if (status.State == ProviderJobState.Failed)
                throw new ProviderException(status.ErrorCode ?? "PROVIDER_ERROR", status.ErrorMessage ?? "provider failed");

            var result = await provider.CollectResultAsync(jobId, ct);
            var manifest = await PersistResultAsync(taskId, provider.Name, result);
            await _store.SetTaskStateAsync(taskId, offer.GenerationId, TaskState.ProviderDone,
                JsonSerializer.Serialize(manifest, Json.Options));

            await AwaitAck(startedAck, deadline, ct);
            await _uploader.UploadAndCompleteAsync(taskId, offer.GenerationId, manifest, deadline, ct);
        }
        catch (ProviderException pe)
        {
            if (pe.Code == "AUTH_REQUIRED") _sessions.MarkAuthRequired(offer.ProfileRef);
            await FailAsync(taskId, offer.GenerationId, pe.Code, pe.Message, pe.RetryAfterSeconds);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            if (running.ServerCancelled)
            {
                // Lease expired / revoked: the server has already finalized the task.
                _log.LogWarning("Task {Task} cancelled by server; results discarded, not retried", taskId);
                await _store.DeleteTaskAsync(taskId);
                TryDeleteDir(ResultDir(taskId));
            }
            else if (!running.ProviderInvoked)
            {
                await FailAsync(taskId, offer.GenerationId, "NOT_STARTED", "cancelled before start", null);
            }
            else
            {
                // Service stopping mid-task: keep local state so the next start
                // reports OUTCOME_UNKNOWN (or resumes the upload if results exist).
                _log.LogWarning("Service stopping while task {Task} was in flight", taskId);
            }
        }
        catch (UploadRejectedException ur)
        {
            _log.LogWarning("Task {Task} result rejected by server: {Reason}", taskId, ur.Message);
            await _store.SetTaskStateAsync(taskId, offer.GenerationId, TaskState.Failed);
        }
        catch (Exception ex)
        {
            _log.LogError(ex, "Task {Task} failed unexpectedly", taskId);
            var code = running.ProviderInvoked ? "OUTCOME_UNKNOWN" : "NOT_STARTED";
            await FailAsync(taskId, offer.GenerationId, code, ex.Message, null);
        }
        finally
        {
            Finish(taskId);
        }
    }

    internal static async Task AwaitAck(Task<Ack> ack, DateTimeOffset deadline, CancellationToken ct)
    {
        var remaining = deadline - DateTimeOffset.UtcNow;
        if (remaining < TimeSpan.FromSeconds(1)) remaining = TimeSpan.FromSeconds(1);
        var done = await Task.WhenAny(ack, Task.Delay(remaining, ct));
        if (done != ack) throw new TimeoutException("server acknowledgement not received before lease deadline");
        var a = await ack;
        if (!a.Ok) throw new UploadRejectedException(a.Reason ?? a.Status);
    }

    private async Task FailAsync(string taskId, string generationId, string code, string message, int? retryAfter)
    {
        _log.LogWarning("Task {Task} failed: {Code} {Message}", taskId, code, message);
        var payload = new JsonObject
        {
            ["task_id"] = taskId,
            ["error_code"] = code,
            ["error_message"] = message.Length > 1000 ? message[..1000] : message,
        };
        if (retryAfter is not null) payload["retry_after_seconds"] = retryAfter;
        await _link.SendEventAsync(MsgType.TaskFailed, payload, persist: true, CancellationToken.None);
        await _store.SetTaskStateAsync(taskId, generationId, TaskState.Failed);
        TryDeleteDir(ResultDir(taskId));
    }

    private async Task<ResultManifest> PersistResultAsync(string taskId, string provider, ProviderResult result)
    {
        var dir = ResultDir(taskId);
        Directory.CreateDirectory(dir);
        var files = new List<ResultFile>();
        for (var i = 0; i < result.Images.Count; i++)
        {
            var img = result.Images[i];
            var ext = img.MimeType switch { "image/jpeg" => "jpg", "image/webp" => "webp", _ => "png" };
            var path = Path.Combine(dir, $"{i}.{ext}");
            await File.WriteAllBytesAsync(path, img.Data);
            files.Add(new ResultFile(i, img.MimeType, img.Data.LongLength,
                Convert.ToHexString(SHA256.HashData(img.Data)).ToLowerInvariant(), path));
        }
        return new ResultManifest(provider, result.Model, result.Metadata, files);
    }

    internal void TryDeleteDir(string dir)
    {
        try { if (Directory.Exists(dir)) Directory.Delete(dir, recursive: true); }
        catch (IOException ex) { _log.LogDebug(ex, "cleanup failed"); }
    }

    /// <summary>Called once at service start, before connecting.</summary>
    public async Task RecoverAsync(CancellationToken ct)
    {
        foreach (var t in await _store.TasksAsync())
        {
            switch (t.State)
            {
                case TaskState.Accepting:
                case TaskState.Accepted:
                    await FailAsync(t.TaskId, t.GenerationId, "NOT_STARTED", "agent restarted before start", null);
                    await _store.DeleteTaskAsync(t.TaskId);
                    break;
                case TaskState.Started:
                    // Provider may or may not have produced output: report, never retry.
                    await FailAsync(t.TaskId, t.GenerationId, "OUTCOME_UNKNOWN",
                        "agent restarted while the provider call was in flight", null);
                    await _store.DeleteTaskAsync(t.TaskId);
                    break;
                case TaskState.ProviderDone when t.Detail is not null:
                    var manifest = JsonSerializer.Deserialize<ResultManifest>(t.Detail, Json.Options)!;
                    if (!await _slots.WaitAsync(0, ct)) break;
                    var running = new RunningTask(t.GenerationId, CancellationTokenSource.CreateLinkedTokenSource(ct));
                    _active[t.TaskId] = running;
                    _log.LogInformation("Resuming upload of locally stored results for task {Task}", t.TaskId);
                    _ = Task.Run(async () =>
                    {
                        try
                        {
                            await _uploader.UploadAndCompleteAsync(t.TaskId, t.GenerationId, manifest,
                                DateTimeOffset.UtcNow.AddMinutes(30), running.Cts.Token);
                        }
                        catch (Exception ex)
                        {
                            _log.LogWarning("Resumed upload for {Task} ended: {Message}", t.TaskId, ex.Message);
                            await _store.SetTaskStateAsync(t.TaskId, t.GenerationId, TaskState.Failed);
                        }
                        finally { Finish(t.TaskId); }
                    }, CancellationToken.None);
                    break;
                default:
                    await _store.DeleteTaskAsync(t.TaskId);
                    TryDeleteDir(ResultDir(t.TaskId));
                    break;
            }
        }
    }
}

public sealed record ResultFile(int Index, string MimeType, long Size, string Sha256, string Path);

public sealed record ResultManifest(string Provider, string? Model, Dictionary<string, object?> Metadata, List<ResultFile> Files);

public sealed class UploadRejectedException(string reason) : Exception(reason);
