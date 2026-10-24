using System;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;

namespace TaskBoard;
internal sealed partial class TaskEditor
{
    UIElement BuildHistory(int taskId)
    {
        if(taskId==0)return Ui.Text("正式保存后开始记录版本。草稿与正式版本独立保留。",12,Ui.Muted);
        var list=new StackPanel();var message=Ui.Text("每次正式保存形成可恢复版本；恢复会建立新版本，不覆盖历史。",12,Ui.Muted);
        return Ui.Column(message,Ui.Button("加载版本历史",async()=>{
            var response=await board.rpc.Call("history-list",J.Obj(("id",taskId)));list.Children.Clear();
            foreach(var version in response.A("versions")){
                int id=version.I("id");var actions=Ui.Row(Ui.Button("查看字段差异",()=>CompareHistory(id),"historyCompare-"+id));
                if(!readOnly)actions.Children.Add(Ui.Button("恢复此版本",()=>RestoreHistory(id,false),"historyRestore-"+id));
                actions.Children.Add(Ui.Button("恢复为新任务",()=>RestoreHistory(id,true),"historyCopy-"+id));
                list.Children.Add(Ui.Card(Ui.Column(Ui.Text(version.S("created_at")+"  ·  "+HistoryReason(version.S("reason")),13),Ui.Text(version.S("title"),15),actions)));
            }
            if(list.Children.Count==0)list.Children.Add(Ui.Text("还没有正式版本。升级前的内容将在首次编辑时保留为基线。",12,Ui.Muted));
        },"historyRefresh"),list);
    }
    static string HistoryReason(string reason)=>reason switch{"save"=>"正式保存","restore"=>"历史恢复","baseline"=>"升级前基线","create"=>"创建任务","action"=>"任务操作",_=>reason==""?"任务版本":reason};
    static string DisplayValue(JsonNode? value)
    {
        if(value is null)return "（未设置）";
        if(value is JsonArray list)return list.Count==0?"（空）":string.Join("\n",list.Select(DisplayValue));
        if(value is JsonObject obj)return string.Join("\n",obj.Select(pair=>FieldName(pair.Key)+"："+DisplayValue(pair.Value)));
        string text=value.ToString();return text switch{"true"=>"是","false"=>"否",""=>"（空）",_=>text};
    }
    static string FieldName(string field)=>field switch{"title"=>"标题","note"=>"说明","status"=>"状态","due"=>"截止日期","checklist"=>"检查清单","label"=>"内容","done"=>"已完成","files"=>"附件","filename"=>"文件名","size"=>"字节数","project_id"=>"项目编号","parent_id"=>"父任务编号","state_id"=>"流程编号","tags"=>"标签","dependencies"=>"依赖任务","milestones"=>"里程碑","name"=>"名称","confirmed"=>"已确认","priority"=>"优先级","today"=>"今日计划","pro"=>"专业设置","options"=>"任务选项","plan_date"=>"计划日期","focus"=>"今日重点","estimate_minutes"=>"预计分钟","time_blocks"=>"工作时段","start"=>"开始","end"=>"结束","reminders"=>"独立提醒","at"=>"时间","series"=>"重复系列","related_ids"=>"相关任务","id"=>"编号","version_label"=>"版本名称","version_note"=>"版本备注",_=>field};
    async Task CompareHistory(int versionId)
    {
        var response=await board.rpc.Call("history-diff",J.Obj(("version_id",versionId)));var panel=Ui.Column(Ui.Text("所选历史版本 → 当前正式版本",18),Ui.Text("这里比较正式保存的内容；当前编辑草稿不包含在右侧。",12,Ui.Muted));
        foreach(var change in response.A("changes")){
            var grid=new Grid();grid.ColumnDefinitions.Add(new ColumnDefinition());grid.ColumnDefinitions.Add(new ColumnDefinition());
            var before=ComparisonText("历史版本",DisplayValue(change?["before"]));var after=ComparisonText("当前版本",DisplayValue(change?["after"]));Grid.SetColumn(after,1);grid.Children.Add(before);grid.Children.Add(after);
            panel.Children.Add(Ui.Card(Ui.Column(Ui.Text(change.S("label",FieldName(change.S("field"))),14),grid)));
        }
        if(response.A("changes").Count==0)panel.Children.Add(Ui.Text("所选版本与当前任务内容一致。",13,Ui.Muted));
        Ui.Dialog(DialogOwner,"任务版本差异",panel,900,720).ShowDialog();
    }
    static StackPanel ComparisonText(string label,string value)
    {
        var text=Ui.Input(value);text.IsReadOnly=true;text.AcceptsReturn=true;text.TextWrapping=TextWrapping.Wrap;text.MinHeight=60;text.MaxHeight=260;text.VerticalScrollBarVisibility=ScrollBarVisibility.Auto;return Ui.Column(Ui.Text(label,11,Ui.Muted),text);
    }
    async Task RestoreHistory(int versionId,bool asNew)
    {
        if(busy)return;
        string prompt=asNew?"将此历史版本恢复为一项新任务。原任务及其历史保持完整；无法保留的依赖或项目关系会由系统检查。当前修改将保留为草稿。继续？":"将历史内容恢复到当前任务，并保存为一个新版本。当前正式内容仍可从历史找回，当前修改保留为草稿。继续？";
        if(!Ui.Confirm(prompt))return;busy=true;
        try{
            await SaveDraft(true);var response=await board.rpc.Call("history-restore",J.Obj(("version_id",versionId),("as_new",asNew)));board.Accept(response);finished=true;FinishClose();int taskId=response.I("id",original.I("id"));if(taskId>0)await board.EditTaskById(taskId);
        }finally{busy=false;}
    }
}
