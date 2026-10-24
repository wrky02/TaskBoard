using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Automation;
using Microsoft.Win32;

namespace TaskBoard;
internal sealed partial class TaskEditor
{
    CancellationTokenSource? uploadCancellation;
    readonly TextBlock transferState=Ui.Text("",12,Ui.Muted);
    Button? cancelUpload;
    UIElement BuildAttachments()
    {
        AutomationProperties.SetAutomationId(fileSearch,"attachmentSearch");
        cancelUpload=Ui.Button("取消导入",()=>uploadCancellation?.Cancel(),"uploadCancel");cancelUpload.Visibility=Visibility.Collapsed;
        var choose=Ui.Button("选择附件",async()=>{var dialog=new OpenFileDialog{Multiselect=true,Title="选择附件"};if(dialog.ShowDialog(DialogOwner)==true)await AddFiles(dialog.FileNames);},"addFiles");
        var paste=Ui.Button("粘贴截图",PasteImage,"pasteImage");choose.IsEnabled=!readOnly;paste.IsEnabled=!readOnly;
        var fileArea=Ui.Column(Ui.Row(choose,paste),Ui.Text("拖入文件或粘贴截图 · 新附件合计不超过 100 MB · 草稿仅保存附件引用",12,Ui.Muted),progress,Ui.Row(transferState,cancelUpload),fileSearch,filesPanel);
        fileArea.AllowDrop=!readOnly;fileArea.Drop+=async(_,e)=>{if(e.Data.GetData(DataFormats.FileDrop) is string[] paths){e.Handled=true;try{await AddFiles(paths);}catch(Exception ex){Ui.Error(ex.Message);}}};RenderFiles();return fileArea;
    }
    Window DialogOwner=>paneMode?board:this;
    static long FileBytes(JsonObject file)
    {
        if(long.TryParse(file.S("size"),out long size))return size;
        string encoded=file.S("data");return encoded.Length==0?0:encoded.Length*3L/4-(encoded.EndsWith("==")?2:encoded.EndsWith("=")?1:0);
    }
    static string FileName(JsonObject file)=>file.S("filename",file.S("name","附件"));
    static bool IsImage(JsonObject file)=>file.B("is_image")||new[]{".png",".jpg",".jpeg",".gif",".bmp",".webp"}.Contains(Path.GetExtension(FileName(file)).ToLowerInvariant());
    JsonObject FileReference(JsonObject file)=>file.S("token")!=""?J.Obj(("token",file.S("token"))):J.Obj(("file_id",file.I("id")));
    static string SizeText(long bytes)=>bytes>=1024*1024?$"{bytes/(1024.0*1024):N1} MB":$"{bytes/1024.0:N1} KB";
    async Task AddFiles(string[] paths,int replacesId=0)
    {
        if(readOnly)return;if(busy)throw new InvalidOperationException("正在处理上一次操作，请稍候。");
        var files=paths.Select(p=>new FileInfo(p)).ToList();long total=files.Sum(f=>f.Exists?f.Length:0);
        if(files.Any(f=>!f.Exists||f.Length==0))throw new IOException("附件必须是存在且非空的文件。");
        if(total+newFiles.Sum(FileBytes)>100L*1024*1024)throw new IOException("本次新增附件合计不能超过 100 MB。");
        busy=true;uploadCancellation=new CancellationTokenSource();var cancel=uploadCancellation.Token;progress.Visibility=Visibility.Visible;cancelUpload!.Visibility=Visibility.Visible;progress.Value=0;
        var staged=new List<JsonObject>();string? activeToken=null;long received=0;bool retained=false;
        try{
            foreach(var file in files){
                cancel.ThrowIfCancellationRequested();var start=await board.rpc.Call("attachment-stage-start",J.Obj(("name",file.Name),("size",file.Length)));activeToken=start.S("token");if(activeToken=="")throw new IOException("暂存附件失败：未收到文件标识。");
                await using var stream=new FileStream(file.FullName,FileMode.Open,FileAccess.Read,FileShare.Read,262144,true);var buffer=new byte[262144];long offset=0;
                while(offset<file.Length){
                    int count=(int)Math.Min(buffer.Length,file.Length-offset);await stream.ReadExactlyAsync(buffer.AsMemory(0,count),cancel);
                    cancel.ThrowIfCancellationRequested();var chunk=await board.rpc.Call("attachment-stage-chunk",J.Obj(("token",activeToken),("offset",offset),("data",Convert.ToBase64String(buffer,0,count))));offset+=count;received+=count;
                    if(long.TryParse(chunk.S("received"),out var acknowledged)&&acknowledged!=offset)throw new IOException("附件接收字节数不一致，请重新导入。");
                    progress.Value=total==0?0:received*100.0/total;transferState.Text=$"{file.Name} · {SizeText(received)} / {SizeText(total)}";
                }
                cancel.ThrowIfCancellationRequested();if(offset!=file.Length)throw new IOException("导入时源文件发生变化，请重新选择。");
                var finishedStage=await board.rpc.Call("attachment-stage-finish",J.Obj(("token",activeToken)));var attachment=(finishedStage["staged"] as JsonObject)?.Copy()??throw new IOException("附件暂存结果不完整。");
                if(replacesId>0)attachment["replaces_id"]=replacesId;staged.Add(attachment);activeToken=null;
            }
            cancel.ThrowIfCancellationRequested();newFiles.AddRange(staged);retained=true;RenderFiles();await SaveDraft(true);transferState.Text=$"已暂存 {staged.Count} 个附件 · 正式保存任务后生效";
        }catch(OperationCanceledException){await AbortStages(staged,activeToken);transferState.Text="导入已取消，源文件未改变。";}
        catch{if(!retained)await AbortStages(staged,activeToken);throw;}
        finally{busy=false;progress.Visibility=Visibility.Collapsed;cancelUpload.Visibility=Visibility.Collapsed;uploadCancellation.Dispose();uploadCancellation=null;}
    }
    async Task AbortStages(IEnumerable<JsonObject> files,string? active)
    {
        foreach(var token in files.Select(f=>f.S("token")).Concat(active is null?Array.Empty<string>():new[]{active}).Where(t=>t!="")){try{await board.rpc.Call("attachment-stage-abort",J.Obj(("token",token)));}catch{/* Abandoned staging is collected by the engine; never delete the user's source. */}}
    }
    async Task PasteImage()
    {
        if(readOnly||!Clipboard.ContainsImage())return;if(busy)throw new InvalidOperationException("正在处理上一次操作，请稍候。");
        var bitmap=Clipboard.GetImage();if(bitmap is null)return;var encoder=new PngBitmapEncoder();encoder.Frames.Add(BitmapFrame.Create(bitmap));using var stream=new MemoryStream();encoder.Save(stream);
        if(stream.Length+newFiles.Sum(FileBytes)>100L*1024*1024)throw new IOException("本次新增附件不能超过 100 MB。");
        busy=true;uploadCancellation=new CancellationTokenSource();progress.Visibility=Visibility.Visible;cancelUpload!.Visibility=Visibility.Visible;progress.Value=0;string? token=null;
        try{
            var start=await board.rpc.Call("attachment-stage-start",J.Obj(("name","截图-"+DateTime.Now.ToString("yyyyMMdd-HHmmss")+".png"),("size",stream.Length)));token=start.S("token");stream.Position=0;var buffer=new byte[262144];long offset=0;int count;
            while((count=await stream.ReadAsync(buffer.AsMemory(0,buffer.Length),uploadCancellation.Token))>0){uploadCancellation.Token.ThrowIfCancellationRequested();await board.rpc.Call("attachment-stage-chunk",J.Obj(("token",token),("offset",offset),("data",Convert.ToBase64String(buffer,0,count))));offset+=count;progress.Value=offset*100.0/stream.Length;transferState.Text=$"截图 · {SizeText(offset)} / {SizeText(stream.Length)}";}
            uploadCancellation.Token.ThrowIfCancellationRequested();var response=await board.rpc.Call("attachment-stage-finish",J.Obj(("token",token)));var file=(response["staged"] as JsonObject)?.Copy()??throw new IOException("截图暂存结果不完整。");newFiles.Add(file);token=null;RenderFiles();await SaveDraft(true);transferState.Text="截图已暂存，可在正式保存前预览。";
        }catch(OperationCanceledException){if(token is not null)await AbortStages(Array.Empty<JsonObject>(),token);transferState.Text="截图导入已取消。";}
        catch{if(token is not null)await AbortStages(Array.Empty<JsonObject>(),token);throw;}
        finally{busy=false;progress.Visibility=Visibility.Collapsed;cancelUpload.Visibility=Visibility.Collapsed;uploadCancellation.Dispose();uploadCancellation=null;}
    }
    void RenderFiles()
    {
        foreach(var file in existingFiles)if(versions.TryGetValue(file.I("id"),out var fields)){file["version_label"]=fields.label.Text;file["version_note"]=fields.note.Text;}
        filesPanel.Children.Clear();versions.Clear();
        foreach(var file in existingFiles.Where(f=>!removed.Contains(f.I("id")))){
            int id=file.I("id");var label=Ui.Input(file.S("version_label","初稿"));label.Width=110;var note=Ui.Input(file.S("version_note"));note.Width=220;Ui.Hint(note,"版本备注");label.IsReadOnly=readOnly;note.IsReadOnly=readOnly;versions[id]=(label,note);
            var actions=Ui.Row(Ui.Button("另存为",()=>ExportFile(file,false)),Ui.Button("系统打开",()=>ExportFile(file,true)));
            if(IsImage(file))actions.Children.Add(Ui.Button("预览图片",()=>Gallery(file)));
            actions.Children.Add(Ui.Button("版本历史",()=>AttachmentVersions(file),"attachmentVersions-"+id));
            if(!readOnly){actions.Children.Add(Ui.Button("新增版本",async()=>{var picker=new OpenFileDialog{Title="选择附件的新版本"};if(picker.ShowDialog(DialogOwner)==true)await AddFiles(new[]{picker.FileName},id);}));actions.Children.Add(Ui.Quiet("移除",()=>{removed.Add(id);RenderFiles();return Task.CompletedTask;}));}
            var row=Ui.Column(Ui.Text($"{FileName(file)}   {SizeText(FileBytes(file))}"),actions,Ui.Row(label,note));row.Visibility=FileName(file).Contains(fileSearch.Text,StringComparison.OrdinalIgnoreCase)?Visibility.Visible:Visibility.Collapsed;filesPanel.Children.Add(Ui.Card(row));
        }
        foreach(var file in newFiles.ToList()){
            var actions=Ui.Row();if(IsImage(file))actions.Children.Add(Ui.Button("预览图片",()=>Gallery(file)));actions.Children.Add(Ui.Button("另存为",()=>ExportFile(file,false)));
            if(!readOnly)actions.Children.Add(Ui.Quiet("移除",async()=>{newFiles.Remove(file);RenderFiles();await SaveDraft(true);}));
            var row=Ui.Column(Ui.Text($"待保存 · {FileName(file)} · {SizeText(FileBytes(file))}"),file.I("replaces_id")>0?Ui.Text("将作为现有附件的新版本，旧内容仍可恢复。",11,Ui.Purple):Ui.Text("附件已暂存，尚未正式写入任务。",11,Ui.Muted),actions);row.Visibility=FileName(file).Contains(fileSearch.Text,StringComparison.OrdinalIgnoreCase)?Visibility.Visible:Visibility.Collapsed;filesPanel.Children.Add(Ui.Card(row));
        }
        if(filesPanel.Children.Count==0)filesPanel.Children.Add(Ui.Text("还没有附件。可以拖入文件，或粘贴截图。",12,Ui.Muted));
    }
    async Task ExportFile(JsonObject file,bool open)
    {
        if(open&&!Ui.Confirm("将把附件保存到你选择的位置，再使用系统默认程序打开。这个外部副本不会随任务更新、加密或删除；请自行保管和清理。继续？"))return;
        var picker=new SaveFileDialog{FileName=Path.GetFileName(FileName(file)),Title=open?"选择外部副本的位置并打开":"另存附件"};if(picker.ShowDialog(DialogOwner)!=true)return;
        var request=FileReference(file);request["path"]=picker.FileName;var response=await board.rpc.Call("attachment-export",request);
        if(open)Process.Start(new ProcessStartInfo(response.S("path",picker.FileName)){UseShellExecute=true});
    }
    async Task<BitmapImage> LoadBitmap(JsonObject file)
    {
        byte[] bytes;
        if(file.S("data")!="")bytes=Convert.FromBase64String(file.S("data"));else{var response=await board.rpc.Call("attachment-read",FileReference(file));bytes=Convert.FromBase64String(response.S("data"));}
        return await Task.Run(()=>{using var stream=new MemoryStream(bytes,false);var bitmap=new BitmapImage();bitmap.BeginInit();bitmap.CacheOption=BitmapCacheOption.OnLoad;bitmap.StreamSource=stream;bitmap.EndInit();bitmap.Freeze();return bitmap;});
    }
    async Task Gallery(JsonObject file)
    {
        var images=existingFiles.Where(f=>!removed.Contains(f.I("id"))).Concat(newFiles).Where(IsImage).ToList();int index=images.FindIndex(f=>ReferenceEquals(f,file));if(index<0)throw new InvalidOperationException("此附件不是支持的图片格式。");
        var image=new Image{Stretch=Stretch.None};var transform=new ScaleTransform(1,1);image.LayoutTransform=transform;
        var viewport=new ScrollViewer{Content=image,HorizontalScrollBarVisibility=ScrollBarVisibility.Auto,VerticalScrollBarVisibility=ScrollBarVisibility.Auto,Background=Ui.Color("#E8EAF2")};
        var caption=Ui.Text("正在读取图片…",13);var zoomLabel=Ui.Text("",12,Ui.Muted);bool fit=true,fetching=false;double zoom=1;int generation=0;
        void SetZoom(double value){fit=false;zoom=Math.Clamp(value,.05,8);transform.ScaleX=transform.ScaleY=zoom;zoomLabel.Text=$"{zoom:P0}";}
        void Fit(){fit=true;if(image.Source is BitmapSource bitmap){zoom=Math.Min(1,Math.Min(Math.Max(1,viewport.ViewportWidth-24)/bitmap.PixelWidth,Math.Max(1,viewport.ViewportHeight-24)/bitmap.PixelHeight));transform.ScaleX=transform.ScaleY=zoom;zoomLabel.Text=$"适屏 · {zoom:P0}";}}
        async Task Show(){int current=++generation;fetching=true;caption.Text="正在读取图片…";try{var bitmap=await LoadBitmap(images[index]);if(current!=generation)return;image.Source=bitmap;caption.Text=$"{index+1} / {images.Count}   {FileName(images[index])}   {bitmap.PixelWidth} × {bitmap.PixelHeight}";if(fit)Fit();}finally{fetching=false;}}
        async Task Move(int delta){if(fetching)return;index=(index+delta+images.Count)%images.Count;await Show();}
        var toolbar=Ui.Row(Ui.Button("← 上一张",()=>Move(-1)),Ui.Button("下一张 →",()=>Move(1)),Ui.Button("−",()=>SetZoom(zoom/1.25)),Ui.Button("＋",()=>SetZoom(zoom*1.25)),Ui.Button("100%",()=>SetZoom(1)),Ui.Button("适屏",Fit),zoomLabel);
        var root=new DockPanel{Margin=new Thickness(18)};DockPanel.SetDock(caption,Dock.Top);root.Children.Add(caption);DockPanel.SetDock(toolbar,Dock.Top);root.Children.Add(toolbar);root.Children.Add(viewport);
        var dialog=Ui.Dialog(DialogOwner,"图片预览",new StackPanel(),950,760);dialog.Content=root;
        Point dragStart=default;double horizontal=0,vertical=0;bool dragging=false;
        viewport.PreviewMouseLeftButtonDown+=(_,e)=>{if(e.OriginalSource is Image){dragging=true;dragStart=e.GetPosition(viewport);horizontal=viewport.HorizontalOffset;vertical=viewport.VerticalOffset;viewport.CaptureMouse();viewport.Cursor=Cursors.SizeAll;e.Handled=true;}};
        viewport.PreviewMouseMove+=(_,e)=>{if(dragging){var point=e.GetPosition(viewport);viewport.ScrollToHorizontalOffset(horizontal+dragStart.X-point.X);viewport.ScrollToVerticalOffset(vertical+dragStart.Y-point.Y);}};
        viewport.PreviewMouseLeftButtonUp+=(_,_)=>{dragging=false;viewport.ReleaseMouseCapture();viewport.Cursor=Cursors.Arrow;};
        viewport.PreviewMouseWheel+=(_,e)=>{if(Keyboard.Modifiers.HasFlag(ModifierKeys.Control)){SetZoom(zoom*(e.Delta>0?1.15:1/1.15));e.Handled=true;}};
        viewport.SizeChanged+=(_,_)=>{if(fit)Fit();};dialog.KeyDown+=async(_,e)=>{try{if(e.Key==Key.Left){e.Handled=true;await Move(-1);}else if(e.Key==Key.Right){e.Handled=true;await Move(1);}else if(e.Key==Key.Escape)dialog.Close();}catch(Exception ex){Ui.Error(ex.Message);}};
        dialog.Loaded+=async(_,_)=>{try{await Show();}catch(Exception ex){Ui.Error(ex.Message);dialog.Close();}};dialog.ShowDialog();await Task.CompletedTask;
    }
    async Task AttachmentVersions(JsonObject file)
    {
        var response=await board.rpc.Call("attachment-versions",J.Obj(("id",file.I("id"))));var panel=new StackPanel();Window? dialog=null;
        foreach(var version in response.A("versions")){
            int id=version.I("id");var row=Ui.Column(Ui.Text(version.S("version_label","版本")+(version.B("is_current")?" · 当前":""),15),Ui.Text(version.S("created_at")+"  "+version.S("filename")+"  "+SizeText(version is JsonObject obj?FileBytes(obj):0),12,Ui.Muted),Ui.Text(version.S("version_note"),12),Ui.Text("SHA-256  "+version.S("sha256"),10,Ui.Muted));
            if(!version.B("is_current")&&!readOnly)row.Children.Add(Ui.Button("恢复为新版本",async()=>{if(!Ui.Confirm("将旧附件内容恢复为新版本，现有内容仍保留在历史中。任务的其他未保存修改将保留为草稿。继续？"))return;await SaveDraft(true);board.Accept(await board.rpc.Call("attachment-version-restore",J.Obj(("id",id))));dialog!.Close();finished=true;FinishClose();}));panel.Children.Add(Ui.Card(row));
        }
        if(panel.Children.Count==0)panel.Children.Add(Ui.Text("此附件暂无版本历史。"));dialog=Ui.Dialog(DialogOwner,"附件版本历史",panel,680,680);dialog.ShowDialog();
    }
}
