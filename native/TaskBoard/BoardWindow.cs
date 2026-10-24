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
    readonly NativePreferences preferences=NativePreferences.Load();
    readonly DispatcherTimer preferenceTimer=new(){Interval=TimeSpan.FromMilliseconds(450)};
    readonly DispatcherTimer searchTimer=new(){Interval=TimeSpan.FromMilliseconds(180)};
    TextBlock brandName=new(),workspaceName=new(),breadcrumbText=new();
    TextBox quickInput=new();
    TaskEditor? currentEditor;
    FrameworkElement? editorContent;
    Grid workspaceHost=new();
    Border detailHost=new();
    bool closingEditor;
    readonly Forms.NotifyIcon tray;
    readonly DispatcherTimer timer=new(){Interval=TimeSpan.FromSeconds(20)};
    readonly HashSet<string> notified=new();
    internal bool Unlocked;
    bool exiting,refreshing,loadingFilters,batchMode;
    readonly Dictionary<string,Button> navigation=new();
    readonly DispatcherTimer undoTimer=new(){Interval=TimeSpan.FromSeconds(1)};
    TextBlock pageHeading=new(),pageSubtitle=new(),selectionText=new();
    StackPanel stats=new();
    Border batchBar=new();
    Button undoButton=new();
    string page="看板",undo="";
    DateTime undoUntil;
    DateTime renderedDay=DateTime.Today;
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
        rpc=engine;Ui.ApplyPreferences(preferences);Title=preferences.ApplicationName;Ui.ApplicationName=Title;Width=Math.Min(1360,SystemParameters.WorkArea.Width-24);Height=Math.Min(900,SystemParameters.WorkArea.Height-24);MinWidth=Math.Min(900,Width);MinHeight=Math.Min(600,Height);
        System.Windows.Automation.AutomationProperties.SetAutomationId(this,"mainWindow");
        WindowStartupLocation=WindowStartupLocation.CenterScreen;Background=Ui.Surface;FontFamily=Ui.Font;FontSize=14;
        Icon=BitmapFrame.Create(new Uri("pack://application:,,,/taskboard.ico"));
        tray=new Forms.NotifyIcon{Icon=new System.Drawing.Icon(Application.GetResourceStream(new Uri("pack://application:,,,/taskboard.ico")).Stream),Text="任务看板系统",Visible=true};
        var menu=new Forms.ContextMenuStrip();menu.Items.Add("打开任务看板",null,(_,_)=>Dispatcher.Invoke(Reopen));
        menu.Items.Add("退出程序",null,(_,_)=>Dispatcher.Invoke(()=>{exiting=true;Close();}));tray.ContextMenuStrip=menu;
        tray.DoubleClick+=(_,_)=>Dispatcher.Invoke(Reopen);tray.BalloonTipClicked+=(_,_)=>Dispatcher.Invoke(Reopen);
        SourceInitialized+=(_,_)=>preferences.Restore(this);
        preferenceTimer.Tick+=(_,_)=>{preferenceTimer.Stop();preferences.Capture(this);};
        void Remember(){if(IsLoaded){preferenceTimer.Stop();preferenceTimer.Start();}}
        LocationChanged+=(_,_)=>Remember();StateChanged+=(_,_)=>Remember();SizeChanged+=(_,_)=>Remember();
        searchTimer.Tick+=(_,_)=>{searchTimer.Stop();Render();};
        PreviewKeyDown+=OnWorkspaceKey;
        Loaded+=async (_,_)=>await Login();
        timer.Tick+=async (_,_)=>{if(Unlocked&&!refreshing){refreshing=true;try{await PollReminders();if(renderedDay!=DateTime.Today){renderedDay=DateTime.Today;Render();}}catch(Exception ex){footer.Text=ex.Message;}finally{refreshing=false;}}};timer.Start();
        undoTimer.Tick+=(_,_)=>{undoButton.Visibility=undo!=""&&DateTime.Now<=undoUntil?Visibility.Visible:Visibility.Collapsed;};undoTimer.Start();
        SizeChanged+=(_,_)=>UpdateDetailLayout();
        Closing+=OnClosing;Closed+=(_,_)=>{timer.Stop();undoTimer.Stop();preferenceTimer.Stop();searchTimer.Stop();tray.Visible=false;tray.Dispose();};
    }
    internal void Reopen(){Show();if(WindowState==WindowState.Minimized)WindowState=preferences.Maximized?WindowState.Maximized:WindowState.Normal;Activate();}
    async void OnClosing(object? sender,System.ComponentModel.CancelEventArgs e)
    {
        preferences.Capture(this);if(closingEditor){e.Cancel=true;return;}
        string behavior=exiting?"exit":State["info"].S("close_behavior","ask");
        if(behavior=="ask"){
            var choice=MessageBox.Show(this,"是：退出程序（退出后不再提醒）\n否：最小化到托盘（继续提醒）\n取消：继续使用\n\n可在设置中记住默认行为。","关闭任务看板",MessageBoxButton.YesNoCancel,MessageBoxImage.Question);
            if(choice==MessageBoxResult.Cancel){e.Cancel=true;return;}
            behavior=choice==MessageBoxResult.No?"tray":"exit";
        }
        if(currentEditor is not null){e.Cancel=true;closingEditor=true;try{await currentEditor.RequestClosePaneAsync();closingEditor=false;if(behavior=="tray"){Hide();return;}exiting=true;Dispatcher.BeginInvoke(Close);}catch(Exception ex){exiting=false;closingEditor=false;Ui.Error("未能保存草稿，窗口保持打开："+ex.Message);}return;}
        if(behavior=="tray"){e.Cancel=true;Hide();tray.ShowBalloonTip(2500,preferences.ApplicationName+"仍在运行","双击托盘图标重新打开；右键可退出。",Forms.ToolTipIcon.Info);}
    }
    internal async Task Login()
    {
        Unlocked=false;State=new();
        try {
            var status=await rpc.Call("status");bool first=status.B("first");
            var password=new PasswordBox{MinHeight=38,Padding=new Thickness(8),Margin=new Thickness(0,8,0,8)};
            var confirm=new PasswordBox{MinHeight=38,Padding=new Thickness(8),Margin=new Thickness(0,8,0,8)};
            System.Windows.Automation.AutomationProperties.SetAutomationId(password,"loginPassword");System.Windows.Automation.AutomationProperties.SetAutomationId(confirm,"loginConfirm");
            System.Windows.Automation.AutomationProperties.SetName(password,"工作区密码");System.Windows.Automation.AutomationProperties.SetName(confirm,"再次输入密码");
            var message=Ui.Text("");bool loggingIn=false;Button? login=null;
            async Task SubmitLogin(){
                if(loggingIn)return;loggingIn=true;login!.IsEnabled=false;
                try{State=await rpc.Call("login",J.Obj(("password",password.Password),("confirm",confirm.Password)));Unlocked=true;BuildShell();await RefreshDraftNotice();await PollReminders();}
                catch(Exception ex){message.Text=ex.Message;password.Focus();}
                finally{loggingIn=false;login.IsEnabled=true;}
            }
            password.KeyDown+=async(_,e)=>{if(e.Key==Key.Enter){e.Handled=true;if(first&&confirm.Password.Length==0)confirm.Focus();else await SubmitLogin();}};
            confirm.KeyDown+=async(_,e)=>{if(e.Key==Key.Enter){e.Handled=true;await SubmitLogin();}};
            var panel=Ui.Column(Ui.Logo(54),Ui.Text(first?"创建本地工作区":"欢迎回来",28),Ui.Text(first?"设置至少 8 位密码，保护工作区入口。":"输入密码，继续处理你的任务。",14,Ui.Muted),password);
            if(first)panel.Children.Add(confirm);
            login=Ui.Button(first?"创建工作区":"解锁工作区",SubmitLogin,"login",true);panel.Children.Add(login);if(!first)panel.Children.Add(Ui.Quiet("忘记密码？使用恢复密钥",RecoverVaultLogin,"recoverWorkspace"));panel.Children.Add(message);panel.Children.Add(Ui.Text("原生 Windows 桌面程序 · 数据保存在本机",12,Ui.Muted));
            Content=new Border{Width=420,Padding=new Thickness(30),Background=Ui.Panel,CornerRadius=new CornerRadius(18),VerticalAlignment=VerticalAlignment.Center,Child=panel};Dispatcher.BeginInvoke(()=>password.Focus());
        }catch(Exception ex){Content=Ui.Column(Ui.Text("启动失败",28),Ui.Text(ex.Message));}
    }
    void BuildShell()
    {
        selected.Clear();batchMode=false;navigation.Clear();
        sidebar=new StackPanel{Margin=new Thickness(16,24,16,12)};
        brandName=Ui.Text("",16,Brushes.White);brandName.FontWeight=FontWeights.SemiBold;brandName.MaxWidth=130;brandName.TextTrimming=TextTrimming.CharacterEllipsis;System.Windows.Automation.AutomationProperties.SetAutomationId(brandName,"applicationName");
        workspaceName=Ui.Text("",10,Ui.Color("#A9B5CD"));workspaceName.MaxWidth=130;System.Windows.Automation.AutomationProperties.SetAutomationId(workspaceName,"workspaceName");
        var brand=Ui.Row(Ui.Logo(38),new Border{Margin=new Thickness(11,0,0,0),Child=Ui.Column(brandName,workspaceName)});
        sidebar.Children.Add(new Border{Child=brand,Margin=new Thickness(4,0,0,30)});
        foreach(var (name,glyph) in new[]{("收件箱","\uE715"),("我的今天","\uE706"),("看板","\uE80A"),("任务列表","\uE8FD"),("周计划","\uE787"),("日历","\uE787"),("周回顾","\uE9D2"),("附件中心","\uE723"),("已归档","\uE7B8"),("回收站","\uE74D")}){
            var button=Nav(name,glyph,()=>{page=name;selected.Clear();Render();},"nav"+name);navigation[name]=button;sidebar.Children.Add(button);
        }
        sidebar.Children.Add(RailLabel("工作区管理"));
        sidebar.Children.Add(Nav("项目管理","\uE8F1",Projects,"projects"));sidebar.Children.Add(Nav("标签管理","\uE8EC",Tags,"tags"));sidebar.Children.Add(Nav("草稿恢复","\uE70F",Drafts,"drafts"));sidebar.Children.Add(Nav("操作记录","\uE81C",Activity,"activity"));sidebar.Children.Add(Nav("恢复中心","\uE777",RecoveryCenter,"recoveryCenter"));
        sidebar.Children.Add(RailLabel("常用视图"));
        foreach(var v in State.A("views")){var view=v!.Copy();sidebar.Children.Add(Nav(view.S("name"),"\uE734",()=>ApplyView(view)));}
        if(State.A("views").Count==0)sidebar.Children.Add(new Border{Padding=new Thickness(12,4,8,8),Child=Ui.Text("保存筛选条件，在这里快速打开",11,Ui.Color("#8995AC"))});
        var railContent=new DockPanel();var railBottom=Ui.Column(Nav("设置与备份","\uE713",Settings,"settings"),Nav("锁定工作区","\uE72E",async()=>{if(currentEditor is not null)await currentEditor.RequestClosePaneAsync();await rpc.Call("lock");await Login();},"lock"),Ui.Text("  ●  本地工作区",10,Ui.Color("#A9B5CD")));railBottom.Margin=new Thickness(16,12,16,16);DockPanel.SetDock(railBottom,Dock.Bottom);railContent.Children.Add(railBottom);railContent.Children.Add(Ui.Scroll(sidebar));
        layout=new DockPanel();var rail=new Border{Width=218,Background=Ui.Color("#1C2538"),Child=railContent};DockPanel.SetDock(rail,Dock.Left);layout.Children.Add(rail);
        var main=new DockPanel{Margin=new Thickness(28,20,28,12)};
        var breadcrumb=new DockPanel{Margin=new Thickness(0,0,0,20)};
        var profile=Ui.Row(Ui.Chip("●  本地保存",Ui.Color("#498D7C"),Ui.Color("#EAF4EF")),new Border{Margin=new Thickness(10,0,0,0),Child=Ui.Chip(State["names"].S("user_name", "我") is string user&&user!=""?user:"我",Ui.Purple)});DockPanel.SetDock(profile,Dock.Right);breadcrumb.Children.Add(profile);
        breadcrumbText=Ui.Text("",11,Ui.Muted);breadcrumb.Children.Add(breadcrumbText);UpdateNames();
        pageHeading=Ui.Text("工作看板",26);pageHeading.FontWeight=FontWeights.Bold;pageHeading.Margin=new Thickness(0,3,0,6);pageSubtitle=Ui.Text("把想法变成行动，让重要的事有序发生。",12,Ui.Muted);
        var heading=new DockPanel{Margin=new Thickness(0,0,0,18)};var create=Ui.Button("＋  新建任务",()=>Edit(null),"newTask",true);create.VerticalAlignment=VerticalAlignment.Center;create.Margin=new Thickness(12,0,0,0);DockPanel.SetDock(create,Dock.Right);heading.Children.Add(create);heading.Children.Add(Ui.Column(new TextBlock{Text="WORK OVERVIEW",Foreground=Ui.Purple,FontSize=9,FontWeight=FontWeights.SemiBold},pageHeading,pageSubtitle));
        stats=new StackPanel{Visibility=ActualHeight<740?Visibility.Collapsed:Visibility.Visible};stats.Margin=new Thickness(0,0,0,16);
        search=Ui.Input("","search");search.Width=190;Ui.Hint(search,"搜索任务标题或正文…");
        priority=Ui.Combo(new[]{"全部优先级","紧急","高","普通","低"});stage=Ui.Combo(new[]{"全部状态","计划中","进行中","完成"});
        dueFilter=Ui.Combo(new[]{"全部日期","今天到期","已逾期","本周到期","无截止日期"});sort=Ui.Combo(new[]{"手动排序","优先级排序","截止日期排序","最近更新"});
        tag=Ui.Combo(new[]{"全部标签"}.Concat(State.A("tags").Select(t=>t.S("name"))).ToArray());
        var filters=Ui.Row(search,priority,stage,tag,dueFilter,sort,ProjectFilter(),Ui.Quiet("保存视图",SaveView),Ui.Quiet("全局搜索",CommandPalette,"commandPalette"),Ui.Quiet("↻",Refresh,"refresh"));
        var quick=quickInput=Ui.Input("","quickTitle");Ui.Hint(quick,"＋  记录一个新任务，按 Enter 添加…");quick.Margin=new Thickness(0,0,10,0);bool creating=false;
        async Task Quick(){if(creating||string.IsNullOrWhiteSpace(quick.Text))return;creating=true;try{var result=await rpc.Call("save",J.Obj(("title",quick.Text),("status","计划中"),("pro",J.Obj(("inbox",page=="收件箱"),("project_id",activeProject)))));quick.Clear();Accept(result);}finally{creating=false;}}
        Ui.OnSubmit(quick,Quick);
        var entry=new DockPanel{Margin=new Thickness(0,0,0,8)};var add=Ui.Button("添加任务",Quick,"quickAdd",true);add.Margin=new Thickness(0);DockPanel.SetDock(add,Dock.Right);entry.Children.Add(add);var batch=Ui.Button("批量选择",()=>{batchMode=!batchMode;selected.Clear();Render();},"batchSelect");batch.Margin=new Thickness(10,0,0,0);DockPanel.SetDock(batch,Dock.Right);entry.Children.Add(batch);entry.Children.Add(quick);
        selectionText=Ui.Text("",12,Ui.Purple);var apply=Ui.Button("编辑选中任务",Bulk,"bulk",true);
        var selectAll=Ui.Button("全选当前结果",()=>{foreach(var task in Filtered())selected.Add(task.I("id"));Render();});
        var cancel=Ui.Quiet("取消选择",()=>{batchMode=false;selected.Clear();Render();return Task.CompletedTask;});
        batchBar=new Border{Background=Ui.Color("#EDEAFF"),CornerRadius=new CornerRadius(8),Padding=new Thickness(12,3,8,3),Margin=new Thickness(0,4,0,10),Child=Ui.Row(selectionText,selectAll,apply,cancel),Visibility=Visibility.Collapsed};
        reminders=new StackPanel();var top=Ui.Column(breadcrumb,heading,stats,entry,filters,batchBar,reminders);top.Margin=new Thickness(0,0,0,12);DockPanel.SetDock(top,Dock.Top);main.Children.Add(top);
        footer=Ui.Text("●  数据已保存到本机",11,Ui.Muted);
        undoButton=Ui.Quiet("撤销操作",async()=>{if(undo==""||DateTime.Now>undoUntil)return;Accept(await rpc.Call("undo",J.Obj(("key",undo))));undo="";undoButton.Visibility=Visibility.Collapsed;},"undo");undoButton.Visibility=Visibility.Collapsed;
        var bottom=new DockPanel{Margin=new Thickness(0,6,0,0)};DockPanel.SetDock(undoButton,Dock.Right);bottom.Children.Add(undoButton);bottom.Children.Add(footer);DockPanel.SetDock(bottom,Dock.Bottom);main.Children.Add(bottom);
        body=new ContentControl{HorizontalContentAlignment=HorizontalAlignment.Stretch,VerticalContentAlignment=VerticalAlignment.Stretch};main.Children.Add(body);layout.Children.Add(main);
        if(detailHost.Child is not null)detailHost.Child=null;
        workspaceHost=new Grid();workspaceHost.ColumnDefinitions.Add(new ColumnDefinition());workspaceHost.ColumnDefinitions.Add(new ColumnDefinition{Width=new GridLength(0)});workspaceHost.ColumnDefinitions.Add(new ColumnDefinition{Width=new GridLength(0)});workspaceHost.Children.Add(layout);
        var splitter=new GridSplitter{Width=6,HorizontalAlignment=HorizontalAlignment.Stretch,VerticalAlignment=VerticalAlignment.Stretch,Background=Ui.Color("#E6E8F1")};Grid.SetColumn(splitter,1);workspaceHost.Children.Add(splitter);
        detailHost=new Border{Background=Ui.Panel,BorderBrush=Ui.Color("#E5E8F1"),BorderThickness=new Thickness(1,0,0,0),Child=editorContent};Grid.SetColumn(detailHost,2);workspaceHost.Children.Add(detailHost);Content=workspaceHost;UpdateDetailLayout();
        search.TextChanged+=(_,_)=>{searchTimer.Stop();searchTimer.Start();};foreach(var c in new[]{priority,stage,tag,dueFilter,sort})c.SelectionChanged+=(_,_)=>{if(!loadingFilters)Render();};Render();
    }
    TextBlock RailLabel(string text){var t=Ui.Text(text,10,Ui.Color("#7E8CA7"));t.Margin=new Thickness(12,24,0,8);return t;}
    Button Nav(string name,string glyph,Action action,string id="")=>Nav(name,glyph,()=>{action();return Task.CompletedTask;},id);
    Button Nav(string name,string glyph,Func<Task> action,string id="")
    {
        var b=Ui.Button(name,action,id);b.Content=Ui.Row(Ui.Glyph(glyph,16,Ui.Color("#A9B5CD")),new TextBlock{Text=name,FontSize=13,VerticalAlignment=VerticalAlignment.Center});b.Background=Brushes.Transparent;b.Foreground=Ui.Color("#BBC5D9");b.BorderThickness=new Thickness(0);b.HorizontalContentAlignment=HorizontalAlignment.Left;b.Padding=new Thickness(12,12,8,12);b.Margin=new Thickness(0,2,0,2);return b;
    }
    void UpdateOverview()
    {
        pageHeading.Text=page=="看板"?"工作看板":page;
        pageSubtitle.Text=page switch {"我的今天"=>"专注今天的安排。加入今日计划，不改变截止日期。","日历"=>"按日期安排工作，让每个截止时间都清晰可见。","已归档"=>"妥善收起已完成的工作，随时回顾。","回收站"=>"误删的任务可以恢复，永久删除后无法撤销。",_=>"把想法变成行动，让重要的事有序发生。"};
        foreach(var (name,b) in navigation){bool active=name==page;b.Background=active?Ui.Purple:Brushes.Transparent;b.Foreground=active?Brushes.White:Ui.Color("#BBC5D9");if(b.Content is WrapPanel row&&row.Children[0] is TextBlock icon)icon.Foreground=active?Brushes.White:Ui.Color("#A9B5CD");}
        batchBar.Visibility=batchMode?Visibility.Visible:Visibility.Collapsed;selectionText.Text=$"已选择 {selected.Count} 项   ";
        stats.Children.Clear();var all=State.A("tasks");var grid=new Grid();for(int i=0;i<4;i++)grid.ColumnDefinitions.Add(new ColumnDefinition());
        var values=new[]{("全部任务",all.Count,"\uE80A","#7063ED","#F0EEFF"),("进行中",all.Count(t=>t.S("status")=="进行中"),"\uE916","#C5903F","#FBF3E6"),("已完成",all.Count(t=>t.S("status")=="完成"),"\uE73E","#4D9B83","#EBF5EF"),("已逾期",all.Count(t=>t.S("status")!="完成"&&DateTime.TryParse(t.S("due_date"),out var d)&&d.Date<DateTime.Today),"\uE823","#D2767D","#FCEDF0")};
        for(int i=0;i<values.Length;i++){var(label,count,icon,ink,bg)=values[i];var panel=new DockPanel();var symbol=Ui.Glyph(icon,19,Ui.Color(ink));symbol.Margin=new Thickness(0);var tile=new Border{Width=38,Height=38,Background=Ui.Color(bg),CornerRadius=new CornerRadius(10),Child=symbol,VerticalAlignment=VerticalAlignment.Center};symbol.HorizontalAlignment=HorizontalAlignment.Center;DockPanel.SetDock(tile,Dock.Right);panel.Children.Add(tile);var number=Ui.Text(count.ToString(),25);number.FontFamily=new FontFamily("Segoe UI");number.FontWeight=FontWeights.SemiBold;number.Margin=new Thickness(0,3,0,0);panel.Children.Add(Ui.Column(Ui.Text(label,11,Ui.Muted),number));var card=Ui.Card(panel);card.Padding=new Thickness(16,11,16,11);card.Margin=new Thickness(0,0,i==3?0:12,0);Grid.SetColumn(card,i);grid.Children.Add(card);}stats.Children.Add(grid);
    }
    internal void Accept(JsonObject result)
    {
        if(result["state"] is JsonObject full) State=full.Copy();
        else if(result["patch"] is JsonObject patch) ApplyPatch(patch);
        else if(result["names"] is JsonObject) State["names"]=result["names"]!.DeepClone();
        UpdateNames();
        if(result.S("undo")!=""){undo=result.S("undo");undoUntil=DateTime.Now.AddSeconds(15);undoButton.Visibility=Visibility.Visible;footer.Text="操作已完成，可在 15 秒内撤销";}else footer.Text="已保存 · "+DateTime.Now.ToString("HH:mm:ss");
        Render();
    }
    void ApplyPatch(JsonObject patch)
    {
        var changed=new HashSet<int>();
        foreach(var id in patch.A("removed_ids")) if(int.TryParse(id?.ToString(),out var value)) changed.Add(value);
        foreach(var group in new[]{"tasks","trash","archived"})
            foreach(var task in patch.A(group)) if(int.TryParse(task?["id"]?.ToString(),out var value)) changed.Add(value);
        foreach(var group in new[]{"tasks","trash","archived"})
            for(int i=State.A(group).Count-1;i>=0;i--)
                if(changed.Contains(State.A(group)[i]?.I("id")??0)) State.A(group).RemoveAt(i);
        foreach(var group in new[]{"tasks","trash","archived"})
        {
            var current=State.A(group);
            foreach(var task in patch.A(group)) current.Add(task?.DeepClone());
        }
    }
    internal async Task Refresh(){Accept(await rpc.Call("state"));}
    void UpdateNames()
    {
        string app=State["names"].S("system_name",preferences.ApplicationName).Trim();string space=State["names"].S("user_name",preferences.WorkspaceName).Trim();
        if(app=="")app="任务看板系统";if(space=="")space="我的工作空间";
        Ui.ApplicationName=app;Title=app+" · "+space;brandName.Text=app;workspaceName.Text=space;breadcrumbText.Text=space+"    /    "+page;
        tray.Text=(app+" · "+space).Length>63?(app+" · "+space)[..63]:app+" · "+space;
        if(tray.ContextMenuStrip?.Items.Count>0)tray.ContextMenuStrip.Items[0].Text="打开 "+app;
        if(preferences.ApplicationName!=app||preferences.WorkspaceName!=space){preferences.ApplicationName=app;preferences.WorkspaceName=space;preferences.Save();}
    }
    IEnumerable<JsonObject> Filtered()
    {
        string group=page=="回收站"?"trash":page=="已归档"?"archived":"tasks";
        var tasks=State.A(group).Select(t=>t!.AsObject()).Where(t=>string.IsNullOrWhiteSpace(search.Text)||(t.S("title")+t.S("note")+string.Join(" ",t.A("checklist").Select(c=>c.S("label")))+string.Join(" ",t.A("files").Select(f=>f.S("filename")))).Contains(search.Text,StringComparison.OrdinalIgnoreCase));
        if(page is not "回收站" and not "已归档")tasks=tasks.Where(t=>!t["pro"].B("project_archived"));
        if(activeProject>0)tasks=tasks.Where(t=>t["pro"].I("project_id")==activeProject);
        if(page=="收件箱")tasks=tasks.Where(t=>t["pro"].B("inbox"));
        if(page=="我的今天")tasks=tasks.Where(t=>t.S("status")!="完成"&&!t["pro"].B("cancelled")&&(t["pro"].B("focus")||t["pro"].S("plan_date")==DateTime.Today.ToString("yyyy-MM-dd")||t["options"].S("today")==DateTime.Today.ToString("yyyy-MM-dd")||DateTime.TryParse(t.S("due_date"),out var d)&&d.Date<=DateTime.Today));
        if(priority.SelectedIndex>0)tasks=tasks.Where(t=>t["options"].I("priority",2)==priority.SelectedIndex-1);
        if(stage.SelectedIndex>0)tasks=tasks.Where(t=>t.S("status")==Stages[stage.SelectedIndex-1]);
        if(tag.SelectedIndex>0&&tag.SelectedIndex<=State.A("tags").Count){int id=State.A("tags")[tag.SelectedIndex-1].I("id");tasks=tasks.Where(t=>t.A("tags").Any(v=>v.I("id")==id));}
        int filter=dueFilter.SelectedIndex;
        if(filter>0)tasks=tasks.Where(t=>filter==4?t.S("due_date")=="":DateTime.TryParse(t.S("due_date"),out var d)&&(filter==1?d.Date==DateTime.Today:filter==2?d.Date<DateTime.Today:d.Date>=DateTime.Today.AddDays(-((int)DateTime.Today.DayOfWeek+6)%7)&&d.Date<DateTime.Today.AddDays(7-((int)DateTime.Today.DayOfWeek+6)%7)));
        return sort.SelectedIndex switch {1=>tasks.OrderBy(t=>t["options"].I("priority",2)),2=>tasks.OrderBy(t=>t.S("due_date","9999")),3=>tasks.OrderByDescending(t=>t.S("updated_at")),_=>tasks.OrderBy(t=>t.I("sort_order"))};
    }
    void Render()
    {
        if(!Unlocked||loadingFilters)return;UpdateOverview();
        var tasks=Filtered().ToList();
        if(currentEditor is not null&&page is "看板" or "我的今天" or "收件箱" or "任务列表"){body.Content=TaskList(tasks);return;}
        if(RenderProfessional(tasks))return;
        if(page=="日历"){RenderCalendar(tasks);return;}
        var columns=new Grid();foreach(var _ in Stages)columns.ColumnDefinitions.Add(new ColumnDefinition());
        for(int i=0;i<Stages.Length;i++){
            string status=Stages[i];var list=tasks.Where(t=>t.S("status")==status).ToList();
            var lane=new DockPanel();var header=new DockPanel{Margin=new Thickness(4,0,4,14)};
            var plus=Ui.Quiet("＋",()=>Edit(J.Obj(("status",status))));plus.ToolTip="在“"+status+"”中新建任务";plus.Padding=new Thickness(6,0,6,0);plus.MinHeight=24;plus.Margin=new Thickness(0);DockPanel.SetDock(plus,Dock.Right);header.Children.Add(plus);
            header.Children.Add(Ui.Row(Ui.Text("●",11,Ui.Color(new[]{"#9B91E7","#D5A258","#67A68F"}[i])),new TextBlock{Text=status,FontSize=14,FontWeight=FontWeights.SemiBold,Foreground=Ui.Ink,Margin=new Thickness(8,3,8,0)},Ui.Chip(list.Count.ToString(),Ui.Muted,Ui.Color("#E6E8F1"))));DockPanel.SetDock(header,Dock.Top);lane.Children.Add(header);
            var stack=new StackPanel();foreach(var task in list)stack.Children.Add(TaskCard(task));
            if(list.Count==0){var empty=Ui.Column(Ui.Glyph(i==2?"\uE73E":"\uE8A5",27,Ui.Color("#B6BACE")),Ui.Text(i==2?"完成的任务会出现在这里":"暂无任务",13,Ui.Muted),Ui.Text(tasks.Count==0&&search.Text!=""?"试试其他关键词或筛选条件":"让工作一步步向前",11,Ui.Color("#9AA3B6")));foreach(FrameworkElement item in empty.Children)item.HorizontalAlignment=HorizontalAlignment.Center;stack.Children.Add(new Border{Child=empty,Padding=new Thickness(8,34,8,34),BorderBrush=Ui.Color("#DFE3EE"),BorderThickness=new Thickness(1),CornerRadius=new CornerRadius(10),Margin=new Thickness(0,0,0,12)});}
            if(page is "看板" or "我的今天"){var add=Ui.Quiet("＋  添加任务",()=>Edit(J.Obj(("status",status))));add.HorizontalAlignment=HorizontalAlignment.Stretch;add.Foreground=Ui.Muted;stack.Children.Add(add);}
            var scroller=Ui.Scroll(stack);scroller.AllowDrop=page is "看板" or "我的今天";
            scroller.Drop+=async(_,e)=>{if(e.Data.GetData("TaskBoard.Task") is int tid){try{Accept(await rpc.Call("action",J.Obj(("action","move"),("id",tid),("status",status))));}catch(Exception ex){Ui.Error(ex.Message);}}};lane.Children.Add(scroller);
            var shell=new Border{Background=Ui.Color("#EEF0F7"),CornerRadius=new CornerRadius(12),Padding=new Thickness(12,14,12,12),Margin=new Thickness(0,0,i==2?0:14,0),Child=lane};Grid.SetColumn(shell,i);columns.Children.Add(shell);
        }
        body.Content=columns;
    }
    internal Border TaskCard(JsonObject task)
    {
        int id=task.I("id"),p=Math.Clamp(task["options"].I("priority",2),0,3);var content=new StackPanel();
        var top=new DockPanel();var menu=new ContextMenu{FontFamily=Ui.Font};
        void Menu(string label,Func<Task> action){var item=new MenuItem{Header=label,Padding=new Thickness(10,7,16,7)};item.Click+=async(_,_)=>{try{await action();}catch(Exception ex){Ui.Error(ex.Message);}};menu.Items.Add(item);}
        Task Act(string action)=>RunAction(id,action);
        Menu("打开任务",()=>Edit(task));
        if(page=="回收站"){Menu("恢复任务",()=>Act("restore"));Menu("永久删除…",async()=>{if(Ui.Confirm("永久删除任务和附件？此操作无法撤销。"))await Act("erase");});}
        else if(page=="已归档")Menu("取消归档",()=>Act("unarchive"));
        else{
            Menu(task["options"].S("today")==DateTime.Today.ToString("yyyy-MM-dd")?"移出今日计划":"加入今日计划",async()=>{var payload=Payload(task);payload["options"]!["today"]=task["options"].S("today")==DateTime.Today.ToString("yyyy-MM-dd")?"":DateTime.Today.ToString("yyyy-MM-dd");Accept(await rpc.Call("save",payload));});
            if(task.S("status")=="完成")Menu("归档任务",()=>Act("archive"));else Menu("标记为完成",async()=>Accept(await rpc.Call("action",J.Obj(("id",id),("action","move"),("status","完成")))));
            Menu("复制任务",()=>Act("duplicate"));menu.Items.Add(new Separator());Menu("移至回收站",()=>Act("trash"));
            AddProfessionalTaskMenu(menu,task);
        }
        var more=Ui.Quiet("⋯",()=>Task.CompletedTask,"more-"+id);more.ContextMenu=menu;more.ToolTip="更多操作（也可右键任务卡片）";more.FontSize=20;more.Padding=new Thickness(5,0,5,0);more.MinHeight=24;more.Margin=new Thickness(4,-3,-4,0);more.Click+=(_,_)=>{menu.PlacementTarget=more;menu.Placement=System.Windows.Controls.Primitives.PlacementMode.Bottom;menu.IsOpen=true;};DockPanel.SetDock(more,Dock.Right);top.Children.Add(more);
        var chips=Ui.Row();if(batchMode){var check=Ui.Check("",selected.Contains(id));System.Windows.Automation.AutomationProperties.SetName(check,"选择任务："+task.S("title"));check.Checked+=(_,_)=>{selected.Add(id);selectionText.Text=$"已选择 {selected.Count} 项   ";};check.Unchecked+=(_,_)=>{selected.Remove(id);selectionText.Text=$"已选择 {selected.Count} 项   ";};chips.Children.Add(check);}
        chips.Children.Add(Ui.Chip(Priorities[p],Ui.Color(new[]{"#9F354A","#815612","#586176","#366C5D"}[p]),Ui.Color(new[]{"#FBECEF","#FCF3E4","#F2F3F7","#ECF5F2"}[p])));
        foreach(var t in task.A("tags").Take(2)){var ink=TagBrush(t.S("color"));var bg=ink.Clone();bg.Opacity=.10;chips.Children.Add(Ui.Chip(t.S("name"),ink,bg));}if(task.A("tags").Count>2)chips.Children.Add(Ui.Chip("+"+(task.A("tags").Count-2),Ui.Muted));top.Children.Add(chips);content.Children.Add(top);
        var title=Ui.Quiet(task.S("title"),()=>Edit(task),"task-"+id);var titleText=Ui.Text(task.S("title"),15);titleText.FontWeight=FontWeights.SemiBold;titleText.Margin=new Thickness(0);title.Content=titleText;title.HorizontalContentAlignment=HorizontalAlignment.Stretch;title.Padding=new Thickness(0,8,0,4);title.Margin=new Thickness(0,2,0,0);title.MinHeight=0;content.Children.Add(title);
        var note=task.S("note");if(note.Length>0){var preview=Ui.Text(note.Replace("\r","").Replace("\n"," "),12,Ui.Muted);preview.MaxHeight=36;preview.LineHeight=18;preview.TextTrimming=TextTrimming.CharacterEllipsis;preview.Margin=new Thickness(0,3,0,8);content.Children.Add(preview);}
        if(task.A("blocked").Count>0)content.Children.Add(Ui.Text("等待："+string.Join("、",task.A("blocked").Select(d=>d.S("title"))),11,Ui.Color("#BE914E")));
        var metadata=Ui.Row();string date=task.S("due_date");bool overdue=date!=""&&date.CompareTo(DateTime.Today.ToString("yyyy-MM-dd"))<0&&task.S("status")!="完成";
        var dueLabel=Ui.Text(date!=""?(task.S("status")=="完成"?"截止 "+date[5..]:"◷  "+Ui.Due(date)):"未设截止日期",11,overdue?Ui.Color("#C66976"):Ui.Muted);dueLabel.ToolTip=date;dueLabel.Margin=new Thickness(0,4,14,4);metadata.Children.Add(dueLabel);
        if(task.A("checklist").Count>0)metadata.Children.Add(Ui.Text($"☑ {task.A("checklist").Count(c=>c.B("done"))}/{task.A("checklist").Count}   ",11,Ui.Muted));
        if(task.A("files").Count>0)metadata.Children.Add(Ui.Text($"附件 {task.A("files").Count}   ",11,Ui.Muted));
        if(task["options"].S("today")==DateTime.Today.ToString("yyyy-MM-dd"))metadata.Children.Add(Ui.Text("今日  ",11,Ui.Purple));
        if(task["options"].S("repeat","off")!="off")metadata.Children.Add(Ui.Text("↻ 重复",11,Ui.Purple));
        content.Children.Add(new Border{BorderBrush=Ui.Color("#EEF0F5"),BorderThickness=new Thickness(0,1,0,0),Margin=new Thickness(0,10,0,0),Padding=new Thickness(0,7,0,0),Child=metadata});
        var card=Ui.Card(content);card.Padding=new Thickness(14,12,14,10);card.ContextMenu=menu;card.MouseEnter+=(_,_)=>card.BorderBrush=Ui.Color("#C5BEF7");card.MouseLeave+=(_,_)=>card.BorderBrush=Ui.Color("#E5E8F1");
        card.AllowDrop=page is "看板" or "我的今天";
        card.Drop+=async(_,e)=>{if(e.Data.GetData("TaskBoard.Task") is int source){e.Handled=true;if(source==id)return;try{var lane=State.A("tasks").Where(t=>t.S("status")==task.S("status")&&t.I("id")!=source).OrderBy(t=>t.I("sort_order")).ToList();int index=lane.FindIndex(t=>t.I("id")==id);Accept(await rpc.Call("action",J.Obj(("id",source),("action","move"),("status",task.S("status")),("index",Math.Max(0,index)))));}catch(Exception ex){Ui.Error(ex.Message);}}};
        Point start=default;bool canDrag=false;card.PreviewMouseLeftButtonDown+=(_,e)=>{start=e.GetPosition(card);canDrag=true;var node=e.OriginalSource as DependencyObject;while(node is not null&&node!=card){if(node is System.Windows.Controls.Primitives.ButtonBase){canDrag=false;break;}node=VisualTreeHelper.GetParent(node);}};
        card.MouseMove+=(_,e)=>{if(canDrag&&e.LeftButton==MouseButtonState.Pressed&&(e.GetPosition(card)-start).Length>12&&page is "看板" or "我的今天"){canDrag=false;DragDrop.DoDragDrop(card,new DataObject("TaskBoard.Task",id),DragDropEffects.Move);}};return card;
    }
    async Task RunAction(int id,string action)=>Accept(await rpc.Call("action",J.Obj(("id",id),("action",action))));
    Button ActionButton(string label,int id,string action)=>Ui.Button(label,async()=>Accept(await rpc.Call("action",J.Obj(("id",id),("action",action)))));
    internal static JsonObject Payload(JsonObject task)
    {
        var p=task.Copy();p["due"]=task["due_date"]?.DeepClone();p["tags"]=new JsonArray(task.A("tags").Select(t=>(JsonNode?)JsonValue.Create(t.I("id"))).ToArray());p["dependencies"]=new JsonArray(task.A("dependencies").Select(t=>(JsonNode?)JsonValue.Create(t.I("id"))).ToArray());return p;
    }
    internal async Task Edit(JsonObject? task)
    {
        if(currentEditor is not null)await currentEditor.RequestClosePaneAsync();
        if(task is null&&activeProject>0)task=J.Obj(("pro",J.Obj(("project_id",activeProject))));
        var editor=task is not null&&task.I("id")==0?new TaskEditor(this,null,task):new TaskEditor(this,task);await OpenEditor(editor);
    }
    async Task OpenEditor(TaskEditor editor)
    {
        if(currentEditor is not null)await currentEditor.RequestClosePaneAsync();currentEditor=editor;
        editorContent=editor.DetachContentForPane(()=>{if(currentEditor!=editor)return;currentEditor=null;editorContent=null;detailHost.Child=null;UpdateDetailLayout();Render();});detailHost.Child=editorContent;UpdateDetailLayout();Render();
    }
    void UpdateDetailLayout()
    {
        if(workspaceHost.ColumnDefinitions.Count<3)return;bool open=currentEditor is not null;bool narrow=open&&ActualWidth<1150;
        layout.Visibility=narrow?Visibility.Collapsed:Visibility.Visible;workspaceHost.ColumnDefinitions[0].Width=narrow?new GridLength(0):new GridLength(1,GridUnitType.Star);workspaceHost.ColumnDefinitions[1].Width=new GridLength(open&&!narrow?6:0);workspaceHost.ColumnDefinitions[2].Width=open?(narrow?new GridLength(1,GridUnitType.Star):new GridLength(Math.Clamp(ActualWidth*.43,470,620))):new GridLength(0);detailHost.Visibility=open?Visibility.Visible:Visibility.Collapsed;stats.Visibility=open||ActualHeight<740?Visibility.Collapsed:Visibility.Visible;
    }
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
