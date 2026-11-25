using System.Buffers.Binary;
using System.IO.Compression;
using System.Text.Json.Nodes;
using FlowAgent.Infrastructure;
using FlowAgent.Models;
using FlowAgent.Providers;
using Microsoft.Extensions.Options;
using Xunit;

namespace FlowAgent.Tests;

public class PngEncoderTests
{
    [Fact]
    public void EncodesValidPngStructureAndPixels()
    {
        var png = PngEncoder.EncodeRgb(3, 2, (x, y) => ((byte)(x * 10), (byte)(y * 20), 7));
        Assert.Equal(new byte[] { 0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A }, png[..8]);
        Assert.Equal("IHDR", System.Text.Encoding.ASCII.GetString(png, 12, 4));
        Assert.Equal(3, BinaryPrimitives.ReadInt32BigEndian(png.AsSpan(16)));
        Assert.Equal(2, BinaryPrimitives.ReadInt32BigEndian(png.AsSpan(20)));
        // Decompress IDAT and check the raw scanlines.
        var idatLen = BinaryPrimitives.ReadInt32BigEndian(png.AsSpan(33));
        Assert.Equal("IDAT", System.Text.Encoding.ASCII.GetString(png, 37, 4));
        using var z = new ZLibStream(new MemoryStream(png, 41, idatLen), CompressionMode.Decompress);
        using var raw = new MemoryStream();
        z.CopyTo(raw);
        var bytes = raw.ToArray();
        Assert.Equal(2 * (1 + 3 * 3), bytes.Length);
        Assert.Equal(new byte[] { 0, 0, 0, 7, 10, 0, 7, 20, 0, 7 }, bytes[..10]);
        Assert.Equal("IEND", System.Text.Encoding.ASCII.GetString(png, png.Length - 8, 4));
    }
}

public class ProviderTests
{
    private static MockImageProvider Mock() =>
        new(Options.Create(new AgentOptions { Mock = new MockOptions { LatencyMilliseconds = 10 } }));

    [Fact]
    public async Task MockProducesRequestedImages()
    {
        var p = Mock();
        var job = await p.GenerateAsync(new ProviderRequest("t", "city", "16:9", 2, [], null), default);
        ProviderJobStatus st;
        do { st = await p.GetStatusAsync(job, default); await Task.Delay(5); }
        while (st.State is ProviderJobState.Running or ProviderJobState.Pending);
        Assert.Equal(ProviderJobState.Succeeded, st.State);
        var res = await p.CollectResultAsync(job, default);
        Assert.Equal(2, res.Images.Count);
        Assert.All(res.Images, i => Assert.Equal("image/png", i.MimeType));
        Assert.Equal(768, BinaryPrimitives.ReadInt32BigEndian(res.Images[0].Data.AsSpan(16)));
        Assert.NotEqual(res.Images[0].Data, res.Images[1].Data);
    }

    [Theory]
    [InlineData("[mock:auth]", "AUTH_REQUIRED")]
    [InlineData("[mock:limit]", "PROVIDER_LIMITED")]
    [InlineData("[mock:review]", "REVIEW_REQUIRED")]
    public async Task MockFailureModes(string token, string code)
    {
        var ex = await Assert.ThrowsAsync<ProviderException>(() =>
            Mock().GenerateAsync(new ProviderRequest("t", $"x {token}", "1:1", 1, [], null), default));
        Assert.Equal(code, ex.Code);
    }

    [Fact]
    public async Task GoogleFlowConnectorIsDisabled()
    {
        var c = new GoogleFlowConnector();
        Assert.False(c.Enabled);
        await Assert.ThrowsAsync<ProviderException>(() =>
            c.GenerateAsync(new ProviderRequest("t", "x", "1:1", 1, [], null), default));
        Assert.Equal("DISABLED", await c.CheckSessionAsync("p", default));
        var registry = new ProviderRegistry([c, Mock()]);
        Assert.Null(registry.Get("google_flow"));
        Assert.Equal(["mock"], registry.EnabledNames(["mock", "google_flow"]));
    }

