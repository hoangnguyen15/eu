using System.Text.Json.Nodes;
using FlowAgent.Models;
using Microsoft.Data.Sqlite;

namespace FlowAgent.Infrastructure;

/// <summary>
/// SQLite-backed durable state:
///  * outbox  – state-changing events not yet acknowledged by the server
///              (replayed in order after reconnect / restart);
///  * tasks   – local execution state per task, used on restart to decide
///              between "not started" (safe to hand back), "outcome unknown"
///              (never retried) and "results on disk" (resume upload).
/// </summary>
public sealed class LocalStore : IDisposable
{
    private readonly string _connString;
    private readonly SemaphoreSlim _lock = new(1, 1);

    public LocalStore(string dataDirectory)
    {
        Directory.CreateDirectory(dataDirectory);
        _connString = new SqliteConnectionStringBuilder
        {
            DataSource = Path.Combine(dataDirectory, "agent.db"),
            Mode = SqliteOpenMode.ReadWriteCreate,
            Cache = SqliteCacheMode.Private,
        }.ToString();
        Execute("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS outbox (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_sent_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                generation_id TEXT NOT NULL,
                state TEXT NOT NULL,
                detail TEXT,
                updated_at TEXT NOT NULL
            );
            """);
    }

    private void Execute(string sql)
    {
        using var conn = new SqliteConnection(_connString);
        conn.Open();
        using var cmd = conn.CreateCommand();
        cmd.CommandText = sql;
        cmd.ExecuteNonQuery();
    }

    private async Task<T> WithConnection<T>(Func<SqliteConnection, Task<T>> fn)
    {
        await _lock.WaitAsync();
        try
        {
            await using var conn = new SqliteConnection(_connString);
            await conn.OpenAsync();
            return await fn(conn);
        }
        finally
        {
            _lock.Release();
        }
    }

    // ---------------------------------------------------------------- outbox
    public Task EnqueueAsync(string eventId, string type, JsonObject payload) => WithConnection(async c =>
    {
        await using var cmd = c.CreateCommand();
        cmd.CommandText = "INSERT OR IGNORE INTO outbox(event_id, type, payload, created_at) VALUES ($e, $t, $p, $c)";
        cmd.Parameters.AddWithValue("$e", eventId);
        cmd.Parameters.AddWithValue("$t", type);
        cmd.Parameters.AddWithValue("$p", payload.ToJsonString());
        cmd.Parameters.AddWithValue("$c", DateTimeOffset.UtcNow.ToString("O"));
        return await cmd.ExecuteNonQueryAsync();
    });

    public Task RemoveAsync(string eventId) => WithConnection(async c =>
    {
        await using var cmd = c.CreateCommand();
        cmd.CommandText = "DELETE FROM outbox WHERE event_id = $e";
        cmd.Parameters.AddWithValue("$e", eventId);
        return await cmd.ExecuteNonQueryAsync();
    });

    public Task MarkSentAsync(string eventId) => WithConnection(async c =>
    {
        await using var cmd = c.CreateCommand();
        cmd.CommandText = "UPDATE outbox SET last_sent_at = $now, attempts = attempts + 1 WHERE event_id = $e";
        cmd.Parameters.AddWithValue("$e", eventId);
        cmd.Parameters.AddWithValue("$now", DateTimeOffset.UtcNow.ToString("O"));
        return await cmd.ExecuteNonQueryAsync();
    });

    public sealed record OutboxItem(string EventId, string Type, JsonObject Payload, DateTimeOffset? LastSentAt);

    /// <summary>Pending events in insertion order. With <paramref name="olderThan"/>, only those not sent recently.</summary>
    public Task<List<OutboxItem>> PendingAsync(TimeSpan? olderThan = null) => WithConnection(async c =>
    {
        await using var cmd = c.CreateCommand();
        cmd.CommandText = "SELECT event_id, type, payload, last_sent_at FROM outbox ORDER BY seq";
        var list = new List<OutboxItem>();
        await using var r = await cmd.ExecuteReaderAsync();
        var cutoff = DateTimeOffset.UtcNow - (olderThan ?? TimeSpan.Zero);
        while (await r.ReadAsync())
        {
            DateTimeOffset? last = r.IsDBNull(3) ? null : DateTimeOffset.Parse(r.GetString(3));
            if (olderThan is not null && last is not null && last > cutoff) continue;
            list.Add(new OutboxItem(r.GetString(0), r.GetString(1), JsonNode.Parse(r.GetString(2))!.AsObject(), last));
        }
        return list;
    });

    public Task<int> OutboxCountAsync() => WithConnection(async c =>
    {
        await using var cmd = c.CreateCommand();
        cmd.CommandText = "SELECT COUNT(*) FROM outbox";
        return Convert.ToInt32(await cmd.ExecuteScalarAsync());
    });

    // ----------------------------------------------------------------- tasks
    public Task SetTaskStateAsync(string taskId, string generationId, string state, string? detail = null) =>
        WithConnection(async c =>
        {
            await using var cmd = c.CreateCommand();
            cmd.CommandText = """
                INSERT INTO tasks(task_id, generation_id, state, detail, updated_at) VALUES ($t, $g, $s, $d, $u)
                ON CONFLICT(task_id) DO UPDATE SET state = $s, detail = COALESCE($d, detail), updated_at = $u
                """;
            cmd.Parameters.AddWithValue("$t", taskId);
            cmd.Parameters.AddWithValue("$g", generationId);
            cmd.Parameters.AddWithValue("$s", state);
            cmd.Parameters.AddWithValue("$d", (object?)detail ?? DBNull.Value);
            cmd.Parameters.AddWithValue("$u", DateTimeOffset.UtcNow.ToString("O"));
            return await cmd.ExecuteNonQueryAsync();
        });

    public Task DeleteTaskAsync(string taskId) => WithConnection(async c =>
    {
        await using var cmd = c.CreateCommand();
        cmd.CommandText = "DELETE FROM tasks WHERE task_id = $t";
        cmd.Parameters.AddWithValue("$t", taskId);
        return await cmd.ExecuteNonQueryAsync();
    });

    public sealed record TaskRow(string TaskId, string GenerationId, string State, string? Detail);

    public Task<List<TaskRow>> TasksAsync() => WithConnection(async c =>
    {
        await using var cmd = c.CreateCommand();
        cmd.CommandText = "SELECT task_id, generation_id, state, detail FROM tasks ORDER BY updated_at";
        var list = new List<TaskRow>();
        await using var r = await cmd.ExecuteReaderAsync();
        while (await r.ReadAsync())
            list.Add(new TaskRow(r.GetString(0), r.GetString(1), r.GetString(2), r.IsDBNull(3) ? null : r.GetString(3)));
        return list;
    });

    public void Dispose() => _lock.Dispose();
}

public static class TaskState
{
    public const string Accepting = "ACCEPTING";       // TASK_ACCEPT sent, ack pending
    public const string Accepted = "ACCEPTED";         // server confirmed; provider NOT invoked yet
    public const string Started = "STARTED";           // provider invoked: outcome unknown if we crash
    public const string ProviderDone = "PROVIDER_DONE"; // images persisted locally; upload can resume
    public const string Uploaded = "UPLOADED";
    public const string Completed = "COMPLETED";
    public const string Failed = "FAILED";
}
