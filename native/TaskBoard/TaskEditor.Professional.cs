using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Automation;

namespace TaskBoard;
internal sealed partial class TaskEditor
{
    ComboBox project=null!,projectState=null!,parentTask=null!,repeatFrequency=null!,repeatBasis=null!,repeatScope=null!,repeatMissed=null!,repeatDependency=null!,monthlyNth=null!,monthlyWeekday=null!;
    DatePicker planDate=null!,seriesStart=null!,seriesEnd=null!;
    TextBox estimate=null!,seriesInterval=null!,seriesCount=null!,seriesMonthDay=null!,seriesTimezone=null!;
    CheckBox inbox=null!,focus=null!,advancedRepeat=null!,seriesOptIn=null!,seriesPaused=null!;
    readonly List<int> projectIds=new(),stateIds=new(),parentIds=new();
    readonly List<CheckBox> weekdays=new();
    readonly List<(CheckBox check,int id)> related=new();
    readonly List<(DatePicker date,TextBox start,TextBox end)> timeBlocks=new();
    readonly List<(DatePicker date,TextBox time,JsonObject original)> independentReminders=new();
    readonly StackPanel timeBlocksPanel=new(),remindersPanel=new(),childrenPanel=new();
    readonly TextBlock seriesPreview=Ui.Text("保存前可预览未来 5 次发生日期。",12,Ui.Muted);
    JsonObject professionalOriginal=new();
    string previewRule="";
    bool changingProject;

