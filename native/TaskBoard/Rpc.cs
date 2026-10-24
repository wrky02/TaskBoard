using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;
using System.Collections.Concurrent;

namespace TaskBoard;
internal sealed class Rpc : IDisposable
{
    readonly Process process;
    readonly SemaphoreSlim writer = new(1, 1);
    readonly ConcurrentDictionary<int, TaskCompletionSource<JsonObject>> pending = new();
    readonly CancellationTokenSource lifetime = new();
    int sequence;
    public Rpc(string[] args)
    {
        string root=AppContext.BaseDirectory;
        string? source=Environment.GetEnvironmentVariable("TASKBOARD_WORKER_SOURCE");
        var start=new ProcessStartInfo(source is null?Path.Combine(root,"engine","TaskBoard.Engine.exe"):(Environment.GetEnvironmentVariable("TASKBOARD_PYTHON")??"python"))
        {UseShellExecute=false,CreateNoWindow=true,RedirectStandardInput=true,RedirectStandardOutput=true,RedirectStandardError=true,StandardInputEncoding=new UTF8Encoding(false),StandardOutputEncoding=new UTF8Encoding(false),StandardErrorEncoding=Encoding.UTF8};
        if(source is not null)start.ArgumentList.Add(source);
        start.ArgumentList.Add("--install-root");start.ArgumentList.Add(root);
        for(int i=0;i<args.Length-1;i++)if(args[i]=="--data-root"){start.ArgumentList.Add("--data-root");start.ArgumentList.Add(args[i+1]);}
        start.Environment["PYTHONUTF8"]="1";
        process=Process.Start(start)??throw new IOException("无法启动本地数据引擎");
        process.ErrorDataReceived+=(_,_)=>{};process.BeginErrorReadLine();
        _=ReadResponses();
    }
    async Task ReadResponses()
    {
        Exception failure=new IOException("本地数据引擎已退出。已保存的草稿会保留，请重新打开程序。");
        try{
            while(!lifetime.IsCancellationRequested){
                var raw=await process.StandardOutput.ReadLineAsync(lifetime.Token);
                if(raw is null)break;
                var reply=JsonNode.Parse(raw)?.AsObject()??throw new IOException("本地引擎返回无效数据");
                if(reply["id"] is null){failure=new IOException(reply.S("error","本地引擎启动失败"));break;}
                int id=reply["id"]!.GetValue<int>();
                if(pending.TryRemove(id,out var completion)){
                    if(reply["error"] is not null)completion.TrySetException(new InvalidOperationException(reply["error"]!.ToString()));
                    else completion.TrySetResult(reply["result"]!.AsObject());
                }
            }
        }catch(Exception ex){failure=ex;}
        finally{foreach(var entry in pending)if(pending.TryRemove(entry.Key,out var waiter))waiter.TrySetException(failure);}
    }
    public async Task<JsonObject> Call(string method,JsonObject? parameters=null)
    {
        if(process.HasExited)throw new IOException("本地数据引擎已停止。请重新打开程序恢复草稿。");
        int id=Interlocked.Increment(ref sequence);
        var completion=new TaskCompletionSource<JsonObject>(TaskCreationOptions.RunContinuationsAsynchronously);
        pending[id]=completion;
        string operation=Guid.NewGuid().ToString("N");
        var request=new JsonObject{["id"]=id,["method"]=method,["operation_id"]=operation,["params"]=parameters?.DeepClone()??new JsonObject()};
        bool entered=false;
        try{
            entered=await writer.WaitAsync(TimeSpan.FromSeconds(10),lifetime.Token);
            if(!entered)throw new TimeoutException("数据引擎正忙，当前输入仍保留。请稍后再试。");
            try{await process.StandardInput.WriteLineAsync(request.ToJsonString());await process.StandardInput.FlushAsync();}
            finally{writer.Release();entered=false;}
            int seconds=method is "restore" or "preview" or "export" or "vault-enable" or "vault-recover" or "recovery-verify"?180:30;
            return await completion.Task.WaitAsync(TimeSpan.FromSeconds(seconds),lifetime.Token);
        }catch(TimeoutException){
            throw new IOException("操作仍在处理，尚未确认保存结果。请保留当前输入，刷新核对后再操作。操作编号："+operation[..8]);
        }finally{if(entered)writer.Release();pending.TryRemove(id,out _);}
    }
    public void Dispose()
    {
        lifetime.Cancel();
        try{process.StandardInput.Close();if(!process.WaitForExit(5000))process.Kill(true);}catch(InvalidOperationException){}
        process.Dispose();lifetime.Dispose();
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
