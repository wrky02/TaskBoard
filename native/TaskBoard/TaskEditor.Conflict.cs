using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;

namespace TaskBoard;
internal sealed partial class TaskEditor
{
    async Task<JsonObject?> ResolveSaveConflict()
    {
        var draft=Collect();var current=await board.rpc.Call("task-get",J.Obj(("id",original.I("id"))));var task=current["task"] as JsonObject??throw new InvalidOperationException("任务已无法读取；当前内容仍保留在草稿中。");
        var latest=BoardWindow.Payload(task);var choices=new List<(string field,ComboBox choice)>();var fields=new StackPanel();Window? dialog=null;JsonObject? result=null;
        string[] editable={"title","note","status","due","options","pro","tags","dependencies","checklist","milestones"};
        foreach(string field in editable){
            if(JsonNode.DeepEquals(draft[field],latest[field]))continue;
            bool onlyNewerChanged=JsonNode.DeepEquals(draft[field],original[field]);bool onlyDraftChanged=JsonNode.DeepEquals(latest[field],original[field]);
            var choice=Ui.Combo(new[]{"采用我的草稿","采用最新正式版本"},onlyNewerChanged?1:0);choices.Add((field,choice));
            var columns=new Grid();columns.ColumnDefinitions.Add(new ColumnDefinition());columns.ColumnDefinitions.Add(new ColumnDefinition());var left=ComparisonText("我的草稿",DisplayValue(draft[field]));var right=ComparisonText("最新正式版本",DisplayValue(latest[field]));Grid.SetColumn(right,1);columns.Children.Add(left);columns.Children.Add(right);
            fields.Children.Add(Ui.Card(Ui.Column(Ui.Text(FieldName(field)+(onlyNewerChanged?" · 正式版本更新":onlyDraftChanged?" · 仅草稿修改":" · 双方都修改过"),14),choice,columns)));
        }
        var explanation=Ui.Text("这个任务在编辑期间已被修改。选择每组字段要保留的内容；附件以最新正式版本为基础，再加入草稿新增附件。草稿中的移除动作只作用于仍存在的原附件。",12,Ui.Muted);
        async Task Merge(){
            var merged=latest.Copy();foreach(var(field,choice)in choices)merged[field]=(choice.SelectedIndex==0?draft[field]:latest[field])?.DeepClone();
            merged["new_files"]=draft["new_files"]?.DeepClone();var active=task.A("files").Select(f=>f.I("id")).ToHashSet();
            merged["removed_files"]=new JsonArray(draft.A("removed_files").Where(id=>int.TryParse(id?.ToString(),out int value)&&active.Contains(value)).Select(n=>n?.DeepClone()).ToArray());
            merged["file_versions"]=new JsonArray(draft.A("file_versions").Where(f=>active.Contains(f.I("id"))).Select(f=>f?.DeepClone()).ToArray());merged["draft_key"]=draftKey;
            result=await board.rpc.Call("save",merged);dialog!.Close();
        }
        async Task Copy(){
            if(!Ui.Confirm("将草稿另存为新任务，复制当前正式附件和草稿新增附件。重复规则、独立提醒、父子关系与依赖不会自动复制。原任务保持最新内容。继续？"))return;
            var copy=draft.Copy();foreach(string key in new[]{"id","updated_at","files","file_versions","removed_files"})copy.Remove(key);copy["copy_files_from"]=original.I("id");copy["title"]=draft.S("title")+"（草稿副本）";copy["dependencies"]=new JsonArray();
            if(copy["options"] is JsonObject options){options["repeat"]="off";options["reminder"]="off";}
            if(copy["pro"] is JsonObject pro){pro["parent_id"]=null;pro["series"]=null;pro["reminders"]=new JsonArray();pro["focus"]=false;pro.Remove("uid");pro.Remove("revision");}
            foreach(var attachment in copy.A("new_files").OfType<JsonObject>())attachment.Remove("replaces_id");
            result=await board.rpc.Call("save",copy);dialog!.Close();
        }
        var actions=Ui.Row(Ui.Button("按选择合并保存",Merge,"conflictMerge",true),Ui.Button("保留双方，另存新任务",Copy,"conflictCopy"),Ui.Button("返回继续编辑",()=>dialog!.Close()));
        var root=new DockPanel{Margin=new Thickness(22)};var heading=Ui.Column(Ui.Text("任务版本发生冲突",22),explanation);DockPanel.SetDock(heading,Dock.Top);root.Children.Add(heading);DockPanel.SetDock(actions,Dock.Bottom);root.Children.Add(actions);root.Children.Add(Ui.Scroll(fields));
        dialog=Ui.Dialog(DialogOwner,"解决任务版本冲突",new StackPanel(),1000,780);dialog.Content=root;dialog.ShowDialog();return result;
    }
}
