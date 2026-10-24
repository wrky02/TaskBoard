using System;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Threading;
using Microsoft.Win32;

namespace TaskBoard;

internal sealed partial class BoardWindow
{
    async Task RecoveryCenter()
    {
        var status=Ui.Text("正在读取保护状态…",14);var target=Ui.Input("","recoveryTarget");target.MinWidth=340;Ui.Hint(target,"选择独立磁盘或本地备份目录");
        var enabled=Ui.Check("自动创建恢复点",true);var interval=Ui.Combo(new[]{"每 5 分钟","每 10 分钟","每 15 分钟","每 30 分钟"},2,"recoveryInterval");var points=new StackPanel();JsonObject state=new();bool refreshing=false;Window? dialog=null;
        async Task RefreshStatus(bool setFields=false){if(refreshing)return;refreshing=true;try{
            state=await rpc.Call("recovery-status");var config=state["config"];if(setFields){target.Text=config.S("target",state.S("target"));enabled.IsChecked=config is null||config.B("enabled");interval.SelectedIndex=Math.Max(0,Array.IndexOf(new[]{5,10,15,30},config.I("interval_minutes",15)));}
            var job=state["job"];string stage=job.S("stage");string stageLabel=stage switch{"queued"=>"等待创建","snapshot"=>"创建一致快照","copy-and-verify"=>"复制并校验","complete"=>"已完成校验","failed"=>"备份失败",_=>"就绪"};
            status.Text="状态："+stageLabel+"\n最近验证完成："+state["last"].S("completed_at","暂无")+"\n目标目录："+state.S("target",target.Text)+(state.B("target_available")?"":"\n目标目录当前不可用，请连接备份磁盘或重新选择目录。");if(job.S("error")!="")status.Text+="\n原因："+job.S("error");
            points.Children.Clear();string folder=state.S("target",target.Text);if(Directory.Exists(folder))foreach(var file in Directory.EnumerateFiles(folder,"*.zip").OrderByDescending(File.GetLastWriteTimeUtc).Take(40)){string path=file;var row=Ui.Row(Ui.Text(Path.GetFileName(file),13),Ui.Button("验证",async()=>{var report=await rpc.Call("recovery-verify",J.Obj(("path",path)));MessageBox.Show(report.ToJsonString(new System.Text.Json.JsonSerializerOptions{WriteIndented=true}),"恢复点验证");}),Ui.Button("预览并恢复",()=>PreviewAndRestore(path),"previewRecovery"));points.Children.Add(Ui.Card(row));}
            if(points.Children.Count==0)points.Children.Add(Ui.Text("此目录尚无恢复点，也可以选择其它完整备份进行恢复。",12,Ui.Muted));
        }catch(Exception ex){status.Text="无法更新恢复状态："+ex.Message;}finally{refreshing=false;}}
        async Task SaveConfig(){state=await rpc.Call("recovery-configure",J.Obj(("enabled",enabled.IsChecked==true),("target",target.Text),("interval_minutes",new[]{5,10,15,30}[Math.Max(0,interval.SelectedIndex)])));await RefreshStatus();}
        var choose=Ui.Button("选择目录",()=>{var open=new OpenFolderDialog{Title="选择恢复点保存目录"};if(open.ShowDialog(this)==true)target.Text=open.FolderName;},"chooseRecoveryTarget");
        var content=Ui.Column(Ui.Text("恢复中心",26),Ui.Text("保护状态以已经完成校验的恢复点为准。独立磁盘上的备份可以抵御本机磁盘故障。",13,Ui.Muted),Ui.Card(status),enabled,Ui.Row(target,choose),Ui.Row(interval,Ui.Button("保存保护设置",SaveConfig,"saveRecoveryConfig",true)),Ui.Row(Ui.Button("立即创建恢复点",async()=>{await SaveConfig();await rpc.Call("recovery-run");await RefreshStatus();},"createRecoveryPoint",true),Ui.Button("创建完整备份",()=>Export("zip")),Ui.Button("选择备份恢复",()=>Import(false))),Ui.Text("最近恢复点",20),points);
        var poll=new DispatcherTimer{Interval=TimeSpan.FromSeconds(2)};poll.Tick+=async(_,_)=>await RefreshStatus();dialog=Ui.Dialog(this,"恢复中心",content,900,760);AutomationProperties.SetAutomationId(dialog,"recoveryCenterDialog");dialog.Loaded+=async(_,_)=>{await RefreshStatus(true);poll.Start();};dialog.Closed+=(_,_)=>poll.Stop();dialog.ShowDialog();await Task.CompletedTask;
    }