    UIElement BuildProfessional(JsonObject value,JsonObject? task)
    {
        professionalOriginal=(value["pro"] as JsonObject)?.Copy()??new JsonObject();
        var pro=professionalOriginal;
        project=Ui.Combo(Array.Empty<string>(),-1,"editorProject");project.MinWidth=180;
        projectState=Ui.Combo(Array.Empty<string>(),-1,"editorProjectState");parentTask=Ui.Combo(Array.Empty<string>(),-1,"editorParent");parentTask.MinWidth=180;
        planDate=new DatePicker{SelectedDate=ParseDate(pro.S("plan_date")),MinHeight=38,Margin=new Thickness(0,4,8,6)};AutomationProperties.SetAutomationId(planDate,"editorPlanDate");
        estimate=Ui.Input(pro.I("estimate_minutes").ToString(),"editorEstimate");estimate.Width=90;Ui.Hint(estimate,"预计分钟");
        focus=Ui.Check("今日重点（最多 3 项）",pro.B("focus"));AutomationProperties.SetAutomationId(focus,"editorFocus");
        inbox=Ui.Check("保留在收件箱，稍后整理",pro["inbox"] is null?pro.I("project_id")==0:pro.B("inbox"));
        projectIds.Add(0);var names=new List<string>{"未分配项目"};
        foreach(var item in board.State.A("projects").Where(p=>!p.B("archived")||p.I("id")==pro.I("project_id"))){projectIds.Add(item.I("id"));names.Add(item.S("name")+(item.B("archived")?"（已归档）":""));}
        project.ItemsSource=names;project.SelectedIndex=Math.Max(0,projectIds.IndexOf(pro.I("project_id")));
        RefreshProjectOptions(pro.I("state_id"),pro.I("parent_id"));
        project.SelectionChanged+=(_,_)=>{if(!changingProject)RefreshProjectOptions(0,0);};
        foreach(var block in pro.A("time_blocks"))AddTimeBlock(block.S("start"),block.S("end"));
        var organization=Ui.Column(Ui.Row(Field("所属项目",project),Field("项目流程",projectState)),Field("父任务（可选）",parentTask),inbox);
        var planning=Ui.Column(Ui.Row(Field("准备哪天做",planDate),Field("预计分钟",estimate)),focus,Ui.Text("计划日期和时段独立于截止日期。时段按本机时区记录，换时区显示对应当地时间。",12,Ui.Muted),timeBlocksPanel,Ui.Quiet("＋ 添加工作时段",()=>{AddTimeBlock("","");return Task.CompletedTask;}));
        var childrenActions=Ui.Row();
        if(task.I("id")>0){childrenActions.Children.Add(Ui.Button("新增子任务",CreateChild,"addSubtask"));childrenActions.Children.Add(Ui.Quiet("刷新子任务",LoadChildren));}
        else childrenActions.Children.Add(Ui.Text("先保存任务，再为它创建独立子任务。",12,Ui.Muted));
        foreach(var child in AllTasks().Where(t=>t?["pro"].I("parent_id")==task.I("id")&&task.I("id")>0))AddChildRow(child);
        return Ui.Column(Ui.Section("项目与任务结构",organization,false),Ui.Section("个人计划",planning,false),Ui.Section("子任务",Ui.Column(childrenPanel,childrenActions),childrenPanel.Children.Count>0));
    }
    static StackPanel Field(string label,UIElement control)=>Ui.Column(Ui.Text(label,11,Ui.Muted),control);
    static DateTime? ParseDate(string value)=>DateTime.TryParse(value,out var date)?date.Date:null;
    IEnumerable<JsonNode?> AllTasks()=>board.State.A("tasks").Concat(board.State.A("archived")).Concat(board.State.A("trash"));
    int SelectedProject=>project.SelectedIndex>=0&&project.SelectedIndex<projectIds.Count?projectIds[project.SelectedIndex]:0;
    void RefreshProjectOptions(int selectedState,int selectedParent)
    {
        changingProject=true;
        try{
            stateIds.Clear();var states=new List<string>();
            foreach(var item in board.State.A("project_states").Where(s=>s.I("project_id")==SelectedProject).OrderBy(s=>s.I("sort_order"))){stateIds.Add(item.I("id"));states.Add(item.S("name"));}
            if(states.Count==0){stateIds.Add(0);states.Add("使用任务状态");}
            projectState.ItemsSource=states;int selected=stateIds.IndexOf(selectedState);projectState.SelectedIndex=selected>=0?selected:0;
            parentIds.Clear();parentIds.Add(0);var parents=new List<string>{"无父任务（独立任务）"};
            foreach(var item in AllTasks().Where(t=>t.I("id")!=original.I("id")&&t?["pro"].I("parent_id")==0&&t?["pro"].I("project_id")==SelectedProject&&t.S("deleted_at")==""&&t.S("archived_at")=="")){parentIds.Add(item.I("id"));parents.Add($"#{item.I("id")}  {item.S("title")}");}
            parentTask.ItemsSource=parents;parentTask.SelectedIndex=Math.Max(0,parentIds.IndexOf(selectedParent));
        }finally{changingProject=false;}
    }
    void AddChildRow(JsonNode? task)
    {
        int id=task.I("id");childrenPanel.Children.Add(Ui.Row(Ui.Chip(task.S("status"),Ui.Muted),Ui.Quiet($"#{id}  {task.S("title")}",()=>OpenRelated(id)),Ui.Text(task.S("due")==""?"无截止日期":Ui.Due(task.S("due")),11,Ui.Muted)));
    }
    async Task LoadChildren(){if(original.I("id")==0)return;var response=await board.rpc.Call("pro-impact",J.Obj(("id",original.I("id"))));childrenPanel.Children.Clear();foreach(var child in response.A("children"))AddChildRow(child);}
    async Task OpenRelated(int id){await RequestClosePaneAsync();await board.EditTaskById(id);}
    async Task CreateChild()
    {
        if(readOnly||busy)return;
        if(Collect().ToJsonString()!=initial&&!Ui.Confirm("将先保留当前任务草稿，再打开新子任务。是否继续？"))return;
        int id=original.I("id"),pid=SelectedProject;await RequestClosePaneAsync();
        await board.Edit(J.Obj(("title",""),("status","计划中"),("pro",J.Obj(("parent_id",id),("project_id",pid),("inbox",false)))));
    }
    async Task ConvertCheck(CheckBox check,TextBox text,DockPanel row)
    {
        if(readOnly||busy||!Ui.Confirm("将正式保存当前修改，再把这条检查项转换成具有独立日期、附件和提醒的子任务。是否继续？"))return;
        ValidateProfessional();await SaveDraft(true);var saved=await board.rpc.Call("save",Collect());board.Accept(saved);
        int index=checks.FindIndex(c=>ReferenceEquals(c.label,text));finished=true;FinishClose();var response=await board.rpc.Call("pro-check-to-child",J.Obj(("id",original.I("id")),("index",index)));board.Accept(response);
        int childId=response.I("id");if(childId>0)await board.EditTaskById(childId);
    }
    void AddTimeBlock(string start,string end)
    {
        var parsedStart=DateTimeOffset.TryParse(start,out var st)?st.ToLocalTime():new DateTimeOffset(DateTime.Today.AddHours(9));var parsedEnd=DateTimeOffset.TryParse(end,out var en)?en.ToLocalTime():parsedStart.AddHours(1);
        var date=new DatePicker{SelectedDate=parsedStart.Date,Width=150,Margin=new Thickness(0,4,8,6)};var from=Ui.Input(parsedStart.ToString("HH:mm"));from.Width=72;var to=Ui.Input(parsedEnd.ToString("HH:mm"));to.Width=72;
        var row=Ui.Row(date,from,Ui.Text("—"),to);timeBlocks.Add((date,from,to));row.Children.Add(Ui.Quiet("移除",()=>{timeBlocks.Remove((date,from,to));timeBlocksPanel.Children.Remove(row);return Task.CompletedTask;}));timeBlocksPanel.Children.Add(row);
    }
    UIElement BuildSchedule(JsonObject value)
    {
        var series=value["pro"]?["series"] as JsonObject;
        advancedRepeat=Ui.Check("使用高级重复规则",series is not null);AutomationProperties.SetAutomationId(advancedRepeat,"editorAdvancedRepeat");
        repeatFrequency=Ui.Combo(new[]{"每 N 天","每 N 周，指定星期","每 N 月，指定日期","每 N 月，第几个星期几"},Math.Max(0,Array.IndexOf(new[]{"daily","weekly","monthly","monthly_weekday"},series.S("frequency","daily"))));
        seriesInterval=Ui.Input(series.I("interval",1).ToString());seriesInterval.Width=65;
        repeatBasis=Ui.Combo(new[]{"完成当前任务后重复","按计划独立生成"},series.S("basis")=="scheduled"?1:0);
        repeatScope=Ui.Combo(new[]{"本次及以后","仅修改本次"},0);
        seriesStart=new DatePicker{SelectedDate=ParseDate(series.S("start"))??due.SelectedDate??DateTime.Today,Width=150,Margin=new Thickness(0,4,8,6)};
        seriesEnd=new DatePicker{SelectedDate=ParseDate(series.S("end")),Width=150,Margin=new Thickness(0,4,8,6)};
        seriesCount=Ui.Input(series.S("count"));seriesCount.Width=85;Ui.Hint(seriesCount,"不限次数");
        seriesMonthDay=Ui.Input(series.I("month_day",DateTime.Today.Day).ToString());seriesMonthDay.Width=65;
        monthlyNth=Ui.Combo(new[]{"第 1 个","第 2 个","第 3 个","第 4 个","第 5 个","最后一个"},series.I("nth",1)==-1?5:Math.Clamp(series.I("nth",1)-1,0,4));
        monthlyWeekday=Ui.Combo(new[]{"周一","周二","周三","周四","周五","周六","周日"},Math.Clamp(series.I("weekday"),0,6));
        var weekdayRow=Ui.Row();string[] weekdayLabels={"周一","周二","周三","周四","周五","周六","周日"};var selectedDays=series.A("weekdays").Select(x=>int.TryParse(x?.ToString(),out var day)?day:0).ToHashSet();
        for(int index=0;index<7;index++){var day=Ui.Check(weekdayLabels[index],selectedDays.Contains(index));weekdays.Add(day);weekdayRow.Children.Add(day);}
        repeatMissed=Ui.Combo(new[]{"错过时跳到下次","补发错过实例（有数量上限）"},series.S("missed")=="catchup"?1:0);
        repeatDependency=Ui.Combo(new[]{"依赖固定任务","依赖上一期任务"},series.S("dependency_mode")=="previous"?1:0);
        seriesTimezone=Ui.Input(series.S("timezone","Asia/Shanghai"));seriesTimezone.Width=190;
        seriesPaused=Ui.Check("暂停系列",series.B("paused"));
        seriesOptIn=Ui.Check("我已查看预览，允许按计划生成独立实例",series is not null&&series.S("basis")=="scheduled");
        var rules=Ui.Column(Ui.Row(repeatFrequency,Field("间隔 N",seriesInterval)),Ui.Row(repeatBasis,repeatScope),weekdayRow,Ui.Row(Field("每月日期",seriesMonthDay),monthlyNth,monthlyWeekday),Ui.Row(Field("开始日期",seriesStart),Field("结束日期（可选）",seriesEnd),Field("总次数（可选）",seriesCount)),Ui.Row(repeatMissed,repeatDependency),Ui.Row(Field("系列时区",seriesTimezone),seriesPaused),Ui.Button("预览未来 5 次",PreviewSeries,"previewSeries"),seriesPreview,seriesOptIn,Ui.Text("按计划模式允许上一项未完成时生成新项；仅在程序运行且工作区解锁时调度。周一至周五不包含法定节假日与调休规则。",12,Ui.Muted));
        void Toggle(){rules.IsEnabled=advancedRepeat.IsChecked==true;repeat.IsEnabled=advancedRepeat.IsChecked!=true;basis.IsEnabled=advancedRepeat.IsChecked!=true;}
        advancedRepeat.Checked+=(_,_)=>Toggle();advancedRepeat.Unchecked+=(_,_)=>Toggle();Toggle();
        foreach(var item in value["pro"].A("reminders").Where(r=>r.S("status","pending")=="pending"))AddIndependentReminder(item!.Copy());
        var reminderActions=Ui.Row(Ui.Quiet("＋ 独立提醒时间",()=>{AddIndependentReminder(new JsonObject());return Task.CompletedTask;}));
        if(series is not null&&original.I("id")>0){reminderActions.Children.Add(Ui.Quiet("跳过本次",()=>SeriesAction("skip")));reminderActions.Children.Add(Ui.Quiet(series.B("paused")?"恢复系列":"暂停系列",()=>SeriesAction(series.B("paused")?"resume":"pause")));}
        return Ui.Column(advancedRepeat,rules,Ui.Text("独立提醒（无需设置截止日期）",13),remindersPanel,reminderActions);
    }
    async Task PreviewSeries(){ValidateSeries();var rule=CollectSeries()!;var response=await board.rpc.Call("pro-series-preview",J.Obj(("rule",rule),("from",DateTime.Today.ToString("yyyy-MM-dd")),("limit",5)));seriesPreview.Text=response.A("dates").Count==0?"规则范围内没有后续日期。":string.Join("  ·  ",response.A("dates").Select(d=>d?.ToString()));previewRule=rule.ToJsonString();}
    async Task SeriesAction(string action)
    {
        if(busy||readOnly||!Ui.Confirm(action=="skip"?"跳过本次会将当前实例标记为取消，并推进到下一周期；不会记为完成。继续？":"确定更改整个重复系列的暂停状态？当前未保存修改将保留为草稿。"))return;
        await SaveDraft(true);board.Accept(await board.rpc.Call("pro-series-action",J.Obj(("id",original.I("id")),("action",action))));finished=true;FinishClose();
    }
    void AddIndependentReminder(JsonObject originalReminder)
    {
        var at=DateTimeOffset.TryParse(originalReminder.S("at"),out var parsed)?parsed.ToLocalTime():new DateTimeOffset(DateTime.Today.AddDays(1).AddHours(9));
        var date=new DatePicker{SelectedDate=at.Date,Width=155,Margin=new Thickness(0,4,8,6)};var time=Ui.Input(at.ToString("HH:mm"));time.Width=80;var row=Ui.Row(date,time,Ui.Text("本机当地时间",11,Ui.Muted));
        independentReminders.Add((date,time,originalReminder));row.Children.Add(Ui.Quiet("取消提醒",()=>{independentReminders.Remove((date,time,originalReminder));remindersPanel.Children.Remove(row);return Task.CompletedTask;}));remindersPanel.Children.Add(row);
    }
    UIElement BuildDependencies(JsonObject value,int taskId)
    {
        var depIds=value.A("dependencies").Select(x=>x is JsonObject?x.I("id"):int.Parse(x!.ToString())).ToHashSet();var relatedIds=value["pro"].A("related_ids").Select(x=>int.Parse(x!.ToString())).ToHashSet();
        var search=Ui.Input();Ui.Hint(search,"搜索依赖或相关任务…");var rows=new StackPanel();
        foreach(var item in AllTasks().Where(t=>t.I("id")!=taskId)){
            int id=item.I("id");if(item.S("deleted_at")!=""&&!depIds.Contains(id)&&!relatedIds.Contains(id))continue;
            var waiting=Ui.Check("等待完成",depIds.Contains(id));var relation=Ui.Check("相关",relatedIds.Contains(id));dependencies.Add((waiting,id));related.Add((relation,id));
            var row=Ui.Column(Ui.Quiet($"#{id}  {item.S("title")}  · {item.S("status")}",()=>OpenRelated(id)),Ui.Row(waiting,relation));row.Tag=item.S("title");rows.Children.Add(row);
        }
        search.TextChanged+=(_,_)=>{foreach(FrameworkElement row in rows.Children)row.Visibility=(row.Tag?.ToString()??"").Contains(search.Text,StringComparison.OrdinalIgnoreCase)?Visibility.Visible:Visibility.Collapsed;};
        var impact=new StackPanel();
        var refresh=Ui.Quiet("查看双向关系与阻塞影响",async()=>{var response=await board.rpc.Call("pro-impact",J.Obj(("id",taskId)));impact.Children.Clear();foreach(var (key,label) in new[]{("upstream","我正在等待"),("downstream","正在等待我"),("related","相关任务")}){impact.Children.Add(Ui.Text(label,12,Ui.Muted));foreach(var item in response.A(key)){int id=item.I("id");impact.Children.Add(Ui.Quiet($"#{id}  {item.S("title")}  · {item.S("status")}",()=>OpenRelated(id)));}}});refresh.IsEnabled=taskId>0;
        return Ui.Column(Ui.Text("等待完成是硬依赖；相关任务仅用于导航，不阻止完成。",12,Ui.Muted),search,new ScrollViewer{Content=rows,MaxHeight=280,VerticalScrollBarVisibility=ScrollBarVisibility.Auto},refresh,impact);
    }
    JsonObject CollectProfessional()
    {
        var pro=professionalOriginal.Copy();pro["project_id"]=SelectedProject>0?JsonValue.Create(SelectedProject):null;int state=projectState.SelectedIndex>=0&&projectState.SelectedIndex<stateIds.Count?stateIds[projectState.SelectedIndex]:0;pro["state_id"]=state>0?JsonValue.Create(state):null;
        int parent=parentTask.SelectedIndex>=0&&parentTask.SelectedIndex<parentIds.Count?parentIds[parentTask.SelectedIndex]:0;pro["parent_id"]=parent>0?JsonValue.Create(parent):null;
        pro["inbox"]=inbox.IsChecked==true;pro["plan_date"]=planDate.SelectedDate?.ToString("yyyy-MM-dd")??"";pro["focus"]=focus.IsChecked==true;pro["estimate_minutes"]=int.TryParse(estimate.Text,out var minutes)?minutes:0;
        pro["related_ids"]=new JsonArray(related.Where(r=>r.check.IsChecked==true).Select(r=>(JsonNode?)JsonValue.Create(r.id)).ToArray());
        pro["time_blocks"]=new JsonArray(timeBlocks.Select(b=>(JsonNode?)J.Obj(("start",LocalTimestamp(b.date.SelectedDate,b.start.Text)),("end",LocalTimestamp(b.date.SelectedDate,b.end.Text)))).ToArray());
        var reminders=professionalOriginal.A("reminders").Where(r=>r.S("status","pending")!="pending").Select(r=>r!.DeepClone()).ToList();
        foreach(var item in independentReminders){var reminder=item.original.Copy();reminder["at"]=LocalTimestamp(item.date.SelectedDate,item.time.Text);reminder["status"]="pending";reminders.Add(reminder);}
        pro["reminders"]=new JsonArray(reminders.ToArray());pro["series"]=advancedRepeat.IsChecked==true?CollectSeries():null;pro["series_scope"]=repeatScope.SelectedIndex==1?"current":"future";pro["series_opt_in"]=seriesOptIn.IsChecked==true;return pro;
    }
    JsonObject? CollectSeries()
    {
        if(advancedRepeat.IsChecked!=true)return null;var series=(professionalOriginal["series"] as JsonObject)?.Copy()??new JsonObject();
        series["frequency"]=new[]{"daily","weekly","monthly","monthly_weekday"}[Math.Max(0,repeatFrequency.SelectedIndex)];series["interval"]=int.TryParse(seriesInterval.Text,out var interval)?interval:1;
        series["weekdays"]=new JsonArray(weekdays.Select((c,i)=>(c,i)).Where(x=>x.c.IsChecked==true).Select(x=>(JsonNode?)JsonValue.Create(x.i)).ToArray());series["month_day"]=int.TryParse(seriesMonthDay.Text,out var day)?day:1;series["nth"]=monthlyNth.SelectedIndex==5?-1:monthlyNth.SelectedIndex+1;series["weekday"]=Math.Max(0,monthlyWeekday.SelectedIndex);
        series["start"]=seriesStart.SelectedDate?.ToString("yyyy-MM-dd")??"";series["end"]=seriesEnd.SelectedDate?.ToString("yyyy-MM-dd")??"";series["count"]=int.TryParse(seriesCount.Text,out var count)?JsonValue.Create(count):null;series["basis"]=repeatBasis.SelectedIndex==1?"scheduled":"completion";series["missed"]=repeatMissed.SelectedIndex==1?"catchup":"skip";series["dependency_mode"]=repeatDependency.SelectedIndex==1?"previous":"fixed";series["timezone"]=seriesTimezone.Text.Trim();series["paused"]=seriesPaused.IsChecked==true;return series;
    }
    static string LocalTimestamp(DateTime? date,string time)
    {
        if(date is null||!TimeSpan.TryParseExact(time.Trim(),@"hh\:mm",CultureInfo.InvariantCulture,out var clock)||clock.TotalHours>=24)return "";
        var local=DateTime.SpecifyKind(date.Value.Date.Add(clock),DateTimeKind.Unspecified);return new DateTimeOffset(local,TimeZoneInfo.Local.GetUtcOffset(local)).ToString("o");
    }
    void ValidateSeries()
    {
        if(!int.TryParse(seriesInterval.Text,out var interval)||interval<1||interval>999)throw new InvalidOperationException("重复间隔请输入 1 至 999 的整数。");
        if(seriesStart.SelectedDate is null)throw new InvalidOperationException("请选择重复规则的开始日期。");
        if(seriesEnd.SelectedDate<seriesStart.SelectedDate)throw new InvalidOperationException("重复结束日期不能早于开始日期。");
        if(seriesCount.Text.Trim()!=""&&(!int.TryParse(seriesCount.Text,out var count)||count<1))throw new InvalidOperationException("重复总次数须为正整数，留空表示不限。");
        if(repeatFrequency.SelectedIndex==1&&!weekdays.Any(c=>c.IsChecked==true))throw new InvalidOperationException("每周重复请至少选择一个星期。");
        if(repeatFrequency.SelectedIndex==2&&(!int.TryParse(seriesMonthDay.Text,out var day)||day<1||day>31))throw new InvalidOperationException("每月日期请输入 1 至 31。");
    }
    void ValidateProfessional()
    {
        if(!int.TryParse(estimate.Text,out var minutes)||minutes<0||minutes>1000000)throw new InvalidOperationException("预计时长请输入有效的非负分钟数。");
        foreach(var block in timeBlocks){if(!DateTimeOffset.TryParse(LocalTimestamp(block.date.SelectedDate,block.start.Text),out var start)||!DateTimeOffset.TryParse(LocalTimestamp(block.date.SelectedDate,block.end.Text),out var end)||end<=start)throw new InvalidOperationException("工作时段需要有效日期及 HH:mm 时间，结束时间须晚于开始时间。");if(TimeZoneInfo.Local.IsInvalidTime(start.DateTime)||TimeZoneInfo.Local.IsInvalidTime(end.DateTime))throw new InvalidOperationException("这个时段位于夏令时跳过的时间，请重新选择。");}
        foreach(var item in independentReminders)if(LocalTimestamp(item.date.SelectedDate,item.time.Text)=="")throw new InvalidOperationException("独立提醒需要有效日期及 HH:mm 时间。");
        if(advancedRepeat.IsChecked==true){ValidateSeries();if(repeatBasis.SelectedIndex==1&&professionalOriginal["series"] is null&&(seriesOptIn.IsChecked!=true||previewRule!=CollectSeries()!.ToJsonString()))throw new InvalidOperationException("启用按计划独立生成前，请先预览当前规则并勾选确认。");}
    }
}