    [Fact]
    public void ApprovedProviderDisabledWithoutConfiguration()
    {
        var p = new ApprovedImageProvider(Options.Create(new AgentOptions()));
        Assert.False(p.Enabled);
    }
}

public class LocalStoreTests : IDisposable
{
    private readonly string _dir = Path.Combine(Path.GetTempPath(), "flowagent-test-" + Guid.NewGuid().ToString("N"));

    [Fact]
    public async Task OutboxIsDurableOrderedAndIdempotent()
    {
        using (var s = new LocalStore(_dir))
        {
            await s.EnqueueAsync("e1", "TASK_STARTED", new JsonObject { ["task_id"] = "t" });
            await s.EnqueueAsync("e2", "UPLOAD_REQUEST", new JsonObject { ["task_id"] = "t" });
            await s.EnqueueAsync("e1", "TASK_STARTED", new JsonObject { ["task_id"] = "dup" });
        }
        using (var s = new LocalStore(_dir)) // survives "restart"
        {
            var pending = await s.PendingAsync();
            Assert.Equal(["e1", "e2"], pending.Select(p => p.EventId));
            Assert.Equal("t", pending[0].Payload["task_id"]!.GetValue<string>());
            await s.MarkSentAsync("e1");
            Assert.Equal(["e2"], (await s.PendingAsync(TimeSpan.FromMinutes(1))).Select(p => p.EventId));
            await s.RemoveAsync("e1");
            Assert.Equal(1, await s.OutboxCountAsync());
        }
    }

    [Fact]
    public async Task TaskStateUpserts()
    {
        using var s = new LocalStore(_dir);
        await s.SetTaskStateAsync("t1", "g1", TaskState.Accepted);
        await s.SetTaskStateAsync("t1", "g1", TaskState.Started, "detail");
        var rows = await s.TasksAsync();
        Assert.Single(rows);
        Assert.Equal(TaskState.Started, rows[0].State);
        Assert.Equal("detail", rows[0].Detail);
        await s.DeleteTaskAsync("t1");
        Assert.Empty(await s.TasksAsync());
    }

    public void Dispose()
    {
        Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools();
        try { Directory.Delete(_dir, true); } catch (IOException) { }
    }
}

public class MessageTests
{
    [Fact]
    public void ParsesOfferAndAck()
    {
        var offer = TaskOffer.Parse(JsonNode.Parse("""
            {"task_id":"t","generation_id":"g","attempt":1,"slot_index":0,"provider":"mock","prompt":"p",
             "aspect_ratio":"1:1","number_of_images":2,"reference_image_urls":[],"profile_ref":"pr",
             "account_email":"a@example.com","offer_expires_at":"2030-01-01T00:00:00+00:00"}
            """)!.AsObject());
        Assert.Equal(2, offer.NumberOfImages);
        Assert.Equal("pr", offer.ProfileRef);
        var ack = Ack.From(JsonNode.Parse("""{"event_id":"e","status":"rejected","result":{"reason":"X"}}""")!.AsObject());
        Assert.False(ack.Ok);
        Assert.Equal("X", ack.Reason);
        var env = JsonNode.Parse(Json.Envelope("HEARTBEAT", new JsonObject()))!;
        Assert.Equal("HEARTBEAT", env["type"]!.GetValue<string>());
    }
}

public class PollingTests
{
    [Theory]
    [InlineData(0.5, 50)]
    [InlineData(5, 200)]
    [InlineData(30, 500)]
    [InlineData(120, 1000)]
    public void PollDelayBacksOffGradually(double elapsedSeconds, int expectedMs) =>
        Assert.Equal(expectedMs, JobExecutor.PollDelay(TimeSpan.FromSeconds(elapsedSeconds)).TotalMilliseconds);
}
