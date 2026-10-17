using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Microsoft.Win32;

namespace TaskBoard;
internal sealed class TaskEditor : Window
{
    readonly BoardWindow board;
    readonly JsonObject original;
    readonly string draftKey;
    readonly TextBox title,note,reminderTime,checkInput,milestoneInput,fileSearch;
    readonly ComboBox status,priority,repeat,basis,reminder;
    readonly CheckBox today;
    readonly DatePicker due;
    readonly StackPanel checksPanel=new(),milestonesPanel=new(),filesPanel=new();
    readonly List<(CheckBox check,int id)> tags=new(),dependencies=new();
    readonly List<(CheckBox done,TextBox label)> checks=new();
    readonly List<(CheckBox done,TextBox name,int id)> milestones=new();
    readonly List<JsonObject> existingFiles=new(),newFiles=new();
    readonly HashSet<int> removed=new();
    readonly Dictionary<int,(TextBox label,TextBox note)> versions=new();
    readonly DispatcherTimer draftTimer=new(){Interval=TimeSpan.FromSeconds(2)};
    readonly TextBlock saveState=Ui.Text("编辑内容独立保存为草稿；点击保存后才正式生效",12,Ui.Muted);
    readonly ProgressBar progress=new(){Height=5,Minimum=0,Maximum=100,Visibility=Visibility.Collapsed};
    string lastDraft="",initial="";
    bool finished,savingDraft,loading=true,busy,readOnly;
    public TaskEditor(BoardWindow owner,JsonObject? task,JsonObject? restored=null,string? key=null)
    {
        board=owner;Owner=owner;Title=task is null?"新建任务":"编辑任务";Width=Math.Min(800,SystemParameters.WorkArea.Width-24);Height=Math.Min(850,SystemParameters.WorkArea.Height-24);MinWidth=Math.Min(650,Width);MinHeight=Math.Min(550,Height);WindowStartupLocation=WindowStartupLocation.CenterOwner;Background=Ui.Surface;FontFamily=owner.FontFamily;FontSize=14;Icon=owner.Icon;
        original=task is null?new JsonObject():BoardWindow.Payload(task);
        var value=restored??original;
        draftKey=key??(task is null?"native-new-"+Guid.NewGuid().ToString("N"):"native-task-"+task.I("id"));
        readOnly=task is not null&&(task.S("archived_at")!=""||task.S("deleted_at")!="");
        title=Ui.Input(value.S("title"),"editorTitle");title.FontSize=21;
        note=Ui.Input(value.S("note"),"editorNote");note.AcceptsReturn=true;note.TextWrapping=TextWrapping.Wrap;note.MinHeight=140;note.VerticalScrollBarVisibility=ScrollBarVisibility.Auto;
        status=Ui.Combo(BoardWindow.Stages,Math.Max(0,Array.IndexOf(BoardWindow.Stages,value.S("status","计划中"))),"editorStatus");
        priority=Ui.Combo(BoardWindow.Priorities,value["options"].I("priority",2),"editorPriority");
        due=new DatePicker{Width=165,MinHeight=34,SelectedDate=DateTime.TryParse(value.S("due"),out var d)?d:null,Margin=new Thickness(0,4,8,4),SelectedDateFormat=DatePickerFormat.Short};System.Windows.Automation.AutomationProperties.SetAutomationId(due,"editorDue");
        today=Ui.Check("加入今日计划（不改变截止日期）",value["options"].S("today")==DateTime.Today.ToString("yyyy-MM-dd"));
        repeat=Ui.Combo(new[]{"不重复","每天","每周","每月"},Array.IndexOf(new[]{"off","daily","weekly","monthly"},value["options"].S("repeat","off")));
        basis=Ui.Combo(new[]{"按固定截止日期","按完成日期"},value["options"].S("repeat_basis")=="completion"?1:0);
        reminder=Ui.Combo(new[]{"不提醒","当天提醒","提前一天"},Array.IndexOf(new[]{"off","today","before"},value["options"].S("reminder","off")));
        reminderTime=Ui.Input(value["options"].S("reminder_time","09:00"));reminderTime.Width=80;
        var top=Ui.Column(title,Ui.Row(Ui.Text("状态"),status,Ui.Text("优先级"),priority,Ui.Text("截止日期"),due),Ui.Row(Ui.Button("今天",()=>due.SelectedDate=DateTime.Today),Ui.Button("明天",()=>due.SelectedDate=DateTime.Today.AddDays(1)),Ui.Button("一周后",()=>due.SelectedDate=DateTime.Today.AddDays(7)),Ui.Button("清除日期",()=>due.SelectedDate=null)),today);
        var tagPanel=Ui.Row();var tagIds=value.A("tags").Select(x=>x is JsonObject?x.I("id"):int.Parse(x!.ToString())).ToHashSet();
        foreach(var t in owner.State.A("tags")){int id=t.I("id");var check=Ui.Check(t.S("name"),tagIds.Contains(id));tags.Add((check,id));tagPanel.Children.Add(check);}
        var depPanel=new StackPanel();var depIds=value.A("dependencies").Select(x=>x is JsonObject?x.I("id"):int.Parse(x!.ToString())).ToHashSet();
        var depSearch=Ui.Input();depSearch.ToolTip="搜索依赖任务";depPanel.Children.Add(depSearch);
        var depList=new StackPanel();foreach(var t in owner.State.A("tasks").Concat(owner.State.A("archived")).Concat(owner.State.A("trash").Where(x=>depIds.Contains(x.I("id"))))){int id=t.I("id");if(id==task.I("id"))continue;var check=Ui.Check($"#{id}  {t.S("title")}  · {t.S("status")}",depIds.Contains(id));dependencies.Add((check,id));depList.Children.Add(check);}
        depSearch.TextChanged+=(_,_)=>{foreach(var (check,_) in dependencies)check.Visibility=check.Content.ToString()!.Contains(depSearch.Text,StringComparison.OrdinalIgnoreCase)?Visibility.Visible:Visibility.Collapsed;};depPanel.Children.Add(new ScrollViewer{Content=depList,MaxHeight=200,VerticalScrollBarVisibility=ScrollBarVisibility.Auto});
        checkInput=Ui.Input("","checkInput");checkInput.Width=360;var addCheck=Ui.Button("添加检查项",()=>{AddCheck(checkInput.Text,false);checkInput.Clear();},"addCheck");checkInput.KeyDown+=(_,e)=>{if(e.Key==Key.Enter){AddCheck(checkInput.Text,false);checkInput.Clear();e.Handled=true;}};
        foreach(var c in value.A("checklist"))AddCheck(c.S("label"),c.B("done"));
        milestoneInput=Ui.Input();milestoneInput.Width=360;foreach(var m in value.A("milestones"))AddMilestone(m.S("name"),m.B("confirmed"),m.I("id"));
        fileSearch=Ui.Input();fileSearch.ToolTip="按附件名称搜索";fileSearch.TextChanged+=(_,_)=>RenderFiles();
        foreach(var f in task.A("files"))existingFiles.Add(f!.Copy());
        foreach(var v in value.A("file_versions")){var f=existingFiles.FirstOrDefault(f=>f.I("id")==v.I("id"));if(f is not null){f["version_label"]=v.S("version_label");f["version_note"]=v.S("version_note");}}
        foreach(var f in value.A("new_files"))newFiles.Add(f!.Copy());foreach(var id in value.A("removed_files"))removed.Add(int.Parse(id!.ToString()));
        var attachmentActions=Ui.Row(Ui.Button("选择附件",async()=>{var dialog=new OpenFileDialog{Multiselect=true,Title="选择附件"};if(dialog.ShowDialog(this)==true)await AddFiles(dialog.FileNames);},"addFiles"),Ui.Button("粘贴截图",PasteImage),Ui.Text("拖入文件或 Ctrl+V · 单次 100 MB",12,Ui.Muted));
        var fileArea=Ui.Column(attachmentActions,progress,fileSearch,filesPanel);fileArea.AllowDrop=true;fileArea.Drop+=async(_,e)=>{if(e.Data.GetData(DataFormats.FileDrop) is string[] paths){try{await AddFiles(paths);}catch(Exception ex){Ui.Error(ex.Message);}}};RenderFiles();
        var details=Ui.Column(Ui.Section("任务说明",note),Ui.Section("标签",tagPanel),Ui.Section("检查清单",Ui.Column(checksPanel,Ui.Row(checkInput,addCheck))),Ui.Section("里程碑",Ui.Column(milestonesPanel,Ui.Row(milestoneInput,Ui.Button("添加里程碑",()=>{AddMilestone(milestoneInput.Text,false,0);milestoneInput.Clear();}))),false),Ui.Section("重复与提醒",Ui.Column(Ui.Row(repeat,basis),Ui.Text("完成当前任务后自动生成下一项；固定日期模式跳过已错过的周期。",12,Ui.Muted),Ui.Row(reminder,reminderTime),Ui.Text("程序在托盘运行时可提醒；完全退出或锁定工作区后无法提醒。",12,Ui.Muted))),Ui.Section("任务依赖",depPanel,false),Ui.Section("附件",fileArea),Ui.Section("操作记录",History(task.I("id")),false));
        var save=Ui.Button("保存任务",Save,"saveTask",true);
        var bottom=Ui.Column(saveState,Ui.Row(save,Ui.Button("关闭（保留草稿）",()=>Close(),"closeEditor"),Ui.Button("放弃草稿",async()=>{if(Ui.Confirm("放弃尚未正式保存的修改？")){await owner.rpc.Call("draft-save",J.Obj(("key",draftKey),("action","delete")));finished=true;Close();}})));
        var root=new DockPanel{Margin=new Thickness(22)};DockPanel.SetDock(top,Dock.Top);root.Children.Add(top);DockPanel.SetDock(bottom,Dock.Bottom);root.Children.Add(bottom);root.Children.Add(Ui.Scroll(details));Content=root;
        if(readOnly){top.IsEnabled=false;details.IsEnabled=false;save.IsEnabled=false;saveState.Text="已归档或回收站任务为只读，请先恢复再编辑。";}
        PreviewKeyDown+=async(_,e)=>{if(e.Key==Key.V&&Keyboard.Modifiers.HasFlag(ModifierKeys.Control)&&Clipboard.ContainsImage()){e.Handled=true;try{await PasteImage();}catch(Exception ex){Ui.Error(ex.Message);}}};
        loading=false;initial=Collect().ToJsonString();lastDraft=restored is null?initial:"";
        draftTimer.Tick+=async(_,_)=>await SaveDraft();if(!readOnly)draftTimer.Start();
        Closing+=async(_,e)=>{if(finished||readOnly)return;e.Cancel=true;if(busy)return;busy=true;try{await SaveDraft(true);finished=true;_ = Dispatcher.BeginInvoke(Close);}catch(Exception ex){Ui.Error("草稿未能保存："+ex.Message);}finally{busy=false;}};
        Closed+=(_,_)=>draftTimer.Stop();Loaded+=(_,_)=>title.Focus();
    }
    UIElement History(int id){var p=new StackPanel();foreach(var a in board.State.A("activity").Where(a=>a.I("task_id")==id))p.Children.Add(Ui.Text(a.S("at")+"  "+a.S("action")+"  "+a.S("detail"),12,Ui.Muted));return p;}
    void AddCheck(string label,bool done)
    {
        if(string.IsNullOrWhiteSpace(label))return;
        var c=Ui.Check("",done);var text=Ui.Input(label);text.Width=470;var row=Ui.Row(c,text);checks.Add((c,text));
        row.Children.Add(Ui.Button("移除",()=>{checks.Remove((c,text));checksPanel.Children.Remove(row);}));checksPanel.Children.Add(row);
        c.Checked+=(_,_)=>{if(!loading&&checks.Count>0&&checks.All(x=>x.done.IsChecked==true)&&status.SelectedIndex!=2&&Ui.Confirm("检查清单已全部完成，是否同时将任务标记为完成？"))status.SelectedIndex=2;};
    }
    void AddMilestone(string name,bool confirmed,int id)
    {
        if(string.IsNullOrWhiteSpace(name))return;var c=Ui.Check("",confirmed);var text=Ui.Input(name);text.Width=450;var row=Ui.Row(c,text);milestones.Add((c,text,id));row.Children.Add(Ui.Button("移除",()=>{milestones.Remove((c,text,id));milestonesPanel.Children.Remove(row);}));milestonesPanel.Children.Add(row);
    }
    async Task AddFiles(string[] paths)
    {
        if(readOnly)return;busy=true;progress.Visibility=Visibility.Visible;progress.Value=0;
        try {
            long total=newFiles.Sum(f=>Convert.FromBase64String(f.S("data")).LongLength);var staged=new List<JsonObject>();
            for(int i=0;i<paths.Length;i++){
                var info=new FileInfo(paths[i]);if(!info.Exists)throw new IOException("请选择文件");total+=info.Length;if(info.Length==0||total>100L*1024*1024)throw new IOException("附件不能为空，单次总计不得超过 100 MB");
                var bytes=await File.ReadAllBytesAsync(paths[i]);staged.Add(J.Obj(("name",info.Name),("data",Convert.ToBase64String(bytes))));progress.Value=(i+1)*100.0/paths.Length;
            }
            newFiles.AddRange(staged);RenderFiles();await SaveDraft(true);
        }finally{busy=false;progress.Visibility=Visibility.Collapsed;}
    }
    async Task PasteImage()
    {
        if(readOnly||!Clipboard.ContainsImage())return;var image=Clipboard.GetImage();if(image is null)return;
        var encoder=new PngBitmapEncoder();encoder.Frames.Add(BitmapFrame.Create(image));using var stream=new MemoryStream();encoder.Save(stream);
        if(stream.Length+newFiles.Sum(f=>Convert.FromBase64String(f.S("data")).LongLength)>100L*1024*1024)throw new IOException("本次新增附件不能超过 100 MB");
        newFiles.Add(J.Obj(("name","截图-"+DateTime.Now.ToString("yyyyMMdd-HHmmss")+".png"),("data",Convert.ToBase64String(stream.ToArray()))));RenderFiles();await SaveDraft(true);
    }
    void RenderFiles()
    {
        // Preserve version fields when rerendering after search or removal.
        foreach(var f in existingFiles)if(versions.TryGetValue(f.I("id"),out var fields)){f["version_label"]=fields.label.Text;f["version_note"]=fields.note.Text;}
        filesPanel.Children.Clear();versions.Clear();
        foreach(var f in existingFiles.Where(f=>!removed.Contains(f.I("id")))){
            var item=f;int id=f.I("id");var version=Ui.Input(f.S("version_label","初稿"));version.Width=100;var vn=Ui.Input(f.S("version_note"));vn.Width=250;versions[id]=(version,vn);
            var row=Ui.Column(Ui.Text($"{f.S("filename")}   {f.I("size")/1024.0:N1} KB"),Ui.Row(Ui.Button("另存为",()=>SaveFile(item)),Ui.Button("预览图片",()=>Gallery(item)),Ui.Button("移除",()=>{removed.Add(id);RenderFiles();})),Ui.Row(Ui.Text("版本"),version,vn));
            row.Visibility=f.S("filename").Contains(fileSearch.Text,StringComparison.OrdinalIgnoreCase)?Visibility.Visible:Visibility.Collapsed;filesPanel.Children.Add(Ui.Card(row));
        }
        foreach(var f in newFiles.ToList()){
            var row=Ui.Row(Ui.Text($"待保存 · {f.S("name")} · {Convert.FromBase64String(f.S("data")).Length/1024.0:N1} KB"),Ui.Button("移除",()=>{newFiles.Remove(f);RenderFiles();}));
            row.Visibility=f.S("name").Contains(fileSearch.Text,StringComparison.OrdinalIgnoreCase)?Visibility.Visible:Visibility.Collapsed;filesPanel.Children.Add(row);
        }
    }
    void SaveFile(JsonObject f){var dialog=new SaveFileDialog{FileName=Path.GetFileName(f.S("filename")),Title="另存附件"};if(dialog.ShowDialog(this)==true){if(Path.GetFullPath(dialog.FileName)==Path.GetFullPath(f.S("path")))throw new IOException("请选择数据目录之外的位置");File.Copy(f.S("path"),dialog.FileName,true);}}
    void Gallery(JsonObject file)
    {
        var images=existingFiles.Where(f=>f.B("is_image")&&!removed.Contains(f.I("id"))).ToList();int index=images.FindIndex(f=>f.I("id")==file.I("id"));if(index<0)throw new InvalidOperationException("此附件不是图片，请另存后使用对应程序查看。");
        var image=new Image{Stretch=Stretch.Uniform,MaxHeight=550};var caption=Ui.Text("");
        void Show(){var f=images[index];var bitmap=new BitmapImage();bitmap.BeginInit();bitmap.CacheOption=BitmapCacheOption.OnLoad;bitmap.UriSource=new Uri(f.S("path"));bitmap.EndInit();image.Source=bitmap;caption.Text=$"{index+1}/{images.Count}  {f.S("filename")}";}
        void Move(int delta){index=(index+delta+images.Count)%images.Count;Show();}
        Show();var dialog=Ui.Dialog(this,"图片预览",Ui.Column(caption,Ui.Row(Ui.Button("← 上一张",()=>Move(-1)),Ui.Button("下一张 →",()=>Move(1))),image),850,720);dialog.KeyDown+=(_,e)=>{if(e.Key==Key.Left)Move(-1);if(e.Key==Key.Right)Move(1);};dialog.ShowDialog();
    }
    JsonObject Collect()
    {
        var p=original.Copy();p["title"]=title.Text;p["note"]=note.Text;p["status"]=BoardWindow.Stages[Math.Max(0,status.SelectedIndex)];p["due"]=due.SelectedDate?.ToString("yyyy-MM-dd");
        p["options"]=J.Obj(("priority",priority.SelectedIndex),("today",today.IsChecked==true?DateTime.Today.ToString("yyyy-MM-dd"):""),("repeat",new[]{"off","daily","weekly","monthly"}[Math.Max(0,repeat.SelectedIndex)]),("repeat_basis",basis.SelectedIndex==1?"completion":"due"),("reminder",new[]{"off","today","before"}[Math.Max(0,reminder.SelectedIndex)]),("reminder_time",reminderTime.Text));
        p["tags"]=new JsonArray(tags.Where(x=>x.check.IsChecked==true).Select(x=>(JsonNode?)JsonValue.Create(x.id)).ToArray());
        p["dependencies"]=new JsonArray(dependencies.Where(x=>x.check.IsChecked==true).Select(x=>(JsonNode?)JsonValue.Create(x.id)).ToArray());
        p["checklist"]=new JsonArray(checks.Select(x=>(JsonNode?)J.Obj(("label",x.label.Text),("done",x.done.IsChecked==true))).ToArray());
        p["milestones"]=new JsonArray(milestones.Select(x=>(JsonNode?)J.Obj(("id",x.id),("name",x.name.Text),("confirmed",x.done.IsChecked==true))).ToArray());
        p["removed_files"]=new JsonArray(removed.Select(x=>(JsonNode?)JsonValue.Create(x)).ToArray());
        p["new_files"]=new JsonArray(newFiles.Select(f=>(JsonNode?)f.DeepClone()).ToArray());
        p["file_versions"]=new JsonArray(versions.Select(f=>(JsonNode?)J.Obj(("id",f.Key),("version_label",f.Value.label.Text),("version_note",f.Value.note.Text))).ToArray());
        p["draft_key"]=draftKey;return p;
    }
    async Task SaveDraft(bool force=false)
    {
        if(loading||readOnly||finished)return;
        // A serialized RPC ensures writes finish in order. Closing waits for any
        // in-flight autosave, then captures the latest edits, never an older copy.
        if(savingDraft){if(!force)return;while(savingDraft)await Task.Delay(25);}
        var p=Collect();string raw=p.ToJsonString();if(raw==lastDraft)return;
        savingDraft=true;
        try {await board.rpc.Call("draft-save",J.Obj(("key",draftKey),("draft",p)));lastDraft=raw;saveState.Text="草稿已保存 · "+DateTime.Now.ToString("HH:mm:ss")+"（尚未正式保存）";}
        catch(Exception ex){saveState.Text="草稿保存失败："+ex.Message;if(force)throw;}
        finally{savingDraft=false;}
    }
    async Task Save()
    {
        if(busy)return;busy=true;draftTimer.Stop();
        try {
            if(string.IsNullOrWhiteSpace(title.Text))throw new InvalidOperationException("请输入任务标题");
            await SaveDraft(true);var result=await board.rpc.Call("save",Collect());board.Accept(result);finished=true;Close();
        }finally{busy=false;if(!finished)draftTimer.Start();}
    }
}
