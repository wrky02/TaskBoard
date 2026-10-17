# 任务看板系统 2.0

单人、本地使用的任务看板，基于 Python 本地 HTTP 服务、系统浏览器和 SQLite。
新版已移除 Qt / WebEngine 运行依赖，重新设计登录页、导航、看板和任务抽屉。

## 使用与数据

- Windows 打包后双击 `TaskBoard.exe`。优先用已安装的 Edge 应用窗口启动并最大化，未找到 Edge 则使用默认浏览器。
- 首次设置至少 8 位本机密码；后续输入密码登录。服务只监听 `127.0.0.1`，自动选用空闲端口。
- 安装默认位置为当前用户的 `LocalAppData\Programs\TaskBoard`，可自选目录。数据优先保存在 EXE 同级 `Data`，不可写时询问是否使用用户目录。
- 关闭浏览器窗口后服务继续运行，快捷方式可重新打开。使用侧栏“退出程序”关闭服务；重复启动复用同一数据目录的服务，可能打开另一个浏览器窗口。
- 显式设置 `TASKBOARD_DATA_ROOT` 或 `--data-root` 时优先使用指定目录，适合隔离开发及数据副本测试。
- 密码用于应用登录，数据库和附件并未加密；同一 Windows 用户下的其他程序仍可能读取本地文件。不要把数据目录或 `.instance.json` 分享给其他用户。

## 功能

三阶段任务与排序拖拽、搜索和筛选记忆、标签与批量操作、手动检查清单、里程碑确认时间、附件与截图粘贴、应用内图片预览、附件版本备注、日历、到期提醒、归档、回收站、操作记录、CSV/XLSX 导出、完整备份恢复及只读附件检查。

附件、检查清单、里程碑与任务字段均先在抽屉中暂存，点击“保存任务”一并提交；取消不会修改已保存数据。单文件及每次新增附件总大小不超过 100 MB；浏览器恢复支持最大 2 GB 的 ZIP。表格导出包含活动和归档任务，不带附件本体。批量操作显示成功与失败数量。任务模板已退休，旧库中的历史检查项仍保留。

## 安全升级与回滚

1. 先关闭旧程序，备份实际安装目录、数据库和附件目录，并记录路径与 SHA256。在数据库副本上试用本版，勿直接覆盖唯一数据。
2. 本版不会猜测其他安装目录的 `Data` 位置。旧程序若使用自选安装目录，请从旧版导出完整备份，再在新版“数据与设置”中恢复，或把完整数据副本用于 `--data-root` 演练。
3. 升级旧库时，先生成 `before-v2-*.zip` 完整备份，再事务性移除退休的表及字段；任务、检查项、附件、标签和密码保持兼容。缺失附件或完整性检查失败会中止升级。
4. 恢复先校验 ZIP、数据库及附件，再生成 `pre-restore-*.zip`。替换失败会尝试恢复原库和附件；自动化已覆盖文件替换失败场景，不代表断电过程已验收。
5. 回滚时关闭新版，保留失败现场，在独立目录中恢复原备份及原安装版本。不要把升级后的唯一数据库交给旧版继续修改。
6. 卸载脚本保留运行时生成的 `Data`。Windows 实际安装、升级和卸载验收尚待 Windows 构建环境执行。

## 开发运行

Python 3.12；无需 Node 构建步骤、Qt 或外部数据库。

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python source/taskboard/launcher.py --data-root /tmp/taskboard-dev
```

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe source\taskboard\launcher.py --data-root "$env:TEMP\taskboard-dev"
```

无图形云机器加 `--no-browser`，自动化测试从隔离数据目录读取启动信息。不在日志或分享截图中暴露 `.instance.json` 的启动凭据。
应用本地工作流不需要云端账号或额外密钥。使用现有独立检出目录；无需建立 Git worktree。

## 测试

在 `source/taskboard`，用虚拟环境 Python 运行：

```sh
python -m unittest -v test_server test_startup_data
python test_storage_v13.py
python test_browser.py
```

浏览器测试优先使用 `/usr/bin/chromium`；也可设置 `TASKBOARD_CHROMIUM` 指向浏览器可执行文件，或先运行 `python -m playwright install chromium`。
浏览器测试在隔离数据目录上运行，不连接真实用户库。截图保存在 `artifacts/`。详见 [验证报告](docs/VALIDATION.md)。

## Windows 安装器

在 Windows x64，安装 Python 3.12 和 Inno Setup 6 后执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
```

脚本安装依赖、运行数据和接口回归、生成 `dist\TaskBoard\TaskBoard.exe`、对冻结程序做登录/保存/退出冒烟检查，再生成 `dist\Setup_TaskBoard_2.0.0.exe` 并打印 SHA256。
`ExecutionPolicy Bypass` 仅用于运行本仓库的本地 PowerShell 脚本，不改变机器永久策略，也不关闭依赖下载的 TLS 或校验。

GitHub Actions 的 `Windows installer` 工作流可手动运行；推送到 `build/windows-installer` 也会构建。工作流包含真实浏览器回归、冻结 EXE 冒烟、隔离安装/卸载与数据保留检查，成功后上传安装器。工作流尚未在 GitHub 执行。

当前已在 Linux 验证源码及 Linux 冻结程序；**尚未生成 Windows Setup.exe**。Windows 实机中文输入法、窗口/任务栏、多显示器及安装升级仍待验收，不宣称闪烁已修复。未设置发布者名称，也未做代码签名，Windows 可能提示未知发布者。
