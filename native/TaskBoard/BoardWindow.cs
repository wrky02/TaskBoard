using System;
using System.Collections.Generic;
using System.Linq;
using System.IO;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Forms=System.Windows.Forms;

namespace TaskBoard;
internal sealed partial class BoardWindow : Window
{
    internal readonly Rpc rpc;
    internal JsonObject State=new();
    readonly Forms.NotifyIcon tray;
    readonly DispatcherTimer timer=new(){Interval=TimeSpan.FromSeconds(20)};
    readonly HashSet<string> notified=new();
    internal bool Unlocked;
    bool exiting,refreshing,loadingFilters;
    string page="看板",undo="";
    DateTime undoUntil;
    StackPanel sidebar=new(),reminders=new();
    DockPanel layout=new();
    ContentControl body=new();
    TextBlock footer=new();
    TextBox search=new();
    ComboBox priority=new(),stage=new(),dueFilter=new(),tag=new(),sort=new();
    readonly HashSet<int> selected=new();
    internal static readonly string[] Stages={"计划中","进行中","完成"};
    internal static readonly string[] Priorities={"紧急","高","普通","低"};
    public BoardWindow(Rpc engine)
    {
        rpc=engine;Title="任务看板系统 · 3.0";Width=1360;Height=900;MinWidth=980;MinHeight=650;
        WindowStartupLocation=WindowStartupLocation.CenterScreen;Background=Ui.Surface;FontFamily=new FontFamily("Microsoft YaHei UI");FontSize=14;
        Icon=BitmapFrame.Create(new Uri("pack://application:,,,/taskboard.ico"));
        tray=new Forms.NotifyIcon{Icon=new System.Drawing.Icon(Application.GetResourceStream(new Uri("pack://application:,,,/taskboard.ico")).Stream),Text="任务看板系统",Visible=true};
        var menu=new Forms.ContextMenuStrip();menu.Items.Add("打开任务看板",null,(_,_)=>Dispatcher.Invoke(Reopen));
        menu.Items.Add("退出程序",null,(_,_)=>Dispatcher.Invoke(()=>{exiting=true;Close();}));tray.ContextMenuStrip=menu;
        tray.DoubleClick+=(_,_)=>Dispatcher.Invoke(Reopen);tray.BalloonTipClicked+=(_,_)=>Dispatcher.Invoke(Reopen);
        Loaded+=async (_,_)=>await Login();
        timer.Tick+=async (_,_)=>{if(Unlocked&&!refreshing){refreshing=true;try{await PollReminders();}catch(Exception ex){footer.Text=ex.Message;}finally{refreshing=false;}}};timer.Start();
        Closing+=OnClosing;Closed+=(_,_)=>{timer.Stop();tray.Visible=false;tray.Dispose();};
    }
    internal void Reopen(){Show();WindowState=WindowState.Normal;Activate();}
    void OnClosing(object? sender,System.ComponentModel.CancelEventArgs e)
    {
        if(exiting)return;
        string behavior=State["info"].S("close_behavior","ask");
        if(behavior=="ask"){
            var choice=MessageBox.Show(this,"是：退出程序（退出后不再提醒）\n否：最小化到托盘（继续提醒）\n取消：继续使用\n\n可在设置中记住默认行为。","关闭任务看板",MessageBoxButton.YesNoCancel,MessageBoxImage.Question);
            if(choice==MessageBoxResult.Cancel){e.Cancel=true;return;}
            behavior=choice==MessageBoxResult.No?"tray":"exit";
        }
        if(behavior=="tray"){e.Cancel=true;Hide();tray.ShowBalloonTip(2500,"任务看板仍在运行","双击托盘图标重新打开；右键可退出。",Forms.ToolTipIcon.Info);}
    }
    internal async Task Login()
    {
        Unlocked=false;State=new();
        try {
            var status=await rpc.Call("status");bool first=status.B("first");
            var password=new PasswordBox{MinHeight=38,Padding=new Thickness(8),Margin=new Thickness(0,8,0,8)};
            var confirm=new PasswordBox{MinHeight=38,Padding=new Thickness(8),Margin=new Thickness(0,8,0,8)};
            System.Windows.Automation.AutomationProperties.SetAutomationId(password,"loginPassword");System.Windows.Automation.AutomationProperties.SetAutomationId(confirm,"loginConfirm");
            var message=Ui.Text("");
            var panel=Ui.Column(Ui.Text("T·",48,Ui.Purple),Ui.Text(first?"创建本地工作区":"欢迎回来",28),Ui.Text(first?"设置至少 8 位密码，保护工作区入口。":"输入密码，继续处理你的任务。",14,Ui.Muted),password);
            if(first)panel.Children.Add(confirm);
            panel.Children.Add(Ui.Button(first?"创建工作区":"解锁工作区",async()=>{
                try{State=await rpc.Call("login",J.Obj(("password",password.Password),("confirm",confirm.Password)));Unlocked=true;BuildShell();await RefreshDraftNotice();await PollReminders();}catch(Exception ex){message.Text=ex.Message;}
            },"login",true));panel.Children.Add(message);panel.Children.Add(Ui.Text("原生 Windows 桌面程序 · 数据保存在本机",12,Ui.Muted));
            Content=new Border{Width=420,Padding=new Thickness(30),Background=Brushes.White,CornerRadius=new CornerRadius(18),VerticalAlignment=VerticalAlignment.Center,Child=panel};password.Focus();
        }catch(Exception ex){Content=Ui.Column(Ui.Text("启动失败",28),Ui.Text(ex.Message));}
    }
    void BuildShell()
    {
        selected.Clear();sidebar=new StackPanel{Margin=new Thickness(18)};
        var logo=Ui.Text("T·  任务看板",23,Brushes.White);sidebar.Children.Add(logo);sidebar.Children.Add(Ui.Text("NATIVE WORKSPACE  /  3.0",10,Brushes.LightSteelBlue));
        foreach(string name in new[]{"看板","我的今天","日历","已归档","回收站"})sidebar.Children.Add(Ui.Button(name,()=>{page=name;selected.Clear();Render();},"nav"+name));
        sidebar.Children.Add(Ui.Text("管理工作区",12,Brushes.LightSteelBlue));
        sidebar.Children.Add(Ui.Button("标签管理",Tags,"tags"));sidebar.Children.Add(Ui.Button("草稿恢复",Drafts,"drafts"));
        sidebar.Children.Add(Ui.Button("操作记录",Activity));sidebar.Children.Add(Ui.Button("设置与备份",Settings,"settings"));
        sidebar.Children.Add(Ui.Button("锁定工作区",async()=>{await rpc.Call("lock");await Login();},"lock"));
        sidebar.Children.Add(Ui.Text("常用视图",12,Brushes.LightSteelBlue));
        foreach(var v in State.A("views")){var view=v!.Copy();sidebar.Children.Add(Ui.Button(view.S("name"),()=>ApplyView(view)));}
        layout=new DockPanel();var rail=new Border{Width=215,Background=new SolidColorBrush(Color.FromRgb(27,36,55)),Child=Ui.Scroll(sidebar)};DockPanel.SetDock(rail,Dock.Left);layout.Children.Add(rail);
        var main=new DockPanel{Margin=new Thickness(25,18,16,12)};
        var heading=Ui.Text(State["names"].S("system_name","任务看板系统"),26);var headingRow=Ui.Row(heading,Ui.Text("   "+State["names"].S("user_name","我的工作区"),14,Ui.Muted));
        search=Ui.Input("","search");search.Width=220;search.ToolTip="搜索任务标题和正文";
        priority=Ui.Combo(new[]{"全部优先级","紧急","高","普通","低"});stage=Ui.Combo(new[]{"全部状态","计划中","进行中","完成"});
        dueFilter=Ui.Combo(new[]{"全部日期","今天到期","已逾期","本周到期","无截止日期"});sort=Ui.Combo(new[]{"手动排序","优先级排序","截止日期排序","最近更新"});
        tag=Ui.Combo(new[]{"全部标签"}.Concat(State.A("tags").Select(t=>t.S("name"))).ToArray());
        var filters=Ui.Row(search,priority,stage,tag,dueFilter,sort,Ui.Button("保存视图",SaveView));
        var quick=Ui.Input("","quickTitle");quick.Width=350;quick.ToolTip="输入任务标题，回车创建";
        async Task Quick(){if(string.IsNullOrWhiteSpace(quick.Text))return;var result=await rpc.Call("save",J.Obj(("title",quick.Text),("status","计划中")));quick.Clear();Accept(result);}
        // WPF IME composition events retain native candidate handling; ignore the
        // IME-processed key so confirming a candidate cannot create a task.
        quick.KeyDown+=async(_,e)=>{if(e.Key==Key.Enter){e.Handled=true;try{await Quick();}catch(Exception ex){Ui.Error(ex.Message);}}};
        var actions=Ui.Row(quick,Ui.Button("快速添加",Quick,"quickAdd",true),Ui.Button("详细新建",()=>Edit(null),"newTask"),Ui.Button("批量编辑",Bulk,"bulk"),Ui.Button("刷新",Refresh,"refresh"));
        reminders=new StackPanel();var top=Ui.Column(headingRow,filters,actions,reminders);DockPanel.SetDock(top,Dock.Top);main.Children.Add(top);
        footer=Ui.Text("数据已保存到本机",12,Ui.Muted);
        var bottom=Ui.Row(footer,Ui.Button("撤销上次操作",async()=>{if(undo==""||DateTime.Now>undoUntil)throw new InvalidOperationException("没有可撤销的操作，撤销有效期为 15 秒");Accept(await rpc.Call("undo",J.Obj(("key",undo))));undo="";},"undo"));DockPanel.SetDock(bottom,Dock.Bottom);main.Children.Add(bottom);
        body=new ContentControl();main.Children.Add(body);layout.Children.Add(main);Content=layout;
        search.TextChanged+=(_,_)=>Render();foreach(var c in new[]{priority,stage,tag,dueFilter,sort})c.SelectionChanged+=(_,_)=>{if(!loadingFilters)Render();};Render();
    }
    internal void Accept(JsonObject result)
    {
        State=result["state"] is JsonObject s?s.Copy():result.Copy();
        if(result.S("undo")!=""){undo=result.S("undo");undoUntil=DateTime.Now.AddSeconds(15);footer.Text="操作已完成，可在 15 秒内撤销";}else footer.Text="已保存 · "+DateTime.Now.ToString("HH:mm:ss");
        Render();
    }
    internal async Task Refresh(){Accept(await rpc.Call("state"));}
    IEnumerable<JsonObject> Filtered()
    {
        string group=page=="回收站"?"trash":page=="已归档"?"archived":"tasks";
        var tasks=State.A(group).Select(t=>t!.AsObject()).Where(t=>string.IsNullOrWhiteSpace(search.Text)||(t.S("title")+t.S("note")).Contains(search.Text,StringComparison.OrdinalIgnoreCase));
        if(page=="我的今天")tasks=tasks.Where(t=>t.S("status")!="完成"&&(t["options"].S("today")==DateTime.Today.ToString("yyyy-MM-dd")||DateTime.TryParse(t.S("due_date"),out var d)&&d.Date<=DateTime.Today));
        if(priority.SelectedIndex>0)tasks=tasks.Where(t=>t["options"].I("priority",2)==priority.SelectedIndex-1);
        if(stage.SelectedIndex>0)tasks=tasks.Where(t=>t.S("status")==Stages[stage.SelectedIndex-1]);
        if(tag.SelectedIndex>0&&tag.SelectedIndex<=State.A("tags").Count){int id=State.A("tags")[tag.SelectedIndex-1].I("id");tasks=tasks.Where(t=>t.A("tags").Any(v=>v.I("id")==id));}
        int filter=dueFilter.SelectedIndex;
        if(filter>0)tasks=tasks.Where(t=>filter==4?t.S("due_date")=="":DateTime.TryParse(t.S("due_date"),out var d)&&(filter==1?d.Date==DateTime.Today:filter==2?d.Date<DateTime.Today:d.Date>=DateTime.Today.AddDays(-((int)DateTime.Today.DayOfWeek+6)%7)&&d.Date<DateTime.Today.AddDays(7-((int)DateTime.Today.DayOfWeek+6)%7)));
        return sort.SelectedIndex switch {1=>tasks.OrderBy(t=>t["options"].I("priority",2)),2=>tasks.OrderBy(t=>t.S("due_date","9999")),3=>tasks.OrderByDescending(t=>t.S("updated_at")),_=>tasks.OrderBy(t=>t.I("sort_order"))};
    }
    void Render()
    {
        if(!Unlocked||loadingFilters)return;
        var tasks=Filtered().ToList();
        if(page=="日历"){RenderCalendar(tasks);return;}
        var columns=new Grid();foreach(var _ in Stages)columns.ColumnDefinitions.Add(new ColumnDefinition());
        for(int i=0;i<Stages.Length;i++){
            string status=Stages[i];var list=tasks.Where(t=>t.S("status")==status).ToList();
            var stack=new StackPanel();stack.Children.Add(Ui.Text(status+"  "+list.Count,18));foreach(var task in list)stack.Children.Add(TaskCard(task));
            if(list.Count==0)stack.Children.Add(Ui.Text("暂无任务",13,Ui.Muted));
            var scroller=Ui.Scroll(stack);scroller.AllowDrop=page is "看板" or "我的今天";
            scroller.Drop+=async(_,e)=>{if(e.Data.GetData("TaskBoard.Task") is int tid){try{Accept(await rpc.Call("action",J.Obj(("action","move"),("id",tid),("status",status))));}catch(Exception ex){Ui.Error(ex.Message);}}};
            Grid.SetColumn(scroller,i);columns.Children.Add(scroller);
        }
        body.Content=columns;
    }
    Border TaskCard(JsonObject task)
    {
        int id=task.I("id"),p=task["options"].I("priority",2);var content=new StackPanel();
        var check=Ui.Check("#"+id,selected.Contains(id));check.Checked+=(_,_)=>selected.Add(id);check.Unchecked+=(_,_)=>selected.Remove(id);
        content.Children.Add(Ui.Row(check,Ui.Text(Priorities[Math.Clamp(p,0,3)],12,p<2?Brushes.IndianRed:Ui.Muted)));
        var title=Ui.Button(task.S("title"),()=>Edit(task),"task-"+id);title.Content=Ui.Text(task.S("title"),16);title.HorizontalContentAlignment=HorizontalAlignment.Left;content.Children.Add(title);
        var note=task.S("note");if(note.Length>0)content.Children.Add(Ui.Text(note.Length>90?note[..90]+"…":note,12,Ui.Muted));
        if(task.S("due_date")!="")content.Children.Add(Ui.Text(Ui.Due(task.S("due_date"))+" · "+task.S("due_date"),12,task.S("due_date").CompareTo(DateTime.Today.ToString("yyyy-MM-dd"))<0&&task.S("status")!="完成"?Brushes.IndianRed:Ui.Muted));
        if(task.A("tags").Count>0){var chips=Ui.Row();foreach(var t in task.A("tags"))chips.Children.Add(new Border{CornerRadius=new CornerRadius(5),Padding=new Thickness(5,1,5,1),Margin=new Thickness(0,3,5,3),Background=TagBrush(t.S("color")),Child=Ui.Text(t.S("name"),11,Brushes.White)});content.Children.Add(chips);}
        if(task.A("checklist").Count>0)content.Children.Add(Ui.Text($"检查清单  {task.A("checklist").Count(c=>c.B("done"))}/{task.A("checklist").Count}",12,Ui.Muted));
        if(task.A("files").Count>0)content.Children.Add(Ui.Text($"附件 {task.A("files").Count}",12,Ui.Muted));
        if(task.A("blocked").Count>0)content.Children.Add(Ui.Text("等待："+string.Join("、",task.A("blocked").Select(d=>d.S("title"))),12,Brushes.DarkOrange));
        if(task["options"].S("repeat","off")!="off")content.Children.Add(Ui.Text("↻ 重复任务",12,Ui.Purple));
        var actions=Ui.Row();
        if(page=="回收站"){actions.Children.Add(ActionButton("恢复",id,"restore"));actions.Children.Add(Ui.Button("永久删除",async()=>{if(Ui.Confirm("永久删除任务和附件？此操作无法撤销。"))Accept(await rpc.Call("action",J.Obj(("id",id),("action","erase"))));}));}
        else if(page=="已归档")actions.Children.Add(ActionButton("取消归档",id,"unarchive"));
        else {
            actions.Children.Add(Ui.Button(task["options"].S("today")==DateTime.Today.ToString("yyyy-MM-dd")?"移出今日":"加入今日",async()=>{
                var payload=Payload(task);payload["options"]!["today"]=task["options"].S("today")==DateTime.Today.ToString("yyyy-MM-dd")?"":DateTime.Today.ToString("yyyy-MM-dd");Accept(await rpc.Call("save",payload));}));
            if(task.S("status")=="完成")actions.Children.Add(ActionButton("归档",id,"archive"));else actions.Children.Add(Ui.Button("完成",async()=>Accept(await rpc.Call("action",J.Obj(("id",id),("action","move"),("status","完成"))))));
            actions.Children.Add(ActionButton("删除",id,"trash"));
        }
        content.Children.Add(actions);var card=Ui.Card(content);
        Point start=default;card.PreviewMouseLeftButtonDown+=(_,e)=>start=e.GetPosition(card);
        card.MouseMove+=(_,e)=>{if(e.LeftButton==MouseButtonState.Pressed&&(e.GetPosition(card)-start).Length>12&&page is "看板" or "我的今天")DragDrop.DoDragDrop(card,new DataObject("TaskBoard.Task",id),DragDropEffects.Move);};return card;
    }
    Button ActionButton(string label,int id,string action)=>Ui.Button(label,async()=>Accept(await rpc.Call("action",J.Obj(("id",id),("action",action)))));
    internal static JsonObject Payload(JsonObject task)
    {
        var p=task.Copy();p["due"]=task["due_date"]?.DeepClone();p["tags"]=new JsonArray(task.A("tags").Select(t=>(JsonNode?)JsonValue.Create(t.I("id"))).ToArray());p["dependencies"]=new JsonArray(task.A("dependencies").Select(t=>(JsonNode?)JsonValue.Create(t.I("id"))).ToArray());return p;
    }
    internal Task Edit(JsonObject? task){var editor=new TaskEditor(this,task);editor.ShowDialog();return Task.CompletedTask;}
    internal static Brush TagBrush(string key)=>key switch {"red"=>Brushes.IndianRed,"blue"=>Brushes.SteelBlue,"green"=>Brushes.SeaGreen,"orange"=>Brushes.DarkOrange,"gray"=>Brushes.SlateGray,_=>Ui.Purple};
    async Task PollReminders()
    {
        var data=await rpc.Call("reminders");reminders.Children.Clear();var list=data.A("reminders");
        foreach(var item in list.Take(4)){
            var r=item!.Copy();var row=Ui.Row(Ui.Text("提醒 · "+r.S("title"),14,Ui.Purple),Ui.Button("打开",()=>Edit(State.A("tasks").FirstOrDefault(t=>t.I("id")==r.I("id"))?.AsObject())));
            foreach(var (label,action,minutes) in new[]{("15 分钟后","snooze",15),("1 小时后","snooze",60),("已知晓","ack",0)})row.Children.Add(Ui.Button(label,async()=>{await rpc.Call("reminder",J.Obj(("id",r.I("id")),("key",r.S("key")),("action",action),("minutes",minutes)));await PollReminders();}));
            reminders.Children.Add(row);
            if(notified.Add(r.I("id")+r.S("key"))&&!IsVisible)tray.ShowBalloonTip(6000,"任务到期提醒",r.S("title"),Forms.ToolTipIcon.Info);
        }
        var active=list.Select(r=>r.I("id")+r.S("key")).ToHashSet();notified.RemoveWhere(x=>!active.Contains(x));
        if(list.Count>4)reminders.Children.Add(Ui.Button($"还有 {list.Count-4} 项提醒，查看全部",AllReminders));
    }
    async Task AllReminders(){var data=await rpc.Call("reminders");var list=new StackPanel();foreach(var item in data.A("reminders")){var r=item!.Copy();list.Children.Add(Ui.Row(Ui.Text(r.S("title")),Ui.Button("打开",()=>Edit(State.A("tasks").First(t=>t.I("id")==r.I("id"))!.AsObject())),Ui.Button("已知晓",async()=>{await rpc.Call("reminder",J.Obj(("id",r.I("id")),("key",r.S("key")),("action","ack")));await PollReminders();})));}Ui.Dialog(this,"全部提醒",list).ShowDialog();}
}
