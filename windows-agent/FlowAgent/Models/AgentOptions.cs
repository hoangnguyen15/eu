namespace FlowAgent.Models;

public sealed class AgentOptions
{
    public const string Section = "Agent";

    /// <summary>Base URL of the mTLS agent gateway, e.g. https://gateway.example.com:8443</summary>
    public string ServerBaseUrl { get; set; } = "";
    public string WebSocketPath { get; set; } = "/ws/v1/agent";

    /// <summary>CA that issued the gateway's TLS certificate. Empty = OS trust store.</summary>
    public string? CaCertificatePath { get; set; }

    public string DataDirectory { get; set; } = "";

    /// <summary>Single-use token, only needed for the first start (enrollment).</summary>
    public string? EnrollmentToken { get; set; }

    public int Capacity { get; set; } = 1;
    public int HeartbeatSeconds { get; set; } = 25;
    public int SessionReportMinutes { get; set; } = 5;
    public int AcceptTimeoutSeconds { get; set; } = 10;
    public List<string> Providers { get; set; } = new() { "mock" };
    public List<AccountProfile> Accounts { get; set; } = new();
    public MockOptions Mock { get; set; } = new();
    public ApprovedOptions Approved { get; set; } = new();
    public GoogleFlowOptions GoogleFlow { get; set; } = new();

    public string ResolvedDataDirectory =>
        string.IsNullOrWhiteSpace(DataDirectory)
            ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "FlowAgent")
            : DataDirectory;
}

/// <summary>
/// A provider account whose browser/app profile lives on THIS machine. Only an
/// opaque profile reference is configured; credentials, cookies or session
/// tokens are never read, exported or sent anywhere by the agent.
/// </summary>
public sealed class AccountProfile
{
    public string GoogleEmail { get; set; } = "";
    public string ProfileRef { get; set; } = "";
    public string Provider { get; set; } = "mock";
}

public sealed class MockOptions
{
    public int LatencyMilliseconds { get; set; } = 800;
}

public sealed class ApprovedOptions
{
    public string BaseUrl { get; set; } = "";
    public string ApiKeyEnvironmentVariable { get; set; } = "FLOWAGENT_APPROVED_API_KEY";
}

public sealed class GoogleFlowOptions
{
    /// <summary>Has no effect: the connector has no implementation until an approved integration exists.</summary>
    public bool Enabled { get; set; }
}
