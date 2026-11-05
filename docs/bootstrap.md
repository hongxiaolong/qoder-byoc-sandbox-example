# 账号资源自动准备

在仓库根目录运行 `./start.sh`。本版本保留自定义分析镜像构建、两个 Sandbox 和三轮任务，没有切换为开发环境替代自定义模板。

## 客户需要提供什么

- 阿里云 AK/SK 和 region；已有环境变量可以回车采用。STS 临时凭证还需配套 Token。
- Qoder 国际站 PAT：独立授权，阿里云凭证不能替代。输入 `open` 打开官方获取入口。
- 选择现有资源或新建，并批准实际变更；无需手填 Team ID、Sandbox Key、ACR 域名、实例 ID 和网络 ID。

凭证验证后依次选择 Team、API Key、ACR、VPC、交换机、安全组和私有仓库。之前选过且仍可用的资源排在第一项，回车即可复用。新建需单独确认，新购 ACR 会先展示目录价。上次已下单的 ACR 会先核对订单和实例状态。

Python 3.11+ 和可运行的 Docker/Buildx 仍是本机构建条件。Python 包安装自动执行，系统软件安装、账号实名、控制台服务协议确认及无权自行完成的 RAM 授权仍由用户/管理员处理。

## 实际执行顺序

1. 用独立 Qoder 凭证查询模型；用 STS GetCallerIdentity 核实阿里云账号，检查历史记录的账号和地域。
2. 完整分页查询 Team，显示名称、ID、计划。新建 Team 使用 QoS Default（接口的 `plan=std`）。只有一个可用资源组时自动采用，有多个时先选择归属。
3. 选择本机已有且可用的 API Key，或在所选 Team 下新建专用 Key。复用时核对已有归属记录及认证状态，云端列表只用于查询 Key 信息。
4. 查询同地域运行中的 ACR 企业版 Basic、Standard、Advanced。新购为 Basic 1 个月、手动续费。向导通过费用 API 展示所选地域的目录价，确认后自动下单并支付，实际扣款以订单为准。自动购买适用于中国站。选定实例后，向导立即验证本机 Docker 的公网登录。已有登录有效时直接沿用，否则用临时凭证经标准输入登录。
5. 以所选 ACR 绑定为优先级查询交换机，要求 FC 支持的可用区、可用状态、至少8个空闲地址；可复用现有 VPC 中的交换机、在现有 VPC 新建交换机，或新建独立 VPC。新交换机自动选择不重叠的 /24 网段。
6. 查询同 VPC 非服务托管的普通安全组。已有规则可确认允许 TCP/443 时可复用，否则选择新建，不修改旧规则。ACR 缺少绑定时，经批准创建 VPC 绑定，允许平台创建 PrivateZone 角色/记录，等待 RUNNING。
7. 准备私有仓库并从接口获取真实域名。新建专用资源以 `qoder-byoc-sandbox-` 开头。现有资源和未完成订单继续使用已记录的名称。
8. 开启公网入口后，等待 `Enable=true` 且 `Status=RUNNING`。用 `curl -4 ip.sb` 查询公网 IPv4，确认后只追加缺失的公网 `/32`，轮询读回白名单，核验通过后才获取临时凭证并登录 Docker。等待超时会暂停并保留记录，重试会继续核验。内网访问在 2.5 按所选 VPC 和交换机绑定。
9. 登录失败时显示关键错误和访问控制入口。VPN（含阿里郎）可能改变出口 IP，请核对实际出口并更新白名单。回车重试，`detail` 查看详情，`shell` 查看供另一终端执行的登录命令。若需回收或重跑，先按 `Ctrl+C` 结束当前向导并释放锁。旧版子终端须先输入 `exit` 返回向导。

如果 ACR 实例已不存在，可在 ACR 菜单改选其他实例，或核对旧订单后单独确认新购。原资源记录保留。

菜单按编号选择，回车默认选 1。需要授权的操作以 `yes` 确认，回车和其他输入只会重新提示。查询或创建报错会暂停，保留错误和资源记录。修正后按 `Ctrl+C` 退出再重跑。

