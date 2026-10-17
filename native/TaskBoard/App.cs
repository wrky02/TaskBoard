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
        app.Resources=(ResourceDictionary)System.Windows.Markup.XamlReader.Parse("""
<ResourceDictionary xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml">
 <Style TargetType="Button">
  <Setter Property="Background" Value="White"/><Setter Property="Foreground" Value="#26314C"/><Setter Property="BorderBrush" Value="#E0E4EF"/><Setter Property="BorderThickness" Value="1"/>
  <Setter Property="Template"><Setter.Value><ControlTemplate TargetType="Button">
   <Border x:Name="chrome" Background="{TemplateBinding Background}" BorderBrush="{TemplateBinding BorderBrush}" BorderThickness="{TemplateBinding BorderThickness}" CornerRadius="7" Padding="{TemplateBinding Padding}">
    <ContentPresenter HorizontalAlignment="{TemplateBinding HorizontalContentAlignment}" VerticalAlignment="Center" RecognizesAccessKey="True"/>
   </Border>
   <ControlTemplate.Triggers><Trigger Property="IsMouseOver" Value="True"><Setter TargetName="chrome" Property="Opacity" Value="0.78"/></Trigger><Trigger Property="IsKeyboardFocused" Value="True"><Setter TargetName="chrome" Property="BorderBrush" Value="#6465E8"/></Trigger><Trigger Property="IsEnabled" Value="False"><Setter TargetName="chrome" Property="Opacity" Value="0.5"/></Trigger></ControlTemplate.Triggers>
  </ControlTemplate></Setter.Value></Setter>
 </Style>
 <Style TargetType="TextBox"><Setter Property="BorderBrush" Value="#DDE1EC"/><Setter Property="Background" Value="White"/><Setter Property="Foreground" Value="#26314C"/></Style>
 <Style TargetType="PasswordBox"><Setter Property="BorderBrush" Value="#DDE1EC"/></Style>
 <Style TargetType="ToolTip"><Setter Property="MaxWidth" Value="400"/></Style>
</ResourceDictionary>
""");
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
