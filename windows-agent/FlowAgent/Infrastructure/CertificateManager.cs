using System.Net.Security;
using System.Runtime.Versioning;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using FlowAgent.Models;
using Microsoft.Extensions.Options;

namespace FlowAgent.Infrastructure;

/// <summary>
/// Owns the agent's mTLS client identity and the trust anchor for the gateway.
/// The private key is generated locally and never leaves the machine; on
/// Windows the stored PFX is additionally protected with DPAPI (LocalMachine).
/// </summary>
public sealed class CertificateManager
{
    private readonly AgentOptions _opt;
    private readonly ILogger<CertificateManager> _log;
    private X509Certificate2? _clientCert;
    private X509Certificate2? _ca;

    public CertificateManager(IOptions<AgentOptions> opt, ILogger<CertificateManager> log)
    {
        _opt = opt.Value;
        _log = log;
    }

    private string PfxPath => Path.Combine(_opt.ResolvedDataDirectory, "agent-identity.bin");
    private string IdPath => Path.Combine(_opt.ResolvedDataDirectory, "workstation-id.txt");

    public bool HasIdentity => File.Exists(PfxPath);

    public string? WorkstationId => File.Exists(IdPath) ? File.ReadAllText(IdPath).Trim() : null;

    public X509Certificate2? CaCertificate
    {
        get
        {
            if (_ca is null && !string.IsNullOrWhiteSpace(_opt.CaCertificatePath) && File.Exists(_opt.CaCertificatePath))
                _ca = X509Certificate2.CreateFromPem(File.ReadAllText(_opt.CaCertificatePath));
            return _ca;
        }
    }

    public X509Certificate2 ClientCertificate =>
        _clientCert ??= LoadIdentity() ?? throw new InvalidOperationException("Agent is not enrolled");

    /// <summary>Validates the gateway certificate against the pinned CA (if configured).</summary>
    public bool ValidateServer(object sender, X509Certificate? cert, X509Chain? chain, SslPolicyErrors errors)
    {
        var ca = CaCertificate;
        if (ca is null) return errors == SslPolicyErrors.None; // OS trust store
        if (cert is null) return false;
        if ((errors & SslPolicyErrors.RemoteCertificateNameMismatch) != 0) return false;
        using var custom = new X509Chain();
        custom.ChainPolicy.TrustMode = X509ChainTrustMode.CustomRootTrust;
        custom.ChainPolicy.CustomTrustStore.Add(ca);
        custom.ChainPolicy.RevocationMode = X509RevocationMode.NoCheck;
        if (chain is not null)
            foreach (var el in chain.ChainElements) custom.ChainPolicy.ExtraStore.Add(el.Certificate);
        return custom.Build(new X509Certificate2(cert));
    }

    public (string CsrPem, ECDsa Key) CreateCsr()
    {
        var key = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        var req = new CertificateRequest($"CN={Environment.MachineName}", key, HashAlgorithmName.SHA256);
        return (req.CreateSigningRequestPem(), key);
    }

    public void SaveIdentity(string certificatePem, ECDsa key, string workstationId, string? caPem)
    {
        Directory.CreateDirectory(_opt.ResolvedDataDirectory);
        using var pub = X509Certificate2.CreateFromPem(certificatePem);
        using var withKey = pub.CopyWithPrivateKey(key);
        var pfx = withKey.Export(X509ContentType.Pkcs12);
        var stored = OperatingSystem.IsWindows() ? ProtectWindows(pfx) : pfx;
        WriteRestricted(PfxPath, stored);
        File.WriteAllText(IdPath, workstationId);
        if (caPem is not null && !string.IsNullOrWhiteSpace(_opt.CaCertificatePath) && !File.Exists(_opt.CaCertificatePath))
            File.WriteAllText(_opt.CaCertificatePath, caPem);
        _clientCert = null;
        _log.LogInformation("Enrollment stored for workstation {WorkstationId}", workstationId);
    }

    private X509Certificate2? LoadIdentity()
    {
        if (!File.Exists(PfxPath)) return null;
        var bytes = File.ReadAllBytes(PfxPath);
        if (OperatingSystem.IsWindows())
        {
            bytes = UnprotectWindows(bytes);
            // SChannel cannot use EphemeralKeySet keys for client authentication;
            // UserKeySet (non-persisted) loads the key into the service account's
            // key store for the lifetime of the certificate object only.
            return new X509Certificate2(bytes, (string?)null, X509KeyStorageFlags.UserKeySet);
        }
        return new X509Certificate2(bytes, (string?)null, X509KeyStorageFlags.EphemeralKeySet);
    }

    [SupportedOSPlatform("windows")]
    private static byte[] ProtectWindows(byte[] data) =>
        ProtectedData.Protect(data, null, DataProtectionScope.LocalMachine);

    [SupportedOSPlatform("windows")]
    private static byte[] UnprotectWindows(byte[] data) =>
        ProtectedData.Unprotect(data, null, DataProtectionScope.LocalMachine);

    private static void WriteRestricted(string path, byte[] data)
    {
        File.WriteAllBytes(path, data);
        if (!OperatingSystem.IsWindows())
            File.SetUnixFileMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite);
        // On Windows, install.ps1 restricts the data directory ACL to SYSTEM + Administrators.
    }
}
