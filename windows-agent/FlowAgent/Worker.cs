using System.Net.WebSockets;
using System.Text;
using System.Text.Json.Nodes;
using FlowAgent.Infrastructure;
using FlowAgent.Models;
using FlowAgent.Providers;
using Microsoft.Extensions.Options;

namespace FlowAgent;

/// <summary>
/// Main service loop: enroll if needed, keep one outbound WSS (mTLS) connection
/// to the gateway, reconnect with exponential backoff + jitter, register,
/// replay the outbox and dispatch server messages.
/// </summary>
public sealed class Worker : BackgroundService
{
    private readonly AgentOptions _opt;
    private readonly AgentLink _link;
    private readonly CertificateManager _certs;
    private readonly ApiClient _api;
    private readonly JobExecutor _jobs;
    private readonly SessionManager _sessions;
    private readonly ProviderRegistry _providers;
    private readonly ILogger<Worker> _log;
    private CancellationToken _serviceStopping;

    public Worker(IOptions<AgentOptions> opt, AgentLink link, CertificateManager certs, ApiClient api,
        JobExecutor jobs, SessionManager sessions, ProviderRegistry providers, ILogger<Worker> log)
    {
        _opt = opt.Value;
        _link = link;
        _certs = certs;
        _api = api;
        _jobs = jobs;
        _sessions = sessions;
        _providers = providers;
        _log = log;
    }

    public static string AgentVersion =>
        typeof(Worker).Assembly.GetName().Version?.ToString(3) ?? "0.0.0";

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        _serviceStopping = stoppingToken;
        await EnsureEnrolledAsync(stoppingToken);
        await _jobs.RecoverAsync(stoppingToken);

