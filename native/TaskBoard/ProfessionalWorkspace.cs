using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using Microsoft.Win32;

namespace TaskBoard;

internal sealed partial class BoardWindow
{
    int activeProject,reviewGeneration;
    DateTime weekStart=DateTime.Today.AddDays(-((int)DateTime.Today.DayOfWeek+6)%7);
    ComboBox projectFilter=new();

    async void OnWorkspaceKey(object sender,KeyEventArgs e)
    {
        if(!Unlocked||e.Key==Key.ImeProcessed||e.IsRepeat)return;
        try {
            if(Keyboard.Modifiers==ModifierKeys.Control&&e.Key==Key.K){e.Handled=true;await CommandPalette();}
            else if(Keyboard.Modifiers==ModifierKeys.Control&&e.Key==Key.N){e.Handled=true;await Edit(null);}
            else if(Keyboard.Modifiers==ModifierKeys.Control&&e.Key==Key.F){e.Handled=true;search.Focus();search.SelectAll();}
            else if(Keyboard.Modifiers==(ModifierKeys.Control|ModifierKeys.Shift)&&e.Key==Key.N){e.Handled=true;quickInput.Focus();}
            else if(e.Key==Key.F1){e.Handled=true;Ui.Dialog(this,"键盘快捷键",Ui.Column(Ui.Text("让操作保持连贯",22),Ui.Text("Ctrl+K  全局搜索与命令\nCtrl+N  新建任务\nCtrl+Shift+N  快速记录\nCtrl+F  筛选当前视图\nCtrl+S  保存任务（详情中）\nEnter  打开选中的任务或提交当前输入\nEsc  关闭搜索、返回列表\nShift+F10  打开任务操作菜单\nF1  查看快捷键")),520,470).ShowDialog();}
        }catch(Exception ex){Ui.Error(ex.Message);}
    }
    ComboBox ProjectFilter()
    {
        var projects=State.A("projects").Where(p=>!p.B("archived")).ToList();
        if(activeProject>0&&!projects.Any(p=>p.I("id")==activeProject))activeProject=0;
        projectFilter=Ui.Combo(new[]{"全部项目"}.Concat(projects.Select(p=>p.S("name"))).ToArray(),projects.FindIndex(p=>p.I("id")==activeProject)+1,"projectFilter");
        AutomationProperties.SetName(projectFilter,"按项目筛选");
        projectFilter.SelectionChanged+=(_,_)=>{activeProject=projectFilter.SelectedIndex>0?projects[projectFilter.SelectedIndex-1].I("id"):0;selected.Clear();Render();};
        return projectFilter;
    }
    bool RenderProfessional(List<JsonObject> tasks)
    {
        UpdateNames();
        switch(page){
            case "任务列表":case "收件箱":body.Content=TaskList(tasks);return true;
            case "我的今天":body.Content=Today(tasks);return true;
            case "周计划":body.Content=WeekAgenda(tasks);return true;
            case "周回顾":_ = RenderReview();return true;
            case "附件中心":body.Content=AttachmentCenter();return true;
            case "看板" when activeProject>0:body.Content=ProjectBoard(tasks);return true;
            default:return false;
        }
    }
    internal FrameworkElement TaskList(IEnumerable<JsonObject> tasks)
    {
        var items=tasks.ToList();
        if(items.Count==0)return Ui.Card(Ui.Column(Ui.Text(page=="收件箱"?"收件箱已清空":"这里暂时没有任务",21),Ui.Text(page=="收件箱"?"随时把想法放进来，再决定项目、日期或下一步行动。":"试试调整关键词、项目或筛选条件。",13,Ui.Muted)));
        var list=new ListBox{ItemsSource=items,Background=Brushes.Transparent,BorderThickness=new Thickness(0),HorizontalContentAlignment=HorizontalAlignment.Stretch,Padding=new Thickness(0)};
        AutomationProperties.SetAutomationId(list,"taskList");AutomationProperties.SetName(list,"任务列表，方向键选择，Enter打开");
        VirtualizingPanel.SetIsVirtualizing(list,true);VirtualizingPanel.SetVirtualizationMode(list,VirtualizationMode.Recycling);ScrollViewer.SetCanContentScroll(list,true);ScrollViewer.SetHorizontalScrollBarVisibility(list,ScrollBarVisibility.Disabled);
        var panel=new FrameworkElementFactory(typeof(VirtualizingStackPanel));list.ItemsPanel=new ItemsPanelTemplate(panel);
        var row=new FrameworkElementFactory(typeof(VirtualTaskRow));row.SetBinding(VirtualTaskRow.BoardProperty,new Binding{RelativeSource=new RelativeSource(RelativeSourceMode.FindAncestor,typeof(BoardWindow),1)});list.ItemTemplate=new DataTemplate{VisualTree=row};
        list.KeyDown+=async(_,e)=>{if(e.Key==Key.Enter&&list.SelectedItem is JsonObject task){e.Handled=true;await Edit(task);}if(e.Key==Key.Apps||(e.Key==Key.F10&&Keyboard.Modifiers==ModifierKeys.Shift)){if(list.SelectedItem is JsonObject item){var menu=TaskCard(item).ContextMenu;menu.PlacementTarget=list;menu.IsOpen=true;e.Handled=true;}}};
        return list;
    }
    internal UIElement ListRow(JsonObject task)
    {
        var row=new DockPanel();int id=task.I("id");
        var more=Ui.Quiet("⋯",()=>Task.CompletedTask,"listMore-"+id);more.Width=36;more.MinHeight=30;AutomationProperties.SetName(more,"任务操作："+task.S("title"));more.Click+=(_,_)=>{var menu=TaskCard(task).ContextMenu;menu.PlacementTarget=more;menu.IsOpen=true;};DockPanel.SetDock(more,Dock.Right);row.Children.Add(more);
        if(batchMode){var check=Ui.Check("",selected.Contains(id));AutomationProperties.SetName(check,"选择任务："+task.S("title"));check.Checked+=(_,_)=>{selected.Add(id);selectionText.Text=$"已选择 {selected.Count} 项";};check.Unchecked+=(_,_)=>{selected.Remove(id);selectionText.Text=$"已选择 {selected.Count} 项";};DockPanel.SetDock(check,Dock.Left);row.Children.Add(check);}
        var title=Ui.Quiet(task.S("title"),()=>Edit(task),"task-"+id);title.HorizontalContentAlignment=HorizontalAlignment.Left;title.Margin=new Thickness(0);title.Padding=new Thickness(0);title.MinHeight=26;title.FontWeight=FontWeights.SemiBold;
        var project=State.A("projects").FirstOrDefault(p=>p.I("id")==task["pro"].I("project_id"));string metadata=task.S("status")+"   ·   "+Priorities[Math.Clamp(task["options"].I("priority",2),0,3)]+"   ·   "+Ui.Due(task.S("due_date"));
        if(project is not null)metadata+="   ·   "+project.S("name");if(task["pro"].B("focus"))metadata+="   ★ 今日重点";if(task["pro"].S("plan_date")!="")metadata+="   ·   计划 "+task["pro"].S("plan_date");
        row.Children.Add(Ui.Column(title,Ui.Text(metadata,11,Ui.Muted)));var card=Ui.Card(row);card.Padding=new Thickness(14,Ui.Compact?5:10,8,Ui.Compact?5:10);card.Margin=new Thickness(0,0,4,Ui.Compact?4:8);return card;
    }
    FrameworkElement Today(List<JsonObject> tasks)
    {
        var root=new DockPanel();var focus=tasks.Count(t=>t["pro"].B("focus"));var info=Ui.Column(Ui.Text("今天最重要的事",19),Ui.Text($"已选 {focus} 项重点。任务菜单可设为重点；今日计划与截止日期分别保存。",12,Ui.Muted));info.Margin=new Thickness(0,0,0,12);DockPanel.SetDock(info,Dock.Top);root.Children.Add(info);
        root.Children.Add(TaskList(tasks.OrderByDescending(t=>t["pro"].B("focus")).ThenBy(t=>t["options"].I("priority",2)).ThenBy(t=>t.S("due_date"))));return root;
    }
    FrameworkElement ProjectBoard(List<JsonObject> tasks)
    {
        var states=State.A("project_states").Where(s=>s.I("project_id")==activeProject).OrderBy(s=>s.I("sort_order")).ToList();
        if(states.Count==0)return TaskList(tasks);
        var grid=new Grid();for(int i=0;i<states.Count;i++)grid.ColumnDefinitions.Add(new ColumnDefinition{Width=new GridLength(1,GridUnitType.Star),MinWidth=220});
        for(int i=0;i<states.Count;i++){
            var state=states[i]!.Copy();var laneTasks=tasks.Where(t=>t["pro"].I("state_id")==state.I("id")).ToList();var lane=new DockPanel();var heading=Ui.Row(Ui.Text(state.S("name"),16),Ui.Chip(laneTasks.Count.ToString(),Ui.Muted));heading.Margin=new Thickness(0,0,0,12);DockPanel.SetDock(heading,Dock.Top);lane.Children.Add(heading);
            var list=TaskList(laneTasks);lane.Children.Add(list);list.AllowDrop=true;list.Drop+=async(_,e)=>{if(e.Data.GetData("TaskBoard.Task") is int id){e.Handled=true;var task=State.A("tasks").FirstOrDefault(t=>t.I("id")==id);if(task is not null)try{await PatchProfessional(task!.AsObject(),p=>{p["project_id"]=activeProject;p["state_id"]=state.I("id");});}catch(Exception ex){Ui.Error(ex.Message);}}};
            var shell=Ui.Card(lane);shell.Background=Ui.Color("#EEF0F7");shell.Margin=new Thickness(0,0,12,0);Grid.SetColumn(shell,i);grid.Children.Add(shell);
        }
        return new ScrollViewer{Content=grid,HorizontalScrollBarVisibility=ScrollBarVisibility.Auto,VerticalScrollBarVisibility=ScrollBarVisibility.Disabled};
    }
    void AddProfessionalTaskMenu(ContextMenu menu,JsonObject task)
    {
        void Add(string label,Func<Task> action){var item=new MenuItem{Header=label};item.Click+=async(_,_)=>{try{await action();}catch(Exception ex){Ui.Error(ex.Message);}};menu.Items.Add(item);}
        menu.Items.Add(new Separator());
        Add(task["pro"].B("focus")?"取消今日重点":"设为今日重点",()=>PatchProfessional(task,p=>p["focus"]=!p.B("focus")));
        Add("安排计划日期…",()=>Schedule(task));
        Add(task["pro"].B("inbox")?"移出收件箱":"移入收件箱",()=>PatchProfessional(task,p=>p["inbox"]=!p.B("inbox")));
        Add("移到项目…",()=>AssignProject(task));
    }
    async Task PatchProfessional(JsonObject task,Action<JsonObject> change)
    {
        var payload=Payload(task);var pro=payload["pro"] as JsonObject??new JsonObject();payload["pro"]=pro;change(pro);Accept(await rpc.Call("save",payload));
    }
    Task Schedule(JsonObject task)
    {
        var date=new DatePicker{SelectedDate=DateTime.TryParse(task["pro"].S("plan_date"),out var value)?value:DateTime.Today};Window? window=null;
        window=Ui.Dialog(this,"安排计划日期",Ui.Column(Ui.Text(task.S("title"),19),Ui.Text("安排何时做这项工作，不改变截止日期。",12,Ui.Muted),date,Ui.Row(Ui.Button("保存计划",async()=>{await PatchProfessional(task,p=>p["plan_date"]=date.SelectedDate?.ToString("yyyy-MM-dd")??"");window!.Close();},"savePlan",true),Ui.Button("取消计划",async()=>{await PatchProfessional(task,p=>p["plan_date"]="");window!.Close();}))),470,310);window.ShowDialog();return Task.CompletedTask;
    }
    Task AssignProject(JsonObject task)
    {
        var projects=State.A("projects").Where(p=>!p.B("archived")).ToList();var combo=Ui.Combo(new[]{"无项目"}.Concat(projects.Select(p=>p.S("name"))).ToArray(),projects.FindIndex(p=>p.I("id")==task["pro"].I("project_id"))+1);Window? dialog=null;
        dialog=Ui.Dialog(this,"移动到项目",Ui.Column(Ui.Text(task.S("title"),18),combo,Ui.Text("父子任务的项目归属必须一致；后端会检查关系冲突。",12,Ui.Muted),Ui.Button("移动",async()=>{int id=combo.SelectedIndex>0?projects[combo.SelectedIndex-1].I("id"):0;await PatchProfessional(task,p=>{p["project_id"]=id;p["state_id"]=0;});dialog!.Close();},primary:true)),480,300);dialog.ShowDialog();return Task.CompletedTask;
    }
    async Task Projects()
    {
        Window? dialog=null;var panel=new StackPanel();
        void Draw(){panel.Children.Clear();foreach(var item in State.A("projects")){var project=item!.Copy();panel.Children.Add(Ui.Card(Ui.Column(Ui.Text(project.S("name")+(project.B("archived")?" · 已归档":""),18),Ui.Text(project.S("goal"),13,Ui.Muted),Ui.Row(Ui.Button("打开",()=>{activeProject=project.I("id");page="看板";dialog!.Close();BuildShell();}),Ui.Button("编辑",async()=>{await EditProject(project);Draw();}),Ui.Button("状态列",async()=>{await ProjectStates(project);Draw();},"projectStates"),Ui.Button(project.B("archived")?"恢复项目":"归档项目",async()=>{if(!Ui.Confirm("确认"+(project.B("archived")?"恢复":"归档")+"该项目？任务保留，项目归档后退出活跃计划。"))return;Accept(await rpc.Call("pro-project-archive",J.Obj(("id",project.I("id")),("archived",!project.B("archived")))));Draw();})))));}}
        var name=Ui.Input("","projectName");Ui.Hint(name,"项目名称，按 Enter 创建…");
        async Task Create(){if(string.IsNullOrWhiteSpace(name.Text))return;Accept(await rpc.Call("pro-project-save",J.Obj(("name",name.Text))));name.Clear();Draw();}
        Ui.OnSubmit(name,Create);Draw();dialog=Ui.Dialog(this,"项目管理",Ui.Column(Ui.Text("项目与流程",25),Ui.Text("每个项目保存目标、时间范围和自己的状态列。",13,Ui.Muted),Ui.Row(name,Ui.Button("创建项目",Create,"createProject",true)),panel),790,720);dialog.ShowDialog();var filters=Filters();BuildShell();ApplyFilters(filters);await Task.CompletedTask;
    }
    Task EditProject(JsonObject project)
    {
        var name=Ui.Input(project.S("name"));var goal=Ui.Input(project.S("goal"));var note=Ui.Input(project.S("note"));note.AcceptsReturn=true;note.MinHeight=90;
        var start=new DatePicker{SelectedDate=DateTime.TryParse(project.S("start_date"),out var s)?s:null};var target=new DatePicker{SelectedDate=DateTime.TryParse(project.S("target_date"),out var t)?t:null};Window? dialog=null;
        dialog=Ui.Dialog(this,"编辑项目",Ui.Column(Ui.Text("项目名称"),name,Ui.Text("目标"),goal,Ui.Row(Ui.Column(Ui.Text("开始日期"),start),Ui.Column(Ui.Text("目标日期"),target)),Ui.Text("说明"),note,Ui.Button("保存项目",async()=>{Accept(await rpc.Call("pro-project-save",J.Obj(("id",project.I("id")),("name",name.Text),("goal",goal.Text),("note",note.Text),("color",project.S("color","purple")),("start_date",start.SelectedDate?.ToString("yyyy-MM-dd")??""),("target_date",target.SelectedDate?.ToString("yyyy-MM-dd")??""))));dialog!.Close();},primary:true)),620,620);dialog.ShowDialog();return Task.CompletedTask;
    }
    Task ProjectStates(JsonObject project)
    {
        var list=new StackPanel();var input=Ui.Input("","stateName");var kind=Ui.Combo(new[]{"待办","进行中","完成","取消"},0,"stateKind");
        string[] kinds={"todo","doing","done","cancelled"};
        void Draw(){list.Children.Clear();var states=State.A("project_states").Where(s=>s.I("project_id")==project.I("id")).OrderBy(s=>s.I("sort_order")).ToList();foreach(var item in states){var state=item!.Copy();list.Children.Add(Ui.Card(Ui.Row(Ui.Text(state.S("name")+" · "+kind.Items[Math.Max(0,Array.IndexOf(kinds,state.S("kind")))]),Ui.Button("重命名",async()=>{var name=Ui.Prompt(this,"状态名称",state.S("name"));if(name is null)return;state["name"]=name;Accept(await rpc.Call("pro-state-save",state));Draw();}),Ui.Button("上移",async()=>{int index=states.FindIndex(s=>s.I("id")==state.I("id"));if(index==0)return;var previous=states[index-1]!.Copy();int old=state.I("sort_order");state["sort_order"]=previous.I("sort_order");previous["sort_order"]=old;Accept(await rpc.Call("pro-state-save",state));Accept(await rpc.Call("pro-state-save",previous));Draw();}),Ui.Button("删除并迁移…",async()=>{var targets=states.Where(s=>s.I("id")!=state.I("id")).ToList();if(targets.Count==0)throw new InvalidOperationException("至少保留一个状态列。");var choice=Ui.Combo(targets.Select(s=>s.S("name")).ToArray());Window? w=null;w=Ui.Dialog(this,"选择迁移目标",Ui.Column(Ui.Text("此列任务将移到下列状态，删除不能直接撤销。"),choice,Ui.Button("确认迁移并删除",async()=>{Accept(await rpc.Call("pro-state-delete",J.Obj(("id",state.I("id")),("target_id",targets[choice.SelectedIndex].I("id")))));w!.Close();Draw();},primary:true)),500,280);w.ShowDialog();}))));}}
        async Task Add(){if(string.IsNullOrWhiteSpace(input.Text))return;Accept(await rpc.Call("pro-state-save",J.Obj(("project_id",project.I("id")),("name",input.Text),("kind",kinds[kind.SelectedIndex]),("sort_order",State.A("project_states").Count+1))));input.Clear();Draw();}
        Ui.OnSubmit(input,Add);Draw();Ui.Dialog(this,"项目状态列",Ui.Column(Ui.Text(project.S("name"),23),Ui.Text("自定义列映射为待办、进行中、完成或取消。取消不满足完成依赖。",12,Ui.Muted),Ui.Row(input,kind,Ui.Button("添加状态列",Add,"createState",true)),list),780,660).ShowDialog();return Task.CompletedTask;
    }
    FrameworkElement WeekAgenda(List<JsonObject> tasks)
    {
        var root=new DockPanel();var heading=Ui.Row(Ui.Button("上一周",()=>{weekStart=weekStart.AddDays(-7);Render();}),Ui.Text(weekStart.ToString("M月d日")+" — "+weekStart.AddDays(6).ToString("M月d日"),19),Ui.Button("下一周",()=>{weekStart=weekStart.AddDays(7);Render();}),Ui.Button("本周",()=>{weekStart=DateTime.Today.AddDays(-((int)DateTime.Today.DayOfWeek+6)%7);Render();}));DockPanel.SetDock(heading,Dock.Top);root.Children.Add(heading);
        var agenda=new StackPanel();for(int i=0;i<7;i++){var day=weekStart.AddDays(i);string key=day.ToString("yyyy-MM-dd");var items=tasks.Where(t=>t["pro"].S("plan_date")==key||(t["pro"].S("plan_date")==""&&t.S("due_date")==key)).ToList();var section=Ui.Column(Ui.Text(day.ToString("M月d日 dddd")+(day==DateTime.Today?" · 今天":""),17),Ui.Text($"{items.Count} 项任务 · 预计 {items.Sum(t=>t["pro"].I("estimate_minutes"))} 分钟",12,Ui.Muted));foreach(var task in items)section.Children.Add(ListRow(task));if(items.Count==0)section.Children.Add(Ui.Text("还没有安排；任务菜单中的「安排计划日期」可加入本日。",12,Ui.Muted));agenda.Children.Add(Ui.Card(section));}root.Children.Add(Ui.Scroll(agenda));return root;
    }
    async Task RenderReview()
    {
        int generation=++reviewGeneration;body.Content=Ui.Text("正在整理本周记录…",17);
        try {
            var report=await rpc.Call("pro-review",J.Obj(("from",weekStart.ToString("yyyy-MM-dd")),("to",weekStart.AddDays(6).ToString("yyyy-MM-dd"))));
            if(generation!=reviewGeneration||page!="周回顾")return;
            var panel=Ui.Column(Ui.Text(weekStart.ToString("M月d日")+" — "+weekStart.AddDays(6).ToString("M月d日")+" 的回顾",23),Ui.Row(Ui.Chip($"完成 {report.I("completed_count")}",Ui.Purple),Ui.Chip($"重新打开 {report.I("reopened_count")}",Ui.Muted),Ui.Chip($"改期 {report.I("postponed_count")}",Ui.Muted)),Ui.Row(Ui.Button("上一周",()=>{weekStart=weekStart.AddDays(-7);Render();}),Ui.Button("下一周",()=>{weekStart=weekStart.AddDays(7);Render();})));
            foreach(var(key,title) in new[]{("completed","这周完成的工作"),("stale","长期没有推进的任务"),("upcoming","即将到来的截止日期"),("focus","当前重点")}){var content=new StackPanel();foreach(var t in report.A(key)){var summary=t!.Copy();content.Children.Add(Ui.Quiet(summary.S("title"),()=>EditTaskById(summary.I("id"))));}if(content.Children.Count==0)content.Children.Add(Ui.Text("暂无记录",12,Ui.Muted));panel.Children.Add(Ui.Section(title,content));}body.Content=Ui.Scroll(panel);
        }catch(Exception ex){if(page=="周回顾")body.Content=Ui.Column(Ui.Text("无法读取回顾"),Ui.Text(ex.Message),Ui.Button("重试",()=>RenderReview()));}
    }
    FrameworkElement AttachmentCenter()
    {
        var all=State.A("tasks").Concat(State.A("archived")).SelectMany(t=>t.A("files").Select(f=>(task:t!,file:f!))).Where(x=>string.IsNullOrWhiteSpace(search.Text)||(x.file.S("filename")+x.task.S("title")).Contains(search.Text,StringComparison.OrdinalIgnoreCase)).ToList();
        var panel=Ui.Column(Ui.Text($"{all.Count} 个附件 · 搜索框可查找文件名或所属任务",14,Ui.Muted));
        foreach(var (task,file) in all){var f=file.Copy();int tid=task.I("id");var actions=Ui.Row(Ui.Button("打开任务",()=>EditTaskById(tid)),Ui.Button("另存为",async()=>{var save=new SaveFileDialog{FileName=Path.GetFileName(f.S("filename"))};if(save.ShowDialog(this)==true)await rpc.Call("attachment-export",J.Obj(("file_id",f.I("id")),("path",save.FileName)));}));panel.Children.Add(Ui.Card(Ui.Column(Ui.Text(f.S("filename"),16),Ui.Text(task.S("title")+" · "+(f.I("size")/1024.0).ToString("N1")+" KB · "+f.S("version_label"),12,Ui.Muted),actions)));}
        if(all.Count==0)panel.Children.Add(Ui.Text("没有匹配的附件。可以在任务详情中拖入文件或粘贴图片。",14,Ui.Muted));return Ui.Scroll(panel);
    }
    internal Task EditTaskById(int id)
    {
        var task=State.A("tasks").Concat(State.A("archived")).Concat(State.A("trash")).FirstOrDefault(t=>t.I("id")==id);if(task is null)throw new InvalidOperationException("任务不存在或已经永久删除。");return Edit(task.AsObject());
    }
    async Task CommandPalette()
    {
        var query=Ui.Input("","globalSearch");Ui.Hint(query,"搜索所有任务、检查清单和附件名称…");var scope=Ui.Combo(new[]{"活动任务","包括归档","包括回收站"},0,"searchScope");var results=new ListBox{MinHeight=220,MaxHeight=410,DisplayMemberPath="Label",Background=Ui.Panel,Foreground=Ui.Ink,BorderThickness=new Thickness(0)};var hint=Ui.Text("Enter 打开 · Esc 返回 · Ctrl+N 新建",12,Ui.Muted);Window? dialog=null;int request=0;
        async Task Search(){int mine=++request;try{var result=await rpc.Call("pro-query",J.Obj(("query",query.Text),("scope",scope.SelectedIndex==0?"active":"all"),("limit",100)));if(mine!=request)return;var tasks=result.A("tasks");if(scope.SelectedIndex==1)tasks=new JsonArray(tasks.Where(t=>State.A("tasks").Concat(State.A("archived")).Any(x=>x.I("id")==t.I("id"))).Select(t=>t!.DeepClone()).ToArray());results.ItemsSource=tasks.Select(t=>new SearchResult(t!.I("id"),t.S("title")+"   ·   "+t.S("status"))).ToList();hint.Text=$"找到 {result.I("total")} 项，显示前 {tasks.Count} 项；Enter 打开";if(results.Items.Count>0)results.SelectedIndex=0;}catch(Exception ex){hint.Text=ex.Message;}}
        async Task Open(){if(results.SelectedItem is SearchResult item){dialog!.Close();await EditTaskById(item.Id);}}
        var delay=new DispatcherTimer{Interval=TimeSpan.FromMilliseconds(200)};delay.Tick+=async(_,_)=>{delay.Stop();await Search();};query.TextChanged+=(_,_)=>{delay.Stop();delay.Start();};scope.SelectionChanged+=async(_,_)=>await Search();Ui.OnSubmit(query,Open);results.MouseDoubleClick+=async(_,_)=>await Open();results.KeyDown+=async(_,e)=>{if(e.Key==Key.Enter){e.Handled=true;await Open();}};
        dialog=Ui.Dialog(this,"全局搜索与命令",Ui.Column(Ui.Text("快速找到下一步",23),query,scope,results,hint,Ui.Row(Ui.Button("新建任务",async()=>{dialog.Close();await Edit(null);}),Ui.Button("查看收件箱",()=>{dialog.Close();page="收件箱";Render();}),Ui.Button("快捷键帮助",()=>Ui.Error("Ctrl+K 搜索 · Ctrl+N 新建 · Ctrl+F 当前筛选 · F1 完整帮助")))),760,650);dialog.KeyDown+=(_,e)=>{if(e.Key==Key.Escape){e.Handled=true;dialog.Close();}};dialog.Closed+=(_,_)=>{delay.Stop();request++;};dialog.Loaded+=async(_,_)=>{query.Focus();await Search();};dialog.ShowDialog();await Task.CompletedTask;
    }
    sealed record SearchResult(int Id,string Label);
}

// Real item containers are created only for visible rows and recycled on scroll.
internal sealed class VirtualTaskRow:ContentControl
{
    public static readonly DependencyProperty BoardProperty=DependencyProperty.Register(nameof(Board),typeof(BoardWindow),typeof(VirtualTaskRow),new PropertyMetadata(null,(_,_)=>{}));
    public BoardWindow? Board {get=>(BoardWindow?)GetValue(BoardProperty);set=>SetValue(BoardProperty,value);}
    public VirtualTaskRow(){HorizontalContentAlignment=HorizontalAlignment.Stretch;DataContextChanged+=(_,_)=>Refresh();Loaded+=(_,_)=>Refresh();}
    void Refresh(){if(Board is not null&&DataContext is JsonObject task)Content=Board.ListRow(task);}
}