    async Task PreviewAndRestore(string path)
    {
        JsonObject preview;
        try {preview=await rpc.Call("preview",J.Obj(("path",path)));}
        catch(InvalidOperationException ex) when(ex.Message.Contains("密码")||ex.Message.Contains("密钥")||ex.Message.Contains("加密")) {
            var password=new PasswordBox{MinHeight=38,Padding=new Thickness(8)};var key=Ui.Input("","backupRecoveryKey");Ui.Hint(key,"恢复密钥（也可以只输入备份口令）");Window? credentials=null;JsonObject? result=null;
            credentials=Ui.Dialog(this,"解锁加密备份",Ui.Column(Ui.Text("输入创建该备份时的口令或恢复密钥",19),Ui.Text("读取预览不会覆盖当前工作区。历史备份可能仍需旧口令。",12,Ui.Muted),Ui.Text("备份口令"),password,Ui.Text("恢复密钥"),key,Ui.Button("读取备份预览",async()=>{result=await rpc.Call("preview",J.Obj(("path",path),("password",password.Password),("recovery_key",key.Text)));password.Clear();key.Clear();credentials!.Close();},"unlockBackup",true)),590,430);credentials.ShowDialog();if(result is null)return;preview=result;
        }
        if(!Ui.Confirm($"备份时间：{preview.S("modified")}\n任务：{preview.I("tasks")} 项\n附件：{preview.I("attachments")} 个\n\n恢复会替换当前数据，并先保留安全备份。确认继续？"))return;
        var restored=await rpc.Call("restore");Accept(restored);BuildShell();MessageBox.Show("恢复完成。原工作区安全备份：\n"+restored.S("safety"),Ui.ApplicationName);
    }

