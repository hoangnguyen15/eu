using System.Text.Json;
using System.Text.Json.Nodes;
using FlowAgent.Infrastructure;
using FlowAgent.Models;

namespace FlowAgent;

/// <summary>
/// UPLOAD_REQUEST -> PUT each image to its pre-signed URL (directly to object
/// storage) -> UPLOAD_COMPLETED (server downloads + verifies MIME, size,
/// SHA-256, makes thumbnails) -> TASK_COMPLETED. The server only marks the
/// generation COMPLETED after every image has been stored and verified.
/// </summary>
public sealed class ResultUploader
{
    private readonly AgentLink _link;
    private readonly ApiClient _api;
    private readonly LocalStore _store;
    private readonly ILogger<ResultUploader> _log;

    public ResultUploader(AgentLink link, ApiClient api, LocalStore store, ILogger<ResultUploader> log)
    {
        _link = link;
        _api = api;
        _store = store;
        _log = log;
    }

    public async Task UploadAndCompleteAsync(string taskId, string generationId, ResultManifest manifest,
        DateTimeOffset deadline, CancellationToken ct)
    {
        var specs = manifest.Files.Select(f => new UploadFileSpec(f.Index, f.MimeType, f.Size, f.Sha256)).ToList();
        var request = new JsonObject
        {
            ["task_id"] = taskId,
            ["files"] = JsonSerializer.SerializeToNode(specs, Json.Options),
        };
        var reqAck = await _link.SendEventAsync(MsgType.UploadRequest, request, persist: true, ct);
        await JobExecutor.AwaitAck(reqAck, deadline, ct);
        var targets = (await reqAck).Result["uploads"].Deserialize<List<UploadTarget>>(Json.Options)
                      ?? throw new UploadRejectedException("no upload targets");

        foreach (var target in targets)
        {
            var file = manifest.Files.Single(f => f.Index == target.Index);
            for (var attempt = 1; ; attempt++)
            {
                try
                {
                    await _api.UploadAsync(target, file.Path, ct);
                    break;
                }
                catch (HttpRequestException ex) when (attempt < 4)
                {
                    _log.LogWarning("Upload attempt {Attempt} for {Key} failed: {Message}", attempt, target.StorageKey,
                        ex.Message);
                    await Task.Delay(TimeSpan.FromSeconds(Math.Pow(2, attempt)), ct);
                }
            }
        }

        var uploaded = targets.Select(t =>
        {
            var f = manifest.Files.Single(x => x.Index == t.Index);
            return new UploadedFile(f.Index, f.MimeType, f.Size, f.Sha256, t.StorageKey, null, null);
        }).ToList();
        var doneAck = await _link.SendEventAsync(MsgType.UploadCompleted, new JsonObject
        {
            ["task_id"] = taskId,
            ["files"] = JsonSerializer.SerializeToNode(uploaded, Json.Options),
        }, persist: true, ct);
        await JobExecutor.AwaitAck(doneAck, deadline, ct);
        await _store.SetTaskStateAsync(taskId, generationId, TaskState.Uploaded);

        var completeAck = await _link.SendEventAsync(MsgType.TaskCompleted, new JsonObject
        {
            ["task_id"] = taskId,
            ["provider"] = manifest.Provider,
            ["model"] = manifest.Model,
            ["provider_metadata"] = JsonSerializer.SerializeToNode(manifest.Metadata, Json.Options),
        }, persist: true, ct);
        await JobExecutor.AwaitAck(completeAck, deadline, ct);
        await _store.SetTaskStateAsync(taskId, generationId, TaskState.Completed);
        _log.LogInformation("Task {Task} completed ({Count} image(s))", taskId, manifest.Files.Count);

        foreach (var f in manifest.Files)
        {
            try { File.Delete(f.Path); } catch (IOException) { }
        }
        await _store.DeleteTaskAsync(taskId);
    }
}
