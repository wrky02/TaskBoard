# Windows 正式代码签名

当前尚无受信任的签名身份，构建产物是**未签名**安装包。`AppPublisher=wrky02` 和 EXE 公司字段只控制显示元数据，不能使 Windows 显示“已验证的发布者”。签名链中的发布者必须是服务审核通过的真实个人或组织身份，未必能直接使用 GitHub 用户名 wrky02。

## 推荐申请：Microsoft Azure Artifact Signing

Artifact Signing 原名 Trusted Signing。申请前在官方页面确认当前个人/组织身份及所在国家或地区的支持情况、价格和审核要求：

- [服务概述与申请条件](https://learn.microsoft.com/azure/trusted-signing/overview)
- [设置签名账户、身份验证与证书配置文件](https://learn.microsoft.com/azure/trusted-signing/quickstart)
- [官方 GitHub Actions 签名工具](https://github.com/Azure/artifact-signing-action)

需要：Azure 订阅、完成身份验证的签名账户、**Public Trust** 证书配置文件（不能用 Private Trust 或测试身份代替）、对应区域的 endpoint，以及有签名权限的应用身份。私钥由云签名服务保管，无需在 GitHub 保存 PFX。

若该服务不支持申请人的地区/身份，可向 DigiCert、Sectigo、GlobalSign 等受信任 CA 申请代码签名服务。新证书通常要求硬件令牌或云 HSM；普通导出 PFX 不一定可用。USB 令牌需要有令牌驱动的自托管 Windows runner，不能假定 GitHub 托管 runner 可访问本地令牌。此仓库目前直接集成 Azure 方案；其他 CA 必须按其官方工具接入后验证，不能仅填入一个证书文件就宣称已支持。

## 不想自行申请或保管证书

可以考虑把 WPF 程序打成 MSIX，通过 Microsoft Store 发布。商店在发布过程中为 MSIX 签名，开发者不必自行购买代码签名证书。但仍需要开发者账号、身份验证和应用审核。商店签名用于商店分发的包，不能据此把 GitHub 上的独立 EXE 标成“已验证的发布者”。

当前仓库交付的是独立 EXE 安装器，尚未接入 MSIX 或商店发布流程。参考 [Microsoft Store 的 MSIX 提交说明](https://learn.microsoft.com/windows/apps/publish/publish-your-app/msix/overview)。

自签名证书、SHA-256 校验和和 GitHub 制品证明都不能自动建立 Windows 公共信任。想保持 GitHub EXE 的分发方式并显示可信发布者，仍需要经身份审核的可信签名服务。

## GitHub 配置（取得身份后）

仓库 Settings → Secrets and variables → Actions：

| 类型 | 名称 | 内容 |
|---|---|---|
| Secret | `AZURE_SIGNING_CLIENT_ID` | Azure 应用的 client ID |
| Secret | `AZURE_SIGNING_TENANT_ID` | tenant ID |
| Secret | `AZURE_SIGNING_SUBSCRIPTION_ID` | subscription ID |
| Variable | `AZURE_SIGNING_ENDPOINT` | 签名账户所在区域的官方 HTTPS endpoint |
| Variable | `AZURE_SIGNING_ACCOUNT` | 签名账户名称 |
| Variable | `AZURE_SIGNING_PROFILE` | 通过审核的 Public Trust 配置文件名称 |
| Variable | `TASKBOARD_SIGNING_ENABLED` | 配置完成后设为 `true` |

Azure 应用需配置 GitHub OIDC 联合身份：issuer `https://token.actions.githubusercontent.com`、audience `api://AzureADTokenExchange`、subject `repo:wrky02/TaskBoard:ref:refs/heads/build/native-windows`。在签名账户/配置文件范围授予官方要求的证书配置文件签名角色。后续若换发布分支，同步修改联合身份 subject。不要对所有分支和任意 fork 放开签名身份。

不要在聊天、源代码、日志或制品中放私钥、PFX 密码、云访问密钥。当前流程使用 OIDC 临时凭据，不要求 client secret。

## 已接入的构建步骤

`.github/workflows/native-windows.yml`：

1. 构建自包含 WPF 程序和本地数据引擎。
2. 在启用签名时，通过官方 `azure/login` 和 `azure/artifact-signing-action`，对 TaskBoard.exe、TaskBoard.dll、TaskBoard.Engine.exe 做 SHA-256 签名并加 RFC3161 时间戳。
3. 用 `Get-AuthenticodeSignature` 检查可信链及时间戳；验证失败即中止，不降级发布未签名包。
4. 使用已签名程序生成 Inno Setup 安装器。
5. 签名并校验 Setup_TaskBoard_3.0.1.exe，再执行实际安装、原生 UI、卸载保留数据验收。
6. 制品附带 `SHA256SUMS.txt` 与 `SIGNING-STATUS.txt`。没有配置签名时，状态明确写为 UNSIGNED。

当前没有证书，因此**云身份认证、真实签名与证书链校验尚未执行**。工作流已接好，需申请身份、配置以上值，在 `build/native-windows` 推送新提交触发构建；工作流进入默认分支后也可通过 workflow_dispatch 手动运行。代码签名不保证立即建立 SmartScreen 信誉，也不能保证防病毒产品永不误报。

下载包的解除锁定仅用于已确认来源和校验值后的本地操作，不等于签名；无需关闭 Defender。仓库不提供关闭安全防护或自行添加不受信任根证书的脚本。
