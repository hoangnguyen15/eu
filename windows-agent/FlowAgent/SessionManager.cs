using System.Collections.Concurrent;
using System.Text.Json.Nodes;
using FlowAgent.Models;
using FlowAgent.Providers;
using Microsoft.Extensions.Options;

namespace FlowAgent;

/// <summary>
/// Reports the state of the provider profiles hosted on this machine
/// (SESSION_STATUS). It only reports status; it never reads, copies or
/// transmits passwords, cookies or session tokens, and it never switches
/// accounts. When a provider asks for re-authentication the profile is marked
/// AUTH_REQUIRED until an operator signs in again on this machine.
/// </summary>
public sealed class SessionManager
{
    private readonly AgentOptions _opt;
    private readonly ProviderRegistry _providers;
    private readonly AgentLink _link;
    private readonly ILogger<SessionManager> _log;
    private readonly ConcurrentDictionary<string, string> _overrides = new(StringComparer.OrdinalIgnoreCase);

    public SessionManager(IOptions<AgentOptions> opt, ProviderRegistry providers, AgentLink link,
        ILogger<SessionManager> log)
    {
        _opt = opt.Value;
        _providers = providers;
        _link = link;
        _log = log;
    }

    public void MarkAuthRequired(string? profileRef)
    {
        if (string.IsNullOrEmpty(profileRef)) return;
        _overrides[profileRef] = "AUTH_REQUIRED";
        _log.LogWarning("Profile {Profile} requires re-authentication by an operator on this machine", profileRef);
    }

    /// <summary>
    /// Operator asked (via the admin console) to re-check sessions, typically
    /// after signing in again manually on this machine: drop local
    /// AUTH_REQUIRED overrides and report fresh provider-checked status.
    /// </summary>
    public async Task RecheckAsync(CancellationToken ct)
    {
        _overrides.Clear();
        _log.LogInformation("Session re-check requested by server");
        await ReportAsync(ct);
    }

    public async Task<List<SessionReport>> BuildReportAsync(CancellationToken ct)
    {
        var reports = new List<SessionReport>();
        foreach (var a in _opt.Accounts)
        {
            string status;
            if (_overrides.TryGetValue(a.ProfileRef, out var forced)) status = forced;
            else if (_providers.Get(a.Provider) is { } p) status = await p.CheckSessionAsync(a.ProfileRef, ct);
            else status = "DISABLED";
            reports.Add(new SessionReport(a.GoogleEmail, a.ProfileRef, a.Provider, status));
        }
        return reports;
    }

    public async Task ReportAsync(CancellationToken ct)
    {
        var reports = await BuildReportAsync(ct);
        if (reports.Count == 0) return;
        var payload = new JsonObject { ["sessions"] = JsonSerializerNode(reports) };
        var ack = await _link.RequestAsync(MsgType.SessionStatus, payload, persist: false, TimeSpan.FromSeconds(15), ct);
        if (ack?.Result["unregistered"] is JsonArray { Count: > 0 } unknown)
            _log.LogWarning("Server does not recognise these accounts as authorized: {Accounts}. " +
                            "An admin must register and authorize them first.", unknown.ToJsonString());
    }

    private static JsonNode JsonSerializerNode(List<SessionReport> reports) =>
        System.Text.Json.JsonSerializer.SerializeToNode(reports, Json.Options)!;
}
