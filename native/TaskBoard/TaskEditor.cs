using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Threading;
using Microsoft.Win32;

namespace TaskBoard;
internal sealed partial class TaskEditor : Window
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
    readonly DockPanel editorRoot=new(){Margin=new Thickness(22,18,22,16)};
    readonly ContentControl editorPane=new();
    string lastDraft="",initial="";
    bool finished,savingDraft,loading=true,busy,readOnly,composing,paneMode,closingWindow;
    Action? paneClosed;
    internal bool IsPaneOpen=>paneMode&&!finished;

    public TaskEditor(BoardWindow owner,JsonObject? task,JsonObject? restored=null,string? key=null)
    {
        board=owner;Owner=owner;Title=task is null?"新建任务":"编辑任务";Width=Math.Min(850,SystemParameters.WorkArea.Width-24);Height=Math.Min(900,SystemParameters.WorkArea.Height-24);MinWidth=Math.Min(500,Width);MinHeight=Math.Min(550,Height);WindowStartupLocation=WindowStartupLocation.CenterOwner;Background=Ui.Surface;FontFamily=owner.FontFamily;FontSize=14;Icon=owner.Icon;
        original=task is null?new JsonObject():BoardWindow.Payload(task);
        if(task?["pro"] is JsonObject taskPro)original["pro"]=taskPro.DeepClone();
        var value=restored??original;
        draftKey=key??(task is null?"native-new-"+Guid.NewGuid().ToString("N"):"native-task-"+task.I("id"));
        readOnly=task is not null&&(task.S("archived_at")!=""||task.S("deleted_at")!="");
        title=Ui.Input(value.S("title"),"editorTitle");title.FontSize=22;title.FontWeight=FontWeights.SemiBold;Ui.Hint(title,"为任务起一个清晰的标题…");
        note=Ui.Input(value.S("note"),"editorNote");note.AcceptsReturn=true;note.TextWrapping=TextWrapping.Wrap;note.MinHeight=110;Ui.Hint(note,"补充任务背景、目标和需要注意的细节…");note.VerticalContentAlignment=VerticalAlignment.Top;note.VerticalScrollBarVisibility=ScrollBarVisibility.Auto;
        status=Ui.Combo(BoardWindow.Stages,Math.Max(0,Array.IndexOf(BoardWindow.Stages,value.S("status","计划中"))),"editorStatus");
        priority=Ui.Combo(BoardWindow.Priorities,value["options"].I("priority",2),"editorPriority");
        due=new DatePicker{MinHeight=38,SelectedDate=DateTime.TryParse(value.S("due"),out var date)?date:null,Margin=new Thickness(0,4,8,6),SelectedDateFormat=DatePickerFormat.Short};System.Windows.Automation.AutomationProperties.SetAutomationId(due,"editorDue");
        today=Ui.Check("加入今日计划（不改变截止日期）",value["options"].S("today")==DateTime.Today.ToString("yyyy-MM-dd"));
        repeat=Ui.Combo(new[]{"不重复","每天","每周","每月"},Math.Max(0,Array.IndexOf(new[]{"off","daily","weekly","monthly"},value["options"].S("repeat","off"))));
        basis=Ui.Combo(new[]{"按固定截止日期","按完成日期"},value["options"].S("repeat_basis")=="completion"?1:0);
        reminder=Ui.Combo(new[]{"不提醒","当天提醒","提前一天"},Math.Max(0,Array.IndexOf(new[]{"off","today","before"},value["options"].S("reminder","off"))));
        reminderTime=Ui.Input(value["options"].S("reminder_time","09:00"));reminderTime.Width=80;Ui.Hint(reminderTime,"提醒时刻，如 09:00");
        var fields=new Grid{Margin=new Thickness(0,6,0,4)};for(int i=0;i<3;i++)fields.ColumnDefinitions.Add(new ColumnDefinition());
        UIElement[] fieldControls={status,priority,due};string[] labels={"任务状态","优先级","截止日期"};
        for(int i=0;i<3;i++){var field=Ui.Column(Ui.Text(labels[i],11,Ui.Muted),fieldControls[i]);Grid.SetColumn(field,i);fields.Children.Add(field);}
        var shortcuts=Ui.Row();foreach(var (label,days) in new[]{("今天",0),("明天",1),("一周后",7)}){var button=Ui.Quiet(label,()=>{due.SelectedDate=DateTime.Today.AddDays(days);return Task.CompletedTask;});button.MinHeight=28;button.Padding=new Thickness(10,3,10,3);button.Foreground=Ui.Purple;shortcuts.Children.Add(button);}var clear=Ui.Quiet("清除日期",()=>{due.SelectedDate=null;return Task.CompletedTask;});clear.MinHeight=28;clear.Padding=new Thickness(10,3,10,3);shortcuts.Children.Add(clear);
        System.Windows.Automation.AutomationProperties.SetAutomationId(editorRoot,"editorPane");
        System.Windows.Automation.AutomationProperties.SetAutomationId(editorPane,"editorPane");System.Windows.Automation.AutomationProperties.SetName(editorPane,"任务详情编辑面板");
        var top=Ui.Column(Ui.Row(Ui.Chip(task is null?"NEW TASK":"TASK / "+task.I("id"),Ui.Purple),Ui.Text(task is null?"记录新的想法与安排":"任务详情",12,Ui.Muted)),title,fields,shortcuts,today);top.Margin=new Thickness(0,0,0,10);
        var tagPanel=Ui.Row();var tagIds=value.A("tags").Select(x=>x is JsonObject?x.I("id"):int.Parse(x!.ToString())).ToHashSet();
        foreach(var tag in owner.State.A("tags")){int id=tag.I("id");var check=Ui.Check(tag.S("name"),tagIds.Contains(id));tags.Add((check,id));tagPanel.Children.Add(check);}
        var depPanel=BuildDependencies(value,task.I("id"));
        checkInput=Ui.Input("","checkInput");checkInput.Width=270;Ui.Hint(checkInput,"添加检查项，按 Enter 确认…");var addCheck=Ui.Button("添加检查项",()=>{AddCheck(checkInput.Text,false);checkInput.Clear();},"addCheck");checkInput.KeyDown+=(_,e)=>{if(e.Key==Key.Enter&&!composing){AddCheck(checkInput.Text,false);checkInput.Clear();e.Handled=true;}};
        foreach(var item in value.A("checklist"))AddCheck(item.S("label"),item.B("done"));
        milestoneInput=Ui.Input();milestoneInput.Width=270;Ui.Hint(milestoneInput,"添加阶段目标…");foreach(var item in value.A("milestones"))AddMilestone(item.S("name"),item.B("confirmed"),item.I("id"));
        milestoneInput.KeyDown+=(_,e)=>{if(e.Key==Key.Enter&&!composing){AddMilestone(milestoneInput.Text,false,0);milestoneInput.Clear();e.Handled=true;}};
        fileSearch=Ui.Input();Ui.Hint(fileSearch,"按名称搜索附件…");fileSearch.TextChanged+=(_,_)=>RenderFiles();
        foreach(var file in task.A("files"))existingFiles.Add(file!.Copy());
        foreach(var version in value.A("file_versions")){var file=existingFiles.FirstOrDefault(f=>f.I("id")==version.I("id"));if(file is not null){file["version_label"]=version.S("version_label");file["version_note"]=version.S("version_note");}}
        foreach(var file in value.A("new_files"))newFiles.Add(file!.Copy());foreach(var id in value.A("removed_files"))removed.Add(int.Parse(id!.ToString()));
        var fileArea=BuildAttachments();
        var professional=BuildProfessional(value,task);
        var details=Ui.Column(professional,Ui.Section("任务说明",note),Ui.Section("标签",tagPanel),Ui.Section("检查清单",Ui.Column(checksPanel,Ui.Row(checkInput,addCheck))),Ui.Section("里程碑",Ui.Column(milestonesPanel,Ui.Row(milestoneInput,Ui.Button("添加里程碑",()=>{AddMilestone(milestoneInput.Text,false,0);milestoneInput.Clear();}))),false),Ui.Section("重复与提醒",Ui.Column(Ui.Text("兼容原有规则：完成本项后生成下一项",12,Ui.Muted),Ui.Row(repeat,basis),Ui.Row(reminder,reminderTime),BuildSchedule(value),Ui.Text("提醒在程序运行且工作区已解锁时生效；休眠期间的提醒将在唤醒后补发。",12,Ui.Muted)),false),Ui.Section("依赖与相关任务",depPanel,false),Ui.Section("附件",fileArea),Ui.Section("正式版本历史",BuildHistory(task.I("id")),false),Ui.Section("操作记录",History(task.I("id")),false));
        var save=Ui.Button("保存任务",Save,"saveTask",true);
        var bottom=Ui.Column(saveState,Ui.Row(save,Ui.Button("关闭（保留草稿）",RequestClosePaneAsync,"closeEditor"),Ui.Button("放弃草稿",DiscardDraft)));
        bottom.Margin=new Thickness(0,12,0,0);DockPanel.SetDock(bottom,Dock.Bottom);editorRoot.Children.Add(bottom);editorRoot.Children.Add(Ui.Scroll(Ui.Column(top,details)));Content=editorRoot;
        if(readOnly){top.IsEnabled=false;professional.IsEnabled=false;note.IsReadOnly=true;save.IsEnabled=false;saveState.Text="已归档或回收站任务为只读，可查看历史与附件；请先恢复再编辑。";}
        TextCompositionManager.AddPreviewTextInputStartHandler(editorRoot,(_,_)=>composing=true);
        TextCompositionManager.AddPreviewTextInputUpdateHandler(editorRoot,(_,_)=>composing=true);
        TextCompositionManager.AddPreviewTextInputHandler(editorRoot,(_,_)=>composing=false);
        editorRoot.PreviewKeyDown+=HandleKeys;
        loading=false;initial=Collect().ToJsonString();lastDraft=restored is null?initial:"";
        draftTimer.Tick+=async(_,_)=>await SaveDraft();if(!readOnly)draftTimer.Start();
        Closing+=async(_,e)=>{if(finished)return;e.Cancel=true;closingWindow=true;try{await RequestClosePaneAsync();}catch(Exception ex){Ui.Error("草稿未能保存："+ex.Message);}finally{closingWindow=false;}};
        Closed+=(_,_)=>Cleanup();Loaded+=(_,_)=>title.Focus();
    }
    internal FrameworkElement DetachContentForPane(Action closed)
    {
        if(IsLoaded)throw new InvalidOperationException("已打开的窗口无法移入详情面板。");
        paneMode=true;paneClosed=closed;Content=null;editorPane.Content=editorRoot;editorRoot.Loaded+=(_,_)=>title.Focus();return editorPane;
    }
    internal async Task RequestClosePaneAsync()
    {
        if(finished)return;
        if(busy)throw new InvalidOperationException("正在保存或导入附件，请等待完成或先取消导入。");
        busy=true;
        try{await SaveDraft(true);finished=true;FinishClose();}finally{busy=false;}
    }
    void FinishClose(){Cleanup();if(paneMode){paneClosed?.Invoke();paneClosed=null;}else if(closingWindow)_=Dispatcher.BeginInvoke(Close);else Close();}
    void Cleanup(){draftTimer.Stop();uploadCancellation?.Cancel();}
    async Task DiscardDraft()
    {
        if(busy||readOnly||!Ui.Confirm("放弃尚未正式保存的修改？已正式保存的任务不受影响。"))return;
        busy=true;try{while(savingDraft)await Task.Delay(25);await board.rpc.Call("draft-save",J.Obj(("key",draftKey),("action","delete")));finished=true;FinishClose();}finally{busy=false;}
    }
    async void HandleKeys(object sender,KeyEventArgs e)
    {
        if(composing||e.Key==Key.ImeProcessed||e.IsRepeat)return;
        bool control=Keyboard.Modifiers.HasFlag(ModifierKeys.Control);
        try{
            if(control&&(e.Key==Key.S||e.Key==Key.Enter)){e.Handled=true;await Save();}
            else if(e.Key==Key.Escape){if(due.IsDropDownOpen){due.IsDropDownOpen=false;e.Handled=true;return;}if(Keyboard.FocusedElement is ComboBox combo&&combo.IsDropDownOpen)return;e.Handled=true;await RequestClosePaneAsync();}
            else if(control&&e.Key==Key.V&&Clipboard.ContainsImage()&&!Clipboard.ContainsText()){e.Handled=true;await PasteImage();}
        }catch(Exception ex){Ui.Error(ex.Message);}
    }
    UIElement History(int id){var panel=new StackPanel();foreach(var item in board.State.A("activity").Where(a=>a.I("task_id")==id))panel.Children.Add(Ui.Text(item.S("at")+"  "+item.S("action")+"  "+item.S("detail"),12,Ui.Muted));if(panel.Children.Count==0)panel.Children.Add(Ui.Text("还没有操作记录",12,Ui.Muted));return panel;}
    void AddCheck(string label,bool done)
    {
        if(string.IsNullOrWhiteSpace(label))return;
        var check=Ui.Check("",done);var text=Ui.Input(label);text.MinWidth=130;var row=new DockPanel();DockPanel.SetDock(check,Dock.Left);row.Children.Add(check);checks.Add((check,text));
        var actions=Ui.Row(Ui.Quiet("移除",()=>{checks.Remove((check,text));checksPanel.Children.Remove(row);return Task.CompletedTask;}));
        if(original.I("id")>0)actions.Children.Add(Ui.Quiet("转为子任务",()=>ConvertCheck(check,text,row)));
        DockPanel.SetDock(actions,Dock.Right);row.Children.Add(actions);row.Children.Add(text);checksPanel.Children.Add(row);
        check.Checked+=(_,_)=>{if(!loading&&checks.Count>0&&checks.All(x=>x.done.IsChecked==true)&&status.SelectedIndex!=2&&Ui.Confirm("检查清单已全部完成，是否同时将任务标记为完成？"))status.SelectedIndex=2;};
    }
    void AddMilestone(string name,bool confirmed,int id)
    {
        if(string.IsNullOrWhiteSpace(name))return;var check=Ui.Check("",confirmed);var text=Ui.Input(name);var row=new DockPanel();milestones.Add((check,text,id));DockPanel.SetDock(check,Dock.Left);row.Children.Add(check);var remove=Ui.Quiet("移除",()=>{milestones.Remove((check,text,id));milestonesPanel.Children.Remove(row);return Task.CompletedTask;});DockPanel.SetDock(remove,Dock.Right);row.Children.Add(remove);row.Children.Add(text);milestonesPanel.Children.Add(row);
    }
    JsonObject Collect()
    {
        var payload=original.Copy();payload["title"]=title.Text;payload["note"]=note.Text;payload["status"]=BoardWindow.Stages[Math.Max(0,status.SelectedIndex)];payload["due"]=due.SelectedDate?.ToString("yyyy-MM-dd");
        var options=original["options"] as JsonObject;var opts=options?.Copy()??new JsonObject();opts["priority"]=priority.SelectedIndex;opts["today"]=today.IsChecked==true?DateTime.Today.ToString("yyyy-MM-dd"):"";opts["repeat"]=advancedRepeat.IsChecked==true?"off":new[]{"off","daily","weekly","monthly"}[Math.Max(0,repeat.SelectedIndex)];opts["repeat_basis"]=basis.SelectedIndex==1?"completion":"due";opts["reminder"]=new[]{"off","today","before"}[Math.Max(0,reminder.SelectedIndex)];opts["reminder_time"]=reminderTime.Text;payload["options"]=opts;
        payload["pro"]=CollectProfessional();
        payload["tags"]=new JsonArray(tags.Where(x=>x.check.IsChecked==true).Select(x=>(JsonNode?)JsonValue.Create(x.id)).ToArray());
        payload["dependencies"]=new JsonArray(dependencies.Where(x=>x.check.IsChecked==true).Select(x=>(JsonNode?)JsonValue.Create(x.id)).ToArray());
        payload["checklist"]=new JsonArray(checks.Select(x=>(JsonNode?)J.Obj(("label",x.label.Text),("done",x.done.IsChecked==true))).ToArray());
        payload["milestones"]=new JsonArray(milestones.Select(x=>(JsonNode?)J.Obj(("id",x.id),("name",x.name.Text),("confirmed",x.done.IsChecked==true))).ToArray());
        payload["removed_files"]=new JsonArray(removed.Select(x=>(JsonNode?)JsonValue.Create(x)).ToArray());
        payload["new_files"]=new JsonArray(newFiles.Select(f=>(JsonNode?)f.DeepClone()).ToArray());
        foreach(var file in existingFiles)if(versions.TryGetValue(file.I("id"),out var fields)){file["version_label"]=fields.label.Text;file["version_note"]=fields.note.Text;}
        payload["file_versions"]=new JsonArray(existingFiles.Where(f=>!removed.Contains(f.I("id"))).Select(f=>(JsonNode?)J.Obj(("id",f.I("id")),("version_label",f.S("version_label")),("version_note",f.S("version_note")))).ToArray());
        payload["draft_key"]=draftKey;return payload;
    }
    async Task SaveDraft(bool force=false)
    {
        if(loading||readOnly||finished)return;
        if(savingDraft){if(!force)return;while(savingDraft)await Task.Delay(25);}
        var payload=Collect();string raw=payload.ToJsonString();if(raw==lastDraft)return;
        savingDraft=true;
        try{await board.rpc.Call("draft-save",J.Obj(("key",draftKey),("draft",payload)));lastDraft=raw;saveState.Text="草稿已保存 · "+DateTime.Now.ToString("HH:mm:ss")+"（尚未正式保存）";}
        catch(Exception ex){saveState.Text="草稿保存失败："+ex.Message;if(force)throw;}
        finally{savingDraft=false;}
    }
    async Task Save()
    {
        if(busy||readOnly)return;busy=true;draftTimer.Stop();
        try{
            if(string.IsNullOrWhiteSpace(title.Text))throw new InvalidOperationException("请输入任务标题");
            ValidateProfessional();await SaveDraft(true);JsonObject? result;
            try{result=await board.rpc.Call("save",Collect());}
            catch(InvalidOperationException ex) when(original.I("id")>0&&(ex.Message.Contains("其他页面更新")||ex.Message.Contains("版本冲突"))){result=await ResolveSaveConflict();}
            if(result is null)return;board.Accept(result);finished=true;FinishClose();
        }finally{busy=false;if(!finished)draftTimer.Start();}
    }
}
