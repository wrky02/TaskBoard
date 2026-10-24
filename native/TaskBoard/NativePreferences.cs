using System;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Interop;
using System.Runtime.InteropServices;
using Forms=System.Windows.Forms;

namespace TaskBoard;

// UI preferences deliberately contain no credentials or task content. They are
// available before unlocking and are independent of database migrations.
internal sealed class NativePreferences
{
    public string Theme { get; set; }="system";
    public string Density { get; set; }="comfortable";
    public double TextScale { get; set; }=1;
    public string ApplicationName { get; set; }="任务看板系统";
    public string WorkspaceName { get; set; }="我的工作空间";
    public string View { get; set; }="board";
    public string Monitor { get; set; }="";
    public double Width { get; set; }=1360;
    public double Height { get; set; }=900;
    public double RelativeLeft { get; set; }=.5;
    public double RelativeTop { get; set; }=.5;
    public bool Maximized { get; set; }
    public bool HasPlacement { get; set; }

    static string PreferencePath {
        get {
            var args=Environment.GetCommandLineArgs();string root=AppContext.BaseDirectory;
            for(int i=0;i<args.Length-1;i++)if(args[i]=="--data-root")root=Path.GetFullPath(args[i+1]);
            string id=Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(root.ToUpperInvariant())))[..16];
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"TaskBoard","Preferences",id+".json");
        }
    }
    internal static NativePreferences Load()
    {
        try {return JsonSerializer.Deserialize<NativePreferences>(File.ReadAllText(PreferencePath))??new();}
        catch(Exception ex) when(ex is IOException or UnauthorizedAccessException or JsonException){return new();}
    }
    internal void Save()
    {
        try {
            string path=PreferencePath;Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            string temp=path+"."+Guid.NewGuid().ToString("N")+".tmp";
            File.WriteAllText(temp,JsonSerializer.Serialize(this));File.Move(temp,path,true);
        }catch(Exception ex) when(ex is IOException or UnauthorizedAccessException){ /* UI preference failures must not lose task edits. */ }
    }
    internal void Restore(Window window)
    {
        if(!HasPlacement)return;
        var monitor=Forms.Screen.AllScreens.FirstOrDefault(s=>s.DeviceName==Monitor)??Forms.Screen.PrimaryScreen!;
        var area=LogicalArea(monitor);
        window.WindowStartupLocation=WindowStartupLocation.Manual;
        window.Width=Math.Clamp(double.IsFinite(Width)?Width:1360,Math.Min(780,area.Width),area.Width);
        window.Height=Math.Clamp(double.IsFinite(Height)?Height:900,Math.Min(540,area.Height),area.Height);
        window.MinWidth=Math.Min(780,area.Width);window.MinHeight=Math.Min(540,area.Height);
        window.Left=area.Left+Math.Clamp(double.IsFinite(RelativeLeft)?RelativeLeft:.5,0,1)*Math.Max(0,area.Width-window.Width);
        window.Top=area.Top+Math.Clamp(double.IsFinite(RelativeTop)?RelativeTop:.5,0,1)*Math.Max(0,area.Height-window.Height);
        if(Maximized)window.WindowState=WindowState.Maximized;
    }
    internal void Capture(Window window)
    {
        if(!window.IsLoaded||window.WindowState==WindowState.Minimized)return;
        var bounds=window.WindowState==WindowState.Normal?new Rect(window.Left,window.Top,window.ActualWidth,window.ActualHeight):window.RestoreBounds;
        if(bounds.IsEmpty||!double.IsFinite(bounds.Left)||bounds.Width<200)return;
        var screen=Forms.Screen.FromHandle(new WindowInteropHelper(window).Handle);var area=LogicalArea(screen);
        Monitor=screen.DeviceName;Width=bounds.Width;Height=bounds.Height;
        RelativeLeft=Math.Clamp((bounds.Left-area.Left)/Math.Max(1,area.Width-bounds.Width),0,1);
        RelativeTop=Math.Clamp((bounds.Top-area.Top)/Math.Max(1,area.Height-bounds.Height),0,1);
        Maximized=window.WindowState==WindowState.Maximized;HasPlacement=true;Save();
    }
    static Rect LogicalArea(Forms.Screen screen)
    {
        double scale=1;
        try {var p=new NativePoint{X=screen.Bounds.Left+1,Y=screen.Bounds.Top+1};var monitor=MonitorFromPoint(p,2);if(GetDpiForMonitor(monitor,0,out uint x,out _)==0&&x>0)scale=x/96.0;}
        catch(DllNotFoundException){}catch(EntryPointNotFoundException){}
        var a=screen.WorkingArea;return new Rect(a.Left/scale,a.Top/scale,a.Width/scale,a.Height/scale);
    }
    [StructLayout(LayoutKind.Sequential)] struct NativePoint { public int X,Y; }
    [DllImport("user32.dll")] static extern IntPtr MonitorFromPoint(NativePoint point,uint flags);
    [DllImport("shcore.dll")] static extern int GetDpiForMonitor(IntPtr monitor,int type,out uint x,out uint y);
}
