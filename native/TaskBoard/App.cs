using System;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Windows;
using System.Windows.Markup;

namespace TaskBoard;
internal sealed class App : Application
{
    Mutex? mutex; EventWaitHandle? wake; RegisteredWaitHandle? listener;
    [STAThread] public static void Main(string[] args)
    {
        CultureInfo.DefaultThreadCurrentCulture=CultureInfo.GetCultureInfo("zh-CN");
        CultureInfo.DefaultThreadCurrentUICulture=CultureInfo.GetCultureInfo("zh-CN");
        FrameworkElement.LanguageProperty.OverrideMetadata(typeof(FrameworkElement),new FrameworkPropertyMetadata(XmlLanguage.GetLanguage("zh-CN")));
        var app=new App{ShutdownMode=ShutdownMode.OnMainWindowClose};
        app.Resources=new ResourceDictionary{Source=new Uri("pack://application:,,,/Theme.xaml")};
        var data=Environment.GetEnvironmentVariable("TASKBOARD_DATA_ROOT")??Path.Combine(AppContext.BaseDirectory,"Data");
        for(int i=0;i<args.Length-1;i++)if(args[i]=="--data-root")data=args[i+1];
        var key=Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(Path.GetFullPath(data).ToUpperInvariant())))[..24];
        app.mutex=new Mutex(true,"Local\\TaskBoard.Native."+key,out bool created);
        if(!created){try{EventWaitHandle.OpenExisting("Local\\TaskBoard.Wake."+key).Set();}catch(WaitHandleCannotBeOpenedException){}app.mutex.Dispose();return;}
        app.wake=new EventWaitHandle(false,EventResetMode.AutoReset,"Local\\TaskBoard.Wake."+key);
        try {
            var rpc=new Rpc(args);var window=new BoardWindow(rpc);app.MainWindow=window;
            app.listener=ThreadPool.RegisterWaitForSingleObject(app.wake,(_,_)=>app.Dispatcher.BeginInvoke(window.Reopen),null,Timeout.Infinite,false);
            app.Exit+=(_,_)=>{app.listener?.Unregister(null);app.wake.Dispose();app.mutex.ReleaseMutex();app.mutex.Dispose();rpc.Dispose();};
            app.DispatcherUnhandledException+=(_,e)=>{Ui.Error(e.Exception.Message);e.Handled=true;};
            app.Run(window);
        } catch(Exception ex){Ui.Error("无法启动任务看板："+ex.Message);}
    }
}
