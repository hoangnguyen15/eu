using System.Text.Json.Nodes;
using FlowAgent.Models;
using Microsoft.Extensions.Options;

namespace FlowAgent;

/// <summary>Sends HEARTBEAT every 20–30 s (interval suggested by the server at REGISTER).</summary>
public sealed class HeartbeatService : BackgroundService
{
    private readonly AgentLink _link;
    private readonly JobExecutor _jobs;
    private readonly AgentOptions _opt;

    public HeartbeatService(AgentLink link, JobExecutor jobs, IOptions<AgentOptions> opt)
    {
        _link = link;
        _jobs = jobs;
        _opt = opt.Value;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            var seconds = Math.Clamp(_link.HeartbeatSeconds > 0 ? _link.HeartbeatSeconds : _opt.HeartbeatSeconds, 1, 30);
            try { await Task.Delay(TimeSpan.FromSeconds(seconds), stoppingToken); }
            catch (OperationCanceledException) { break; }
            if (!_link.IsConnected) continue;
            var active = new JsonArray(_jobs.ActiveTaskIds().Select(id => (JsonNode?)JsonValue.Create(id)).ToArray());
            await _link.SendEphemeralAsync(MsgType.Heartbeat, new JsonObject
            {
                ["active_tasks"] = active,
                ["free_slots"] = _jobs.FreeSlots,
            }, stoppingToken);
        }
    }
}
