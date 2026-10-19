using System;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Automation;

namespace TaskBoard;
internal static class Ui
{
    public static FontFamily Font=new(new Uri(AppContext.BaseDirectory), "./Fonts/#Noto Sans SC");
    public static Brush Ink=new SolidColorBrush(System.Windows.Media.Color.FromRgb(34,42,63)), Muted=new SolidColorBrush(System.Windows.Media.Color.FromRgb(110,120,141)), Purple=new SolidColorBrush(System.Windows.Media.Color.FromRgb(112,99,237)), Surface=new SolidColorBrush(System.Windows.Media.Color.FromRgb(245,246,251));
    public static TextBlock Text(string text, double size=14, Brush? color=null)=>new() { Text=text, FontSize=size, Foreground=color??Ink, TextWrapping=TextWrapping.Wrap, Margin=new Thickness(0,4,0,4) };
    public static TextBox Input(string value="", string id="") {
        var box=new TextBox {Text=value,MinHeight=38,Padding=new Thickness(9,6,9,6),Margin=new Thickness(0,4,8,6),VerticalContentAlignment=VerticalAlignment.Center};
        if(id!="") AutomationProperties.SetAutomationId(box,id); return box;
    }
    public static Button Button(string title, Func<Task> action, string id="", bool primary=false) {
        var b=new Button {Content=title,Padding=new Thickness(12,7,12,7),Margin=new Thickness(0,4,8,4),MinHeight=38,Cursor=System.Windows.Input.Cursors.Hand};
        if(primary){b.Background=Purple;b.Foreground=Brushes.White;b.BorderBrush=Purple;}
        if(id!="") AutomationProperties.SetAutomationId(b,id);
        b.Click+=async (_,_)=> {b.IsEnabled=false;try {await action();} catch(Exception ex) {Error(ex.Message);} finally {b.IsEnabled=true;} }; return b;
    }
    public static Button Button(string title, Action action, string id="",bool primary=false)=>Button(title,()=>{action();return Task.CompletedTask;},id,primary);
    public static Brush Color(string hex)=>(Brush)new BrushConverter().ConvertFromString(hex)!;
    public static TextBlock Glyph(string glyph,double size=18,Brush? color=null)=>new(){Text=glyph,FontFamily=new FontFamily("Segoe MDL2 Assets"),FontSize=size,Foreground=color??Muted,VerticalAlignment=VerticalAlignment.Center,Margin=new Thickness(0,0,10,0)};
    public static Border Logo(double size=40)=>new(){Width=size,Height=size,Background=Purple,CornerRadius=new CornerRadius(size/4),Child=new TextBlock{Text="T·",FontFamily=new FontFamily("Segoe UI"),FontSize=size*.65,FontWeight=FontWeights.Bold,Foreground=Brushes.White,HorizontalAlignment=HorizontalAlignment.Center,VerticalAlignment=VerticalAlignment.Center,Margin=new Thickness(1,-2,0,0)}};
    public static Border Chip(string label,Brush ink,Brush? background=null)=>new(){Background=background??Color("#F0EEFF"),CornerRadius=new CornerRadius(5),Padding=new Thickness(7,3,7,3),Margin=new Thickness(0,2,6,2),Child=new TextBlock{Text=label,FontSize=11,Foreground=ink,VerticalAlignment=VerticalAlignment.Center}};
    public static void Hint(TextBox input,string hint){input.Tag=hint;AutomationProperties.SetName(input,hint);}
    public static Button Quiet(string label,Func<Task> action,string id="") {var b=Button(label,action,id);b.Background=Brushes.Transparent;b.BorderThickness=new Thickness(0);return b;}
    public static void Error(string text)=>MessageBox.Show(text,"任务看板系统",MessageBoxButton.OK,MessageBoxImage.Warning);
    public static bool Confirm(string text)=>MessageBox.Show(text,"任务看板系统",MessageBoxButton.YesNo,MessageBoxImage.Question)==MessageBoxResult.Yes;
    public static StackPanel Column(params UIElement[] children) {var p=new StackPanel();foreach(var x in children)p.Children.Add(x);return p;}
    public static WrapPanel Row(params UIElement[] children) {var p=new WrapPanel();foreach(var x in children)p.Children.Add(x);return p;}
    public static Border Card(UIElement child)=>new(){Child=child,Background=Brushes.White,CornerRadius=new CornerRadius(12),Padding=new Thickness(16),Margin=new Thickness(0,0,0,12),BorderBrush=new SolidColorBrush(System.Windows.Media.Color.FromRgb(229,232,241)),BorderThickness=new Thickness(1)};
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
