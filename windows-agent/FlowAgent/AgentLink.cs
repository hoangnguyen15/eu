using System.Collections.Concurrent;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json.Nodes;
using FlowAgent.Infrastructure;
using FlowAgent.Models;

namespace FlowAgent;

/// <summary>
/// The agent's view of the server connection. Events that change server state
/// are written to the SQLite outbox *before* sending, and removed only when the
/// server acknowledges them (status ok/rejected). Unacknowledged events are
/// replayed in order after reconnecting or restarting; the server deduplicates
/// by event_id and re-sends the original ack.
/// </summary>
public sealed class AgentLink
{
    private readonly LocalStore _store;
    private readonly ILogger<AgentLink> _log;
    private readonly ConcurrentDictionary<string, TaskCompletionSource<Ack>> _pending = new();
    private readonly SemaphoreSlim _sendLock = new(1, 1);
    private ClientWebSocket? _ws;

    public AgentLink(LocalStore store, ILogger<AgentLink> log)
    {
        _store = store;
        _log = log;
    }

    public bool IsConnected => _ws?.State == WebSocketState.Open;
    public int HeartbeatSeconds { get; set; } = 25;

    internal void Attach(ClientWebSocket ws) => _ws = ws;

    internal void Detach() => _ws = null;

    private async Task<bool> SendRawAsync(string type, JsonObject payload, CancellationToken ct)
    {
        var ws = _ws;
        if (ws is null || ws.State != WebSocketState.Open) return false;
        var bytes = Encoding.UTF8.GetBytes(Json.Envelope(type, payload));
        await _sendLock.WaitAsync(ct);
        try
        {
            await ws.SendAsync(bytes, WebSocketMessageType.Text, true, ct);
            return true;
        }
        catch (Exception ex) when (ex is WebSocketException or ObjectDisposedException or InvalidOperationException)
        {
            _log.LogDebug(ex, "send failed for {Type}", type);
            return false;
        }
        finally
        {
            _sendLock.Release();
        }
    }

    /// <summary>Fire-and-forget message (heartbeats, progress). Not persisted.</summary>
    public async Task SendEphemeralAsync(string type, JsonObject payload, CancellationToken ct)
    {
        payload["event_id"] ??= Guid.NewGuid().ToString("N");
        await SendRawAsync(type, payload, ct);
    }

    /// <summary>
    /// Sends an event and returns a task that completes with the server's ack.
    /// persist=true: survives disconnects/restarts via the outbox.
    /// </summary>
    public async Task<Task<Ack>> SendEventAsync(string type, JsonObject payload, bool persist, CancellationToken ct)
    {
        var eventId = Guid.NewGuid().ToString("N");
        payload["event_id"] = eventId;
        var tcs = new TaskCompletionSource<Ack>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pending[eventId] = tcs;
        if (persist) await _store.EnqueueAsync(eventId, type, payload);
        if (await SendRawAsync(type, payload, ct) && persist) await _store.MarkSentAsync(eventId);
        return tcs.Task;
    }

    /// <summary>Convenience: send and wait for the ack, with a timeout.</summary>
    public async Task<Ack?> RequestAsync(string type, JsonObject payload, bool persist, TimeSpan timeout,
        CancellationToken ct)
    {
        var ackTask = await SendEventAsync(type, payload, persist, ct);
        var done = await Task.WhenAny(ackTask, Task.Delay(timeout, ct));
        return done == ackTask ? await ackTask : null;
    }

    internal async Task OnAckAsync(JsonObject payload)
    {
        var ack = Ack.From(payload);
        if (ack.Status == "error" && ack.Retryable)
        {
            _log.LogWarning("Server could not process {EventId} yet; it stays in the outbox", ack.EventId);
            return; // replayed later by the outbox pump
        }
        await _store.RemoveAsync(ack.EventId);
        if (_pending.TryRemove(ack.EventId, out var tcs)) tcs.TrySetResult(ack);
    }

    /// <summary>Re-sends outbox events in insertion order.</summary>
    internal async Task ReplayOutboxAsync(TimeSpan? olderThan, CancellationToken ct)
    {
        foreach (var item in await _store.PendingAsync(olderThan))
        {
            if (!IsConnected) return;
            if (await SendRawAsync(item.Type, item.Payload, ct)) await _store.MarkSentAsync(item.EventId);
        }
    }
}
