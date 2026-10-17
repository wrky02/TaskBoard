using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace TaskBoard;
internal sealed class Rpc : IDisposable
{
    readonly Process process;
    readonly SemaphoreSlim gate = new(1, 1);
    int sequence;
    public Rpc(string[] args)
    {
        string root = AppContext.BaseDirectory;
        string? source = Environment.GetEnvironmentVariable("TASKBOARD_WORKER_SOURCE");
        var start = new ProcessStartInfo(source is null ? Path.Combine(root, "engine", "TaskBoard.Engine.exe") : (Environment.GetEnvironmentVariable("TASKBOARD_PYTHON") ?? "python"))
        { UseShellExecute=false, CreateNoWindow=true, RedirectStandardInput=true, RedirectStandardOutput=true, RedirectStandardError=true, StandardInputEncoding=new UTF8Encoding(false), StandardOutputEncoding=new UTF8Encoding(false), StandardErrorEncoding=Encoding.UTF8 };
        if (source is not null) start.ArgumentList.Add(source);
        start.ArgumentList.Add("--install-root"); start.ArgumentList.Add(root);
        for (int i=0;i<args.Length-1;i++) if(args[i]=="--data-root") { start.ArgumentList.Add("--data-root"); start.ArgumentList.Add(args[i+1]); }
        start.Environment["PYTHONUTF8"]="1";
        process=Process.Start(start) ?? throw new IOException("无法启动本地数据引擎");
        // Drain stderr without saving task content or credentials.
        process.ErrorDataReceived += (_,_)=>{}; process.BeginErrorReadLine();
    }
    public async Task<JsonObject> Call(string method, JsonObject? parameters=null)
    {
        await gate.WaitAsync();
        try {
            if(process.HasExited) throw new IOException("本地数据引擎已停止，请重启程序；未保存内容可从草稿恢复。");
            var id=++sequence;
            var request=new JsonObject { ["id"]=id, ["method"]=method, ["params"]=parameters?.DeepClone() ?? new JsonObject() };
            await process.StandardInput.WriteLineAsync(request.ToJsonString()); await process.StandardInput.FlushAsync();
            string? raw=await process.StandardOutput.ReadLineAsync();
            if(raw is null) throw new IOException("本地数据引擎意外退出，请重启程序。");
            var reply=JsonNode.Parse(raw)!.AsObject();
            if(reply["error"] is not null) throw new InvalidOperationException(reply["error"]!.ToString());
            if(reply["id"]?.GetValue<int>()!=id) throw new IOException("本地通信序号不一致");
            return reply["result"]!.AsObject();
        } finally { gate.Release(); }
    }
    public void Dispose()
    {
        try { process.StandardInput.Close(); if(!process.WaitForExit(5000)) process.Kill(true); } catch(InvalidOperationException) {}
        process.Dispose();
    }
}
internal static class J
{
    public static string S(this JsonNode? node, string key, string fallback="")=>node?[key]?.ToString() ?? fallback;
    public static int I(this JsonNode? node, string key, int fallback=0)=>int.TryParse(node.S(key),out var value)?value:fallback;
    public static bool B(this JsonNode? node, string key)=>node.S(key) is "true" or "1";
    public static JsonArray A(this JsonNode? node, string key)=>node?[key] as JsonArray ?? new JsonArray();
    public static JsonObject Copy(this JsonNode node)=>node.DeepClone().AsObject();
    public static JsonObject Obj(params (string key, object? value)[] pairs) {
        var result=new JsonObject(); foreach(var (key,value) in pairs) result[key]=value is JsonNode n?n.DeepClone():System.Text.Json.JsonSerializer.SerializeToNode(value); return result;
    }
}
