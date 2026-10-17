# 任务看板系统 3.0 · 原生 Windows 桌面版

WPF 原生界面、本地 SQLite 数据和私有数据引擎。**不调用 Edge 或其他浏览器，不使用 WebView2、QtWebEngine、Electron，不启动 HTTP 服务。**

安装包包含 .NET 与数据引擎运行组件，用户无需安装 Python 或 .NET。适用于 Windows 10/11 x64。发布者元数据为 **wrky02**。

## 下载与使用

**[3.0.0 成功构建与下载](https://github.com/wrky02/TaskBoard/actions/runs/39023528151)**。Windows 实际安装、原生控件、无浏览器进程/无 TCP 监听、托盘和卸载保留数据均通过。登录 GitHub，在 Artifacts 下载 `TaskBoard-Native-Windows-3.0.0`，解压运行 `Setup_TaskBoard_3.0.0.exe`。包内附带 SHA256SUMS.txt 与 SIGNING-STATUS.txt。

尚未取得受信任的签名身份，当前包是**未签名**构建。已接入 Azure Artifact Signing 自动签名流程；申请与配置步骤见 [正式代码签名](docs/CODE-SIGNING.md)。wrky02 元数据不等于 Windows 的“已验证的发布者”。

首次设置至少 8 位工作区密码。密码用于限制应用入口，数据库和附件不加密。所有任务和附件留在本机。默认数据目录为安装目录旁的 `Data`；设置页显示实际使用位置。安装目录不可写时使用历史用户目录，可通过 `--data-root` 或 `TASKBOARD_DATA_ROOT` 显式选择数据位置。

## 功能与迁移

优先级、我的今天、检查清单进度、常用视图、附件拖入与截图粘贴、图片预览、草稿恢复、批量编辑、重复任务、依赖、提醒、原生日历、标签管理、归档/回收站、撤销、自动备份、迁移预览、诊断与 CSV/Excel 导出。完整规则见 [3.0 版本说明](docs/releases/3.0.0.md)。

窗口关闭可选择退出或最小化到托盘，并在设置中记住选择。托盘运行且工作区已解锁时可以提醒；完全退出或锁定后不再提醒。Windows 通知策略可能隐藏系统气泡，应用内保留未处理提醒。

升级沿用旧版安装标识和 Data 目录；先关闭旧版本，建议保留完整备份。旧结构升级前自动生成备份；导入 ZIP 或旧 Data 目录先校验并显示数量，再由用户确认替换。缺失附件会阻止迁移。卸载保留数据。需要回滚时，请关闭新版，在独立目录中恢复旧备份及旧程序。

## 架构

- `native/TaskBoard/`：WPF 窗口、原生控件、托盘、图片与文件对话框。
- `source/taskboard/native_worker.py`：由主程序启动的私有子进程，使用 stdin/stdout JSON 通信；无网络监听。
- `domain.py`、`features.py`、`storage.py`：任务事务、计划与依赖、SQLite 迁移/备份等共享规则。
- 保留 2.x 的浏览器实现用于兼容性回归，**原生安装包不包含也不启动旧入口**。

## 开发和验证

Windows 开发需要 .NET SDK（见 global.json）、Python 3.12；生成安装器另需 Inno Setup 6。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-native-build.txt
$env:TASKBOARD_PYTHON = (Resolve-Path .venv\Scripts\python.exe).Path
$env:TASKBOARD_WORKER_SOURCE = (Resolve-Path source\taskboard\native_worker.py).Path
dotnet run --project native/TaskBoard -- --data-root "$env:TEMP\TaskBoard-native-dev"
```

Windows 一次性构建与打包：

```powershell
./scripts/build-native.ps1
```

Linux 云环境支持交叉编译和数据规则测试，无法运行 WPF 窗口：

```sh
dotnet build native/TaskBoard/TaskBoard.csproj
cd source/taskboard
python -m unittest -v test_native_worker test_server test_startup_data test_features
python test_storage_v13.py
```

所有测试用临时数据，不连接用户的真实工作区。Windows CI 实际安装后通过 UI Automation 操作原生界面，验证托盘、重复启动、退出及卸载保留数据，并监控子进程和 TCP 监听。真实输入法、DPI、多显示器仍需实机验收。详见 [验证记录](docs/VALIDATION.md)。

签名使用 GitHub OIDC 临时身份，不在源码或制品中保存证书私钥。构建分支：`build/native-windows`。

2.0 基线保留在 [Releases](https://github.com/wrky02/TaskBoard/releases/tag/v2.0.0)。[2.1 说明](docs/releases/2.1.0.md) 对应历史浏览器版本。
