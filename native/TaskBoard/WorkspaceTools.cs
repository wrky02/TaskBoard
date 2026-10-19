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
using Microsoft.Win32;

namespace TaskBoard;
internal sealed partial class BoardWindow
{
    JsonObject Filters()=>J.Obj(("query",search.Text),("priority",priority.SelectedIndex-1),("status",stage.SelectedIndex>0?Stages[stage.SelectedIndex-1]:""),("tag",tag.SelectedIndex>0?State.A("tags")[tag.SelectedIndex-1].I("id"):0),("due",new[]{"","today","overdue","week","none"}[Math.Max(0,dueFilter.SelectedIndex)]),("sort",new[]{"manual","priority","due","updated"}[Math.Max(0,sort.SelectedIndex)]));
    async Task SaveView(){var name=Ui.Prompt(this,"视图名称");if(name is null)return;var filters=Filters();State=await rpc.Call("view",J.Obj(("name",name),("filters",filters)));BuildShell();ApplyFilters(filters);}
    void ApplyView(JsonObject view){page="看板";ApplyFilters(view["filters"]!.AsObject());}
    void ApplyFilters(JsonObject f)
    {
        loadingFilters=true;search.Text=f.S("query");priority.SelectedIndex=f.S("priority")==""?0:f.I("priority",-1)+1;stage.SelectedIndex=Array.IndexOf(Stages,f.S("status"))+1;
        tag.SelectedIndex=State.A("tags").Select((t,i)=>(t,i)).Where(x=>x.t.I("id")==f.I("tag")).Select(x=>x.i+1).FirstOrDefault();
        dueFilter.SelectedIndex=Math.Max(0,Array.IndexOf(new[]{"","today","overdue","week","none"},f.S("due")));sort.SelectedIndex=Math.Max(0,Array.IndexOf(new[]{"manual","priority","due","updated"},f.S("sort")));loadingFilters=false;Render();
    }
    Task Tags()
    {
        Window? window=null;var list=new StackPanel();var input=Ui.Input("","tagName");input.Width=320;Ui.Hint(input,"输入新标签名称，按 Enter 添加…");
        void Draw(){list.Children.Clear();foreach(var node in State.A("tags")){
            var t=node!.Copy();var color=Ui.Combo(new[]{"紫色","蓝色","绿色","橙色","红色","灰色"},Array.IndexOf(new[]{"purple","blue","green","orange","red","gray"},t.S("color")));
            color.SelectionChanged+=async(_,_)=>{try{Accept(await rpc.Call("tag",J.Obj(("id",t.I("id")),("action","color"),("color",new[]{"purple","blue","green","orange","red","gray"}[color.SelectedIndex]))));}catch(Exception ex){Ui.Error(ex.Message);}};
            list.Children.Add(Ui.Card(Ui.Column(Ui.Text(t.S("name")+" · "+t.I("count")+" 项任务",16),Ui.Row(color,Ui.Button("重命名",async()=>{var name=Ui.Prompt(window!,"标签名称",t.S("name"));if(name is not null){Accept(await rpc.Call("tag",J.Obj(("id",t.I("id")),("action","rename"),("name",name))));Draw();}}),Ui.Button("合并到…",async()=>{
                var targets=State.A("tags").Where(x=>x.I("id")!=t.I("id")).ToList();if(targets.Count==0)return;var choice=Ui.Combo(targets.Select(x=>x.S("name")).ToArray());Window? merge=null;
                merge=Ui.Dialog(window!,"选择合并目标",Ui.Column(Ui.Text("合并后保留目标标签，任务关联自动更新。"),choice,Ui.Button("确认合并",async()=>{Accept(await rpc.Call("tag",J.Obj(("id",t.I("id")),("action","merge"),("target",targets[choice.SelectedIndex].I("id")))));merge!.Close();Draw();})),450,240);merge.ShowDialog();await Task.CompletedTask;
            }),Ui.Button("删除",async()=>{if(Ui.Confirm("删除标签？任务本身会保留。")){Accept(await rpc.Call("tag",J.Obj(("id",t.I("id")),("action","delete"))));Draw();}})))));
        }}
        async Task Add(){if(string.IsNullOrWhiteSpace(input.Text))return;Accept(await rpc.Call("tag",J.Obj(("action","create"),("name",input.Text))));input.Clear();Draw();}
        input.KeyDown+=async(_,e)=>{if(e.Key==Key.Enter){e.Handled=true;try{await Add();}catch(Exception ex){Ui.Error(ex.Message);}}};
        var hint=Ui.Text("",12,Ui.Muted);input.TextChanged+=(_,_)=>hint.Text=string.IsNullOrWhiteSpace(input.Text)?"":string.Join("、",State.A("tags").Where(t=>t.S("name").Contains(input.Text,StringComparison.OrdinalIgnoreCase)).Select(t=>t.S("name")));
        Draw();window=Ui.Dialog(this,"标签管理",Ui.Column(Ui.Text("标签管理",25),Ui.Row(input,Ui.Button("添加标签",Add,"createTag",true)),hint,list),760,660);window.ShowDialog();var filters=Filters();BuildShell();ApplyFilters(filters);return Task.CompletedTask;
    }
    async Task RefreshDraftNotice(){var data=await rpc.Call("drafts");if(data.A("drafts").Count>0)footer.Text=$"发现 {data.A("drafts").Count} 份未正式保存的草稿，请从侧栏「草稿恢复」查看。";}
    async Task Drafts()
    {
        var data=await rpc.Call("drafts");Window? w=null;var list=new StackPanel();
        foreach(var item in data.A("drafts")){
            var d=item!.Copy();var row=Ui.Card(Ui.Column(Ui.Text(d.S("title","未命名草稿"),17),Ui.Text(d.S("updated"),12,Ui.Muted),Ui.Row(Ui.Button("恢复编辑",async()=>{
                var payload=await rpc.Call("draft-get",J.Obj(("key",d.S("id"))));JsonObject? task=State.A("tasks").FirstOrDefault(t=>t.I("id")==payload.I("id"))?.AsObject();
                if(payload.I("id")>0&&(task is null||task.S("updated_at")!=payload.S("updated_at")))throw new InvalidOperationException("原任务已变化或不在看板中。为防止覆盖，请先检查任务；此草稿保留。可使用「恢复为新任务」。");
                w!.Close();new TaskEditor(this,task,payload,d.S("id")).ShowDialog();
            }),Ui.Button("恢复为新任务",async()=>{var payload=await rpc.Call("draft-get",J.Obj(("key",d.S("id"))));payload.Remove("id");payload.Remove("updated_at");payload["removed_files"]=new JsonArray();payload["file_versions"]=new JsonArray();w!.Close();new TaskEditor(this,null,payload,d.S("id")).ShowDialog();}),Ui.Button("删除草稿",async()=>{if(Ui.Confirm("永久删除此草稿？")){await rpc.Call("draft-save",J.Obj(("key",d.S("id")),("action","delete")));w!.Close();await Drafts();}}))));list.Children.Add(row);
        }
        if(data.A("drafts").Count==0)list.Children.Add(Ui.Text("没有未保存的草稿。"));w=Ui.Dialog(this,"草稿恢复",list);w.ShowDialog();
    }
    Task Activity(){var list=new StackPanel();foreach(var a in State.A("activity"))list.Children.Add(Ui.Card(Ui.Column(Ui.Text(a.S("action")+" · "+a.S("title")),Ui.Text(a.S("at")+"  "+a.S("detail"),12,Ui.Muted))));Ui.Dialog(this,"操作记录",list,800,650).ShowDialog();return Task.CompletedTask;}
    Task Bulk()
    {
        var ids=selected.Where(id=>State.A("tasks").Any(t=>t.I("id")==id)).ToArray();if(ids.Length==0){Ui.Error("先勾选需要修改的任务。");return Task.CompletedTask;}
        CheckBox changeStatus=Ui.Check("修改状态"),changePriority=Ui.Check("修改优先级"),changeDue=Ui.Check("修改截止日期（留空则清除）"),changeTags=Ui.Check("替换标签（未勾选标签则清除）");
        var statusBox=Ui.Combo(Stages);var priorityBox=Ui.Combo(Priorities,2);var dateBox=new DatePicker{Width=180};var tagChecks=State.A("tags").Select(t=>(box:Ui.Check(t.S("name")),id:t.I("id"))).ToList();var tagPanel=Ui.Row();foreach(var t in tagChecks)tagPanel.Children.Add(t.box);Window? w=null;
        w=Ui.Dialog(this,$"批量编辑 {ids.Length} 项任务",Ui.Column(Ui.Text("只修改勾选的字段；失败的项目会逐项报告。"),Ui.Row(changeStatus,statusBox),Ui.Row(changePriority,priorityBox),Ui.Row(changeDue,dateBox),changeTags,tagPanel,Ui.Button("应用修改",async()=>{
            var changes=new JsonObject();if(changeStatus.IsChecked==true)changes["status"]=Stages[statusBox.SelectedIndex];if(changePriority.IsChecked==true)changes["priority"]=priorityBox.SelectedIndex;if(changeDue.IsChecked==true)changes["due"]=dateBox.SelectedDate?.ToString("yyyy-MM-dd");if(changeTags.IsChecked==true)changes["tags"]=new JsonArray(tagChecks.Where(t=>t.box.IsChecked==true).Select(t=>(JsonNode?)JsonValue.Create(t.id)).ToArray());
            if(changes.Count==0)return;var result=await rpc.Call("bulk",J.Obj(("ids",ids),("changes",changes)));Accept(result);var failed=result["result"].A("failed");if(failed.Count>0)Ui.Error(string.Join("\n",failed.Select(f=>"#"+f.I("id")+" "+f.S("error"))));w!.Close();
        },"applyBulk",true)),650,550);w.ShowDialog();return Task.CompletedTask;
    }
    DateTime calendarMonth=new(DateTime.Today.Year,DateTime.Today.Month,1);
    void RenderCalendar(List<JsonObject> tasks)
    {
        var grid=new Grid();for(int i=0;i<7;i++)grid.ColumnDefinitions.Add(new ColumnDefinition());for(int i=0;i<7;i++)grid.RowDefinitions.Add(new RowDefinition{Height=i==0?GridLength.Auto:new GridLength(1,GridUnitType.Star)});
        var weekdays=new[]{"周一","周二","周三","周四","周五","周六","周日"};for(int i=0;i<7;i++){var day=Ui.Text(weekdays[i]);Grid.SetColumn(day,i);grid.Children.Add(day);}
        DateTime first=calendarMonth.AddDays(-((int)calendarMonth.DayOfWeek+6)%7);
        for(int index=0;index<42;index++){
            var date=first.AddDays(index);var panel=Ui.Column(Ui.Text(date.Day.ToString(),16,date.Month==calendarMonth.Month?Ui.Ink:Ui.Muted));
            foreach(var task in tasks.Where(t=>t.S("due_date")==date.ToString("yyyy-MM-dd")))panel.Children.Add(Ui.Button(task.S("title"),()=>Edit(task)));
            var cell=new Border{Child=Ui.Scroll(panel),Padding=new Thickness(4),BorderBrush=Brushes.LightGray,BorderThickness=new Thickness(0,0,1,1),Background=date==DateTime.Today?new SolidColorBrush(Color.FromRgb(235,234,255)):Brushes.White};Grid.SetColumn(cell,index%7);Grid.SetRow(cell,index/7+1);grid.Children.Add(cell);
        }
        var root=new DockPanel();var heading=Ui.Row(Ui.Button("上个月",()=>{calendarMonth=calendarMonth.AddMonths(-1);Render();}),Ui.Text(calendarMonth.ToString("yyyy 年 M 月"),20),Ui.Button("下个月",()=>{calendarMonth=calendarMonth.AddMonths(1);Render();}),Ui.Button("回到今天",()=>{calendarMonth=new(DateTime.Today.Year,DateTime.Today.Month,1);Render();}));DockPanel.SetDock(heading,Dock.Top);root.Children.Add(heading);root.Children.Add(grid);body.Content=root;
    }
    Task Settings()
    {
        var names=State["names"];var system=Ui.Input(names.S("system_name","任务看板系统"));var user=Ui.Input(names.S("user_name","我"));var close=Ui.Combo(new[]{"每次询问","退出程序","最小化到托盘"},Array.IndexOf(new[]{"ask","exit","tray"},State["info"].S("close_behavior","ask")),"closeBehavior");
        var info=State["info"];var panel=Ui.Column(Ui.Text("设置与数据",25),Ui.Text("3.0.1 · 原生 WPF · 发布者 wrky02",14,Ui.Purple),Ui.Text("数据位置："+info.S("data_path")),Ui.Text("最近自动备份："+info.S("last_backup","暂无")+" · 共 "+info.I("backup_count")+" 份"),Ui.Text("每日首次启动备份，保留最近 7 份。备份包含任务、附件与密码设置。",12,Ui.Muted));
        if(info.S("backup_error")!="")panel.Children.Add(Ui.Text("备份失败："+info.S("backup_error"),13,Brushes.IndianRed));
        panel.Children.Add(Ui.Section("个性化与关闭行为",Ui.Column(Ui.Text("系统名称"),system,Ui.Text("工作区名称"),user,Ui.Text("点击窗口 × 时"),close,Ui.Button("保存设置",async()=>{Accept(await rpc.Call("settings",J.Obj(("system_name",system.Text),("user_name",user.Text),("close_behavior",new[]{"ask","exit","tray"}[Math.Max(0,close.SelectedIndex)]))));},"saveSettings",true))));
        panel.Children.Add(Ui.Section("备份、迁移与导出",Ui.Column(Ui.Row(Ui.Button("创建完整备份",()=>Export("zip"),"backup"),Ui.Button("从 ZIP 恢复",()=>Import(false),"restoreZip"),Ui.Button("导入旧版 Data 目录",()=>Import(true),"importData")),Ui.Row(Ui.Button("导出 CSV",()=>Export("csv")),Ui.Button("导出 Excel",()=>Export("xlsx"))),Ui.Text("导入会替换当前数据。先预览并确认，恢复前自动创建安全备份。",12,Ui.Muted))));
        panel.Children.Add(Ui.Section("诊断",Ui.Column(Ui.Row(Ui.Button("导出诊断报告",async()=>{var dialog=new SaveFileDialog{FileName="TaskBoard-diagnostics.json",Filter="JSON|*.json"};if(dialog.ShowDialog(this)==true){await rpc.Call("diagnostics",J.Obj(("path",dialog.FileName)));MessageBox.Show("诊断报告已导出。");}}),Ui.Button("检查附件完整性",async()=>{var report=await rpc.Call("health");MessageBox.Show(report.ToJsonString(new System.Text.Json.JsonSerializerOptions{WriteIndented=true}),"附件检查");})),Ui.Text("诊断默认排除任务标题、正文、附件内容与名称、密码及本地路径。",12,Ui.Muted))));
        var old=new PasswordBox{MinHeight=32,Margin=new Thickness(0,5,0,5)};var next=new PasswordBox{MinHeight=32,Margin=new Thickness(0,5,0,5)};var confirm=new PasswordBox{MinHeight=32,Margin=new Thickness(0,5,0,5)};
        panel.Children.Add(Ui.Section("修改密码",Ui.Column(Ui.Text("旧密码"),old,Ui.Text("新密码（至少 8 位）"),next,Ui.Text("再次输入"),confirm,Ui.Button("修改密码",async()=>{await rpc.Call("password",J.Obj(("old",old.Password),("new",next.Password),("confirm",confirm.Password)));old.Clear();next.Clear();confirm.Clear();MessageBox.Show("密码已更新。");})),false));
        var views=new StackPanel();foreach(var v in State.A("views")){var item=v!.Copy();var row=Ui.Row(Ui.Text(item.S("name")),Ui.Button("删除视图",async()=>{State=await rpc.Call("view",J.Obj(("action","delete"),("id",item.I("id"))));BuildShell();}));views.Children.Add(row);}panel.Children.Add(Ui.Section("管理常用视图",views,false));
        Ui.Dialog(this,"设置与备份",panel,780,760).ShowDialog();return Task.CompletedTask;
    }
    async Task Export(string format)
    {
        var dialog=new SaveFileDialog{FileName="TaskBoard-"+DateTime.Now.ToString("yyyyMMdd-HHmmss")+"."+format,Filter=format.ToUpperInvariant()+"|*."+format};if(dialog.ShowDialog(this)!=true)return;
        await rpc.Call("export",J.Obj(("format",format),("path",dialog.FileName)));MessageBox.Show("导出完成："+dialog.FileName,"任务看板");
    }
    async Task Import(bool directory)
    {
        string path;
        if(directory){var dialog=new OpenFolderDialog{Title="选择包含 tasks.sqlite3 和 attachments 的旧版 Data 目录"};if(dialog.ShowDialog(this)!=true)return;path=dialog.FolderName;}
        else{var dialog=new OpenFileDialog{Filter="完整备份 ZIP|*.zip"};if(dialog.ShowDialog(this)!=true)return;path=dialog.FileName;}
        var info=await rpc.Call("preview",J.Obj(("path",path)));
        if(!Ui.Confirm($"备份时间：{info.S("modified")}\n任务：{info.I("tasks")} 项\n附件：{info.I("attachments")} 个\n\n导入会替换当前数据，并先保留安全备份。确认继续？"))return;
        var result=await rpc.Call("restore");Accept(result);BuildShell();MessageBox.Show("导入完成。恢复前的数据保存在：\n"+result.S("safety"),"任务看板");
    }
}