    async Task VaultSettings()
    {
        var state=await rpc.Call("vault-status");bool encrypted=state.B("enabled");var password=new PasswordBox{MinHeight=38,Padding=new Thickness(8)};AutomationProperties.SetAutomationId(password,"vaultPassword");var accepted=Ui.Check("我了解：早期导出的外部明文副本不会自动改变，恢复密钥需要另行保管。");Window? dialog=null;
        var panel=Ui.Column(Ui.Text("加密工作区",25),Ui.Text(encrypted?"已启用：数据库与附件使用磁盘加密。":"尚未启用：当前密码只保护应用入口。",16,Ui.Purple),Ui.Text("加密保护存储的数据；在已解锁的 Windows 会话中仍应妥善保管设备。忘记密码和恢复密钥后无法解密。",13,Ui.Muted));
        if(!encrypted){panel.Children.Add(Ui.Text("输入当前工作区密码"));panel.Children.Add(password);panel.Children.Add(accepted);panel.Children.Add(Ui.Button("启用加密",async()=>{
            if(accepted.IsChecked!=true)throw new InvalidOperationException("请先阅读并勾选恢复密钥说明。");
            if(!Ui.Confirm("启用加密前会创建安全迁移副本，完成后数据库和附件以加密形式保存。请保持设备连接电源，确认继续？"))return;
            var result=await rpc.Call("vault-enable",J.Obj(("password",password.Password)));password.Clear();if(result["state"] is JsonObject)Accept(result);dialog!.Close();ShowRecoveryKey(result.S("recovery_key"));
        },"enableVault",true));}else{panel.Children.Add(Ui.Text("工作区密码可以在设置中修改。旧备份仍可能使用创建时的口令或旧恢复密钥。",13,Ui.Muted));panel.Children.Add(Ui.Button("重新生成恢复密钥",async()=>{if(!Ui.Confirm("生成新恢复密钥后，当前工作区将使用新密钥。旧备份仍可能需要旧密钥，请保留必要的历史凭据。继续？"))return;var result=await rpc.Call("vault-new-recovery-key");ShowRecoveryKey(result.S("recovery_key"));},"newRecoveryKey"));}
        dialog=Ui.Dialog(this,"加密工作区",panel,640,550);dialog.ShowDialog();
    }
    void ShowRecoveryKey(string key)
    {
        if(string.IsNullOrWhiteSpace(key))throw new InvalidOperationException("加密已处理，但未收到恢复密钥。请重新生成并妥善保存。");
        var text=Ui.Input(key,"recoveryKey");text.IsReadOnly=true;text.TextWrapping=TextWrapping.Wrap;var saved=Ui.Check("我已将恢复密钥保存在工作区之外的安全位置");Window? dialog=null;
        dialog=Ui.Dialog(this,"保存恢复密钥",Ui.Column(Ui.Text("请立即保存恢复密钥",24),Ui.Text("此处仅显示一次。不要把它放在同一个加密工作区中。",13,Ui.Muted),text,Ui.Button("复制恢复密钥",()=>Clipboard.SetText(key),"copyRecoveryKey"),saved,Ui.Button("已保存，完成",()=>{if(saved.IsChecked!=true){Ui.Error("请先保存恢复密钥并勾选确认。");return;}dialog!.DialogResult=true;},"confirmRecoveryKey",true)),650,390);dialog.ShowDialog();
    }
    Task RecoverVaultLogin()
    {
        var key=Ui.Input("","loginRecoveryKey");Ui.Hint(key,"TB4-… 恢复密钥");var password=new PasswordBox{MinHeight=38,Padding=new Thickness(8)};var confirm=new PasswordBox{MinHeight=38,Padding=new Thickness(8)};Window? dialog=null;
        dialog=Ui.Dialog(this,"使用恢复密钥",Ui.Column(Ui.Text("恢复加密工作区的访问",23),Ui.Text("使用此前保存的恢复密钥设置新密码。未启用加密的工作区无法通过此方式找回密码。",13,Ui.Muted),key,Ui.Text("新密码"),password,Ui.Text("再次输入"),confirm,Ui.Button("恢复并设置新密码",async()=>{State=await rpc.Call("vault-recover",J.Obj(("recovery_key",key.Text),("new_password",password.Password),("confirm",confirm.Password)));password.Clear();confirm.Clear();key.Clear();Unlocked=true;dialog!.Close();BuildShell();await RefreshDraftNotice();},"recoverVault",true)),630,490);dialog.ShowDialog();return Task.CompletedTask;
    }
    Task PortableData()
    {
        var mode=Ui.Combo(new[]{"跳过已存在任务（推荐）","导入为新的副本","替换相同标识的任务"},0,"importMode");var result=Ui.Text("",13,Ui.Muted);
        async Task ExportData(){var save=new SaveFileDialog{FileName="TaskBoard-content-"+DateTime.Now.ToString("yyyyMMdd-HHmmss")+".json",Filter="完整任务内容 JSON|*.json"};if(save.ShowDialog(this)!=true)return;if(!Ui.Confirm("此文件包含任务内容和附件，采用明文格式，不包含密码或恢复密钥。请保存在可信位置。继续导出？"))return;var report=await rpc.Call("pro-export",J.Obj(("path",save.FileName)));result.Text=$"已导出 {report.I("tasks")} 项任务、{report.I("attachments")} 个附件。";}
        async Task ImportData(){var open=new OpenFileDialog{Title="选择完整内容导出文件",Filter="完整任务内容 JSON|*.json"};if(open.ShowDialog(this)!=true)return;string path=open.FileName;string hash=Convert.ToHexString(SHA256.HashData(await File.ReadAllBytesAsync(path))).ToLowerInvariant();string choice=new[]{"skip","copy","replace"}[Math.Max(0,mode.SelectedIndex)];var report=await rpc.Call("pro-import-preview",J.Obj(("path",path),("mode",choice)));if(!Ui.Confirm($"即将导入 {report.I("tasks")} 项任务、{report.I("attachments")} 个附件。\n已有 {report.I("existing")} 项，新任务 {report.I("new")} 项。\n处理方式：{mode.SelectedItem}\n\n确认导入？"))return;if(hash!=Convert.ToHexString(SHA256.HashData(await File.ReadAllBytesAsync(path))).ToLowerInvariant())throw new IOException("导入文件在预览后发生变化，请重新选择并检查。");var imported=await rpc.Call("pro-import",J.Obj(("path",path),("mode",choice),("confirm",true),("expected_sha256",hash)));Accept(imported);result.Text=$"导入完成：{imported.I("imported")} 项；跳过 {imported.I("skipped")} 项。";}
        Ui.Dialog(this,"完整数据导出与合并",Ui.Column(Ui.Text("数据可以完整带走",24),Ui.Text("完整内容格式保留稳定标识、项目、关系、检查清单和附件；密码与密钥不包含在内容导出中。需要恢复整个工作区时，请使用加密完整备份。",13,Ui.Muted),Ui.Button("导出完整内容",ExportData,"exportPortable",true),Ui.Text("合并导入时遇到相同任务"),mode,Ui.Text("替换模式不会用较旧版本静默覆盖较新数据。关系或附件校验失败时会拒绝导入。",12,Ui.Muted),Ui.Button("预览并导入",ImportData,"importPortable"),result),700,530).ShowDialog();return Task.CompletedTask;
    }
}