按中国内地 Default 目录价，默认两个 Sandbox（合计 6 vCPU、12 GiB 内存）同时活跃 1 小时，CPU 和内存约 ¥0.94。磁盘、OSS 和 Qoder 用量另计，实际费用随用量变化。[Agent Sandbox 计费说明](https://help.aliyun.com/zh/agent-sandbox/product-overview/billing-overview)

查询或创建失败不视为“账号没有资源”。错误保留接口名、错误码、RequestId，不输出请求体/密钥。错误提示提供服务入口；完成开通、授权或余额处理后重新运行即可保留已完成进度。

## 恢复和回收

`var/state/bootstrap.json` 记录账号、地域、项目标识、资源归属、写操作意图、幂等 Token、完成状态；Key 明文只在 `config/.env`，不写资源账本。重跑自动匹配本项目资源。购买和支持幂等的网络创建使用相同 Token 恢复；没有幂等保障且结果未知的请求会先通过列表核对，不能安全确认时停止并要求核对该操作。明确的权限/参数拒绝可在修正后重试。禁止同时运行两个准备向导。

操作记录按目标 ACR、VPC 等参数隔离，同名命名空间和相同白名单地址不会跨实例继承归属。创建结果已保存、资源归属尚未保存时，可根据已完成操作里的精确资源 ID 恢复。卸载遇到明确的 NotFound 按已删除续跑；权限、资源占用和服务错误仍保留失败状态。

- `qoder-byoc-sandbox-example cleanup`：回收运行实例和 Qoder 资源，保留模板及基础设施。
- 收尾选择 **3 回收运行和准备资源（含历史）**。先回收本轮运行，再按当前配置和历史配置列出准备账本中的资源。每个资源只列一次。输入 `yes` 后按归属回收，项目创建的 API Key 会先核对所属 Team，再禁用并删除。已回收运行时，可直接执行 `.venv/bin/python scripts/bootstrap.py --uninstall`。
- 复用资源保留。归属不明时暂停核对。准备资源单项回收失败时标记「需手动处理」，其余可回收项继续执行。
- 自动开启的公网入口先关闭，再删除自己的白名单，避免移除最后一个条目后扩大暴露范围。
- 对清单标记「人工退订」的预付费 ACR 实例，进入费用与成本的「订单管理 → 退订管理」，按实例 ID 核对订单并退订。确认退订后，实例立即删除并停止计费。关联 OSS 存储会保留，请单独核对 Bucket、数据及用量。共享服务角色保留。 [费用与成本](https://usercenter2.aliyun.com/home) · [ACR 官方退订说明](https://help.aliyun.com/zh/acr/product-overview/refund-policy) · [OSS 控制台](https://oss.console.aliyun.com/)

不要删除资源账本或跨账号拷贝凭证后继续运行。脚本不允许将其他账号/地域覆盖进已有记录。同目录卸载后再次准备仍会重新查询云端；外部删除的资源遇到不确定记录应先核对，禁止手工篡改归属。

## 验证范围

新增测试使用内存云服务覆盖空白账号创建与重复运行、拒绝授权、账号切换、未知写请求禁止重放、购买超时幂等和归属保护；使用真正安装的官方 SDK 与本机 HTTP 服务验证 Team/Key 请求的签名、序列化和 RPC 分页参数。原有业务、网页、下载、Worker 和向导测试一并回归。

本次合入版本未使用真实凭证，未创建云资源或真实购买。空白账号首次开通、ACR 购买、服务角色授权、PrivateZone、不同地域容量和端到端 LIVE_PASS 仍需在实际账号验收；本地测试不能证明这些外部条件已满足。

## 官方接口依据

- [Sandbox 原生 SDK](https://help.aliyun.com/zh/agent-sandbox/alibaba-cloud-sdk-api-description-1)
- [Team 创建及开通前提](https://help.aliyun.com/zh/agent-sandbox/create-team)
- [资源组查询 ListResourceGroups](https://help.aliyun.com/zh/resource-management/api-listresourcegroups)
- [Key 明文与管理边界](https://help.aliyun.com/zh/agent-sandbox/api-key-management)
- [ACR 创建并支付](https://help.aliyun.com/zh/acr/developer-reference/createinstance-1)
- [预付费目录价查询](https://help.aliyun.com/zh/user-center/developer-reference/api-bssopenapi-2017-12-14-getsubscriptionprice)
- [ACR VPC 绑定](https://help.aliyun.com/zh/acr/developer-reference/api-cr-2018-12-01-createinstancevpcendpointlinkedvpc)
- [临时登录凭证](https://help.aliyun.com/zh/acr/developer-reference/api-cr-2018-12-01-getauthorizationtoken)
- [FC 支持的可用区](https://help.aliyun.com/zh/functioncompute/configure-network-settings)，清单核对日期 2026-10-29。
