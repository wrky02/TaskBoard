using System;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Automation;
using System.Collections.Generic;
using System.Linq;
using System.Windows.Input;
using Microsoft.Win32;

namespace TaskBoard;
internal static class Ui
{
    public static FontFamily Font=new(new Uri(AppContext.BaseDirectory), "./Fonts/#Noto Sans SC");
    public static string ApplicationName="任务看板系统";
    public static bool Dark,Compact;
    public static double TextScale=1;
    public static Brush Ink=>ThemeBrush("#222A3F","fg");
    public static Brush Muted=>ThemeBrush("#626D82","fg");
    public static Brush Purple=>ThemeBrush("#7063ED","accent");
    public static Brush Surface=>ThemeBrush("#F5F6FB","bg");
    public static Brush Panel=>ThemeBrush("#FFFFFF","bg");
    public static TextBlock Text(string text, double size=14, Brush? color=null)=>new() { Text=text, FontFamily=Font, FontSize=size*TextScale, Foreground=color??Ink, TextWrapping=TextWrapping.Wrap, Margin=new Thickness(0,4,0,4) };
    public static TextBox Input(string value="", string id="") {
        var box=new TextBox {Text=value,MinHeight=Compact?32:38,FontSize=13*TextScale,Padding=new Thickness(9,6,9,6),Margin=new Thickness(0,4,8,6),VerticalContentAlignment=VerticalAlignment.Center};
        if(id!="") AutomationProperties.SetAutomationId(box,id); return box;
    }
    public static Button Button(string title, Func<Task> action, string id="", bool primary=false) {
        var b=new Button {Content=title,FontSize=13*TextScale,Padding=new Thickness(12,Compact?5:7,12,Compact?5:7),Margin=new Thickness(0,4,8,4),MinHeight=Compact?32:38,Cursor=System.Windows.Input.Cursors.Hand};
        if(primary){b.Background=Purple;b.Foreground=Brushes.White;b.BorderBrush=Purple;}
        if(id!="") AutomationProperties.SetAutomationId(b,id);
        AutomationProperties.SetName(b,title);
        b.Click+=async (_,_)=> {b.IsEnabled=false;try {await action();} catch(Exception ex) {Error(ex.Message);} finally {b.IsEnabled=true;} }; return b;
    }
    public static Button Button(string title, Action action, string id="",bool primary=false)=>Button(title,()=>{action();return Task.CompletedTask;},id,primary);
    public static Brush Color(string hex)=>ThemeBrush(hex,BackgroundColors.Contains(hex.ToUpperInvariant())?"bg":"fg");
    static readonly HashSet<string> BackgroundColors=new(StringComparer.OrdinalIgnoreCase){"#F0EEFF","#F2F3F7","#FBECEF","#FCF3E4","#ECF5F2","#EAF4EF","#EDEAFF","#E6E8F1","#EEF0F7","#FBF3E6","#EBF5EF","#FCEDF0","#1C2538","#EEF0F5"};
    static Brush ThemeBrush(string hex,string role)
    {
        if(SystemParameters.HighContrast)return role=="bg"?SystemColors.WindowBrush:role=="accent"?SystemColors.HighlightBrush:SystemColors.WindowTextBrush;
        var c=(System.Windows.Media.Color)ColorConverter.ConvertFromString(hex);
        if(Dark&&role!="accent") {
            if(role=="bg")c=(System.Windows.Media.Color)ColorConverter.ConvertFromString(hex.Equals("#1C2538",StringComparison.OrdinalIgnoreCase)?"#101827":hex.Equals("#F5F6FB",StringComparison.OrdinalIgnoreCase)?"#151D2B":"#222D40");
            else if(role=="bd")c=(System.Windows.Media.Color)ColorConverter.ConvertFromString("#4E5B73");
            else if(Math.Max(c.R,Math.Max(c.G,c.B))-Math.Min(c.R,Math.Min(c.G,c.B))>45)c=System.Windows.Media.Color.FromRgb((byte)Math.Max((int)c.R,150),(byte)Math.Max((int)c.G,150),(byte)Math.Max((int)c.B,180));
            else c=(System.Windows.Media.Color)ColorConverter.ConvertFromString("#DCE3F0");
        }
        return new SolidColorBrush(c);
    }
    public static void ApplyPreferences(NativePreferences preferences)
    {
        bool systemDark=false;
        try {systemDark=Registry.GetValue(@"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize","AppsUseLightTheme",1) is int value&&value==0;}catch(System.Security.SecurityException){}
        Dark=preferences.Theme=="dark"||(preferences.Theme=="system"&&systemDark);Compact=preferences.Density=="compact";TextScale=Math.Clamp(preferences.TextScale,1,1.5);
        var resources=Application.Current.Resources;
        foreach(string key in resources.Keys.Cast<object>().OfType<string>().Where(k=>k.StartsWith("Palette_")).ToArray()){
            var parts=key.Split('_');resources[key]=ThemeBrush("#"+parts[2],parts[1]);
        }
    }
    // Enter during IME composition confirms the candidate, never a task/tag.
    public static void OnSubmit(TextBox input,Func<Task> submit)
    {
        bool composing=false,busy=false;
        TextCompositionManager.AddPreviewTextInputStartHandler(input,(_,_)=>composing=true);
        TextCompositionManager.AddPreviewTextInputHandler(input,(_,_)=>composing=false);
        input.KeyDown+=async(_,e)=>{
            if(e.Key!=Key.Enter||e.ImeProcessedKey==Key.Enter||composing||busy)return;
            e.Handled=true;busy=true;try{await submit();}catch(Exception ex){Error(ex.Message);}finally{busy=false;}
        };
    }
    public static TextBlock Glyph(string glyph,double size=18,Brush? color=null)=>new(){Text=glyph,FontFamily=new FontFamily("Segoe MDL2 Assets"),FontSize=size,Foreground=color??Muted,VerticalAlignment=VerticalAlignment.Center,Margin=new Thickness(0,0,10,0)};
    public static Border Logo(double size=40)=>new(){Width=size,Height=size,Background=Purple,CornerRadius=new CornerRadius(size/4),Child=new TextBlock{Text="T·",FontFamily=new FontFamily("Segoe UI"),FontSize=size*.65,FontWeight=FontWeights.Bold,Foreground=Brushes.White,HorizontalAlignment=HorizontalAlignment.Center,VerticalAlignment=VerticalAlignment.Center,Margin=new Thickness(1,-2,0,0)}};
    public static Border Chip(string label,Brush ink,Brush? background=null)=>new(){Background=background??Color("#F0EEFF"),CornerRadius=new CornerRadius(5),Padding=new Thickness(7,3,7,3),Margin=new Thickness(0,2,6,2),Child=new TextBlock{Text=label,FontFamily=Font,FontSize=11,Foreground=ink,VerticalAlignment=VerticalAlignment.Center}};
    public static void Hint(TextBox input,string hint){input.Tag=hint;AutomationProperties.SetName(input,hint);}
    public static Button Quiet(string label,Func<Task> action,string id="") {var b=Button(label,action,id);b.Background=Brushes.Transparent;b.BorderThickness=new Thickness(0);return b;}
    public static void Error(string text)=>MessageBox.Show(text,ApplicationName,MessageBoxButton.OK,MessageBoxImage.Warning);
    public static bool Confirm(string text)=>MessageBox.Show(text,ApplicationName,MessageBoxButton.YesNo,MessageBoxImage.Question)==MessageBoxResult.Yes;
    public static StackPanel Column(params UIElement[] children) {var p=new StackPanel();foreach(var x in children)p.Children.Add(x);return p;}
    public static WrapPanel Row(params UIElement[] children) {var p=new WrapPanel();foreach(var x in children)p.Children.Add(x);return p;}
    public static Border Card(UIElement child)=>new(){Child=child,Background=Panel,CornerRadius=new CornerRadius(12),Padding=new Thickness(Compact?11:16),Margin=new Thickness(0,0,0,Compact?8:12),BorderBrush=ThemeBrush("#E5E8F1","bd"),BorderThickness=new Thickness(1)};
    public static ScrollViewer Scroll(UIElement child)=>new(){Style=(Style)Application.Current.FindResource("ThinScrollViewer"),Content=child,VerticalScrollBarVisibility=ScrollBarVisibility.Auto,HorizontalScrollBarVisibility=ScrollBarVisibility.Disabled};
    public static ComboBox Combo(string[] labels,int selected=0,string id="") {
        var c=new ComboBox{ItemsSource=labels,SelectedIndex=selected,MinWidth=118,MinHeight=38,Padding=new Thickness(6),Margin=new Thickness(0,4,8,6)};
        if(id!="")AutomationProperties.SetAutomationId(c,id);return c;
    }
    public static CheckBox Check(string text,bool value=false)=>new(){Content=text,IsChecked=value,Margin=new Thickness(0,6,10,6),VerticalContentAlignment=VerticalAlignment.Center};
    public static Expander Section(string title,UIElement content,bool expanded=true)=>new(){Header=title,Content=content,IsExpanded=expanded,Margin=new Thickness(0,0,0,12),Padding=new Thickness(4)};
    public static Window Dialog(Window owner,string title,UIElement content,double width=650,double height=550)=>new(){Owner=owner,Title=title,Content=Scroll(new Border{Padding=new Thickness(22),Child=content}),Width=Math.Min(width,SystemParameters.WorkArea.Width-24),Height=Math.Min(height,SystemParameters.WorkArea.Height-24),MinWidth=400,MinHeight=300,WindowStartupLocation=WindowStartupLocation.CenterOwner,Background=Surface,FontFamily=Font,FontSize=14,Icon=owner.Icon};
    public static string? Prompt(Window owner,string title,string initial="") {
        string? result=null;var input=Input(initial);Window? w=null;
        var ok=Button("确定",()=>{if(string.IsNullOrWhiteSpace(input.Text))return;result=input.Text.Trim();w!.DialogResult=true;},primary:true);ok.IsDefault=true;
        w=Dialog(owner,title,Column(Text(title,19),input,ok),450,220);w.Loaded+=(_,_)=>input.Focus();w.ShowDialog();return result;
    }
    public static string Due(string date) {
        if(!DateTime.TryParse(date,out var due))return "无截止日期";
        int days=(due.Date-DateTime.Today).Days;return days switch {0=>"今天截止",1=>"明天截止",<0=>$"逾期 {-days} 天",_=>$"还剩 {days} 天"};
    }
}