        var attempt = 0;
        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                await RunConnectionAsync(stoppingToken);
                attempt = 0;
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception ex)
            {
                _log.LogWarning("Connection lost: {Message}", ex.Message);
            }
            finally
            {
                _link.Detach();
            }
            attempt++;
            var backoff = Math.Min(60, Math.Pow(2, Math.Min(attempt, 6)));
            var delay = TimeSpan.FromSeconds(backoff * (0.5 + Random.Shared.NextDouble() / 2));
            _log.LogInformation("Reconnecting in {Delay:F1}s", delay.TotalSeconds);
            try { await Task.Delay(delay, stoppingToken); } catch (OperationCanceledException) { break; }
        }
    }

    private async Task EnsureEnrolledAsync(CancellationToken ct)
    {
        while (!_certs.HasIdentity && !ct.IsCancellationRequested)
        {
            try
            {
                await _api.EnrollAsync(ct);
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                _log.LogError("Enrollment failed: {Message}. Retrying in 30s", ex.Message);
                await Task.Delay(TimeSpan.FromSeconds(30), ct);
            }
        }
    }

    private async Task RunConnectionAsync(CancellationToken ct)
    {
        using var ws = new ClientWebSocket();
        ws.Options.ClientCertificates.Add(_certs.ClientCertificate);
        ws.Options.RemoteCertificateValidationCallback = _certs.ValidateServer;
        ws.Options.KeepAliveInterval = TimeSpan.FromSeconds(20);
        var uri = new UriBuilder(_opt.ServerBaseUrl)
        {
            Scheme = _opt.ServerBaseUrl.StartsWith("https", StringComparison.OrdinalIgnoreCase) ? "wss" : "ws",
            Path = _opt.WebSocketPath,
        }.Uri;
        _log.LogInformation("Connecting to {Uri}", uri);
        using (var connectCts = CancellationTokenSource.CreateLinkedTokenSource(ct))
        {
            connectCts.CancelAfter(TimeSpan.FromSeconds(30));
            await ws.ConnectAsync(uri, connectCts.Token);
        }
        _link.Attach(ws);

        using var connCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        var reader = ReadLoopAsync(ws, connCts.Token);

        var register = new RegisterPayload(Environment.MachineName, AgentVersion, _opt.Capacity,
            _providers.EnabledNames(_opt.Providers).ToList(), _jobs.ActiveTaskIds().ToList());
        var ack = await _link.RequestAsync(MsgType.Register, Json.ToObject(register), persist: false,
            TimeSpan.FromSeconds(15), ct) ?? throw new IOException("REGISTER was not acknowledged");
        if (!ack.Ok) throw new IOException($"REGISTER rejected: {ack.Reason}");
        _link.HeartbeatSeconds = ack.Result["heartbeat_interval_seconds"]?.GetValue<int>() ?? _opt.HeartbeatSeconds;
        _log.LogInformation("Registered as workstation {Id}", ack.Result["workstation_id"]);

        await _link.ReplayOutboxAsync(null, ct);
        await _sessions.ReportAsync(ct);
        var pump = OutboxPumpAsync(connCts.Token);

        try
        {
            await reader;
        }
        finally
        {
            connCts.Cancel();
            try { await pump; } catch (OperationCanceledException) { }
        }
    }

    private async Task OutboxPumpAsync(CancellationToken ct)
    {
        var lastSessionReport = DateTimeOffset.UtcNow;
        while (!ct.IsCancellationRequested)
        {
            await Task.Delay(TimeSpan.FromSeconds(15), ct);
            await _link.ReplayOutboxAsync(TimeSpan.FromSeconds(15), ct);
            if (DateTimeOffset.UtcNow - lastSessionReport > TimeSpan.FromMinutes(_opt.SessionReportMinutes))
            {
                await _sessions.ReportAsync(ct);
                lastSessionReport = DateTimeOffset.UtcNow;
            }
        }
    }

    private TimeSpan LivenessTimeout =>
        TimeSpan.FromSeconds(2 * Math.Clamp(_link.HeartbeatSeconds, 1, 30) + 5);

    private async Task ReadLoopAsync(ClientWebSocket ws, CancellationToken ct)
    {
        var buffer = new byte[64 * 1024];
        using var message = new MemoryStream();
        while (ws.State == WebSocketState.Open && !ct.IsCancellationRequested)
        {
            message.SetLength(0);
            WebSocketReceiveResult result;
            do
            {
                // Liveness watchdog: the server acks every heartbeat, so silence for
                // longer than 2 heartbeats means a half-open connection (NAT/firewall
                // drop, proxy crash). Abort and reconnect instead of hanging forever.
                using var silence = CancellationTokenSource.CreateLinkedTokenSource(ct);
                silence.CancelAfter(LivenessTimeout);
                try
                {
                    result = await ws.ReceiveAsync(buffer, silence.Token);
                }
                catch (OperationCanceledException) when (!ct.IsCancellationRequested)
                {
                    ws.Abort();
                    throw new IOException($"no message from server for {LivenessTimeout.TotalSeconds:F0}s; connection presumed dead");
                }
                if (result.MessageType == WebSocketMessageType.Close)
                {
                    _log.LogWarning("Server closed connection: {Status} {Description}", result.CloseStatus,
                        result.CloseStatusDescription);
                    return;
                }
                message.Write(buffer, 0, result.Count);
                if (message.Length > 4 * 1024 * 1024) throw new IOException("message too large");
            } while (!result.EndOfMessage);

            JsonObject? env;
            try { env = JsonNode.Parse(Encoding.UTF8.GetString(message.ToArray())) as JsonObject; }
            catch (Exception) { _log.LogWarning("Ignoring malformed server message"); continue; }
            if (env is null) continue;
            var type = env["type"]?.GetValue<string>();
            var payload = env["payload"] as JsonObject ?? new JsonObject();
            switch (type)
            {
                case MsgType.EventAck:
                    await _link.OnAckAsync(payload);
                    break;
                case MsgType.TaskOffer:
                    // Jobs live for the lifetime of the *service*, not of this socket:
                    // a dropped connection must not cancel work in progress.
                    _ = Task.Run(() => _jobs.OnOfferAsync(payload, _serviceStopping), CancellationToken.None);
                    break;
                case MsgType.TaskCancel:
                    _jobs.OnCancel(payload);
                    break;
                case MsgType.Error:
                    _log.LogWarning("Server error: {Payload}", payload.ToJsonString());
                    break;
                default:
                    _log.LogDebug("Unhandled message {Type}", type);
                    break;
            }
        }
    }
}
