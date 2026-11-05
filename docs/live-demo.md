# 实操指引：三轮引导与连续对话

首次体验用项目根目录的 `./start.sh`，按 [README](../README.md) 六阶段完成。本文用于手动逐条执行和排障；所有命令在项目根目录运行。Web 只观察执行，消息由终端或 Qoder 控制台发送。

## 1. 安装依赖

需要 Python 3.11+、已启动的 Docker 与 Buildx：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[example,build]"
qoder-byoc-sandbox-example --help
```

看到命令帮助即表示本机入口可用；尚未验证云端权限或创建资源。新开终端后需重新激活 `.venv`，或直接使用 `.venv/bin/qoder-byoc-sandbox-example`。

## 2. 配置连接

已有配置时保留；`cp -n` 不覆盖：

```bash
cp -n config/.env.example config/.env
cp -n config/.env.cloud.example config/.env.cloud
cp -n config/registry.example.toml config/registry.toml
chmod 600 config/.env config/.env.cloud
```

`config/.env` 填 Qoder 国际站 PAT 和对应阿里云 Team 的 Key。默认地址如下，不能混用 E2B Cloud Key：

```dotenv
QODER_ACCESS_TOKEN=<Qoder 国际站 PAT>
E2B_API_KEY=<阿里云 Sandbox Team API Key>
E2B_API_URL=https://api.cn-hangzhou.sandbox.aliyuncs.com
E2B_DOMAIN=cn-hangzhou.sandbox.aliyuncs.com
```

`config/.env.cloud` 填 `ALIBABA_CLOUD_ACCESS_KEY_ID`、`ALIBABA_CLOUD_ACCESS_KEY_SECRET`；临时凭证另填 `ALIBABA_CLOUD_SECURITY_TOKEN`。AK 用于阿里云准备与模板管理，不注入 Worker。Qoder API 地址由 `config/app.toml` 配置，默认调用国际站。

`config/registry.toml` 填实际 ACR 仓库、地域、Team 和镜像拉取网络。`team_id` 必须与 Key 对应，不能仅凭 Team 名称认定相同：

```toml
repository="<实例名>-registry.cn-hangzhou.cr.aliyuncs.com/sandboxhub/qoder-byoc-sandbox-example"
instance_id="<ACR 企业版实例 ID>"
vpc_id="<VPC ID>"
vswitch_id="<vSwitch ID>"
security_group_id="<安全组 ID>"
region="cn-hangzhou"
team_id="<与 Key 对应的 Team ID>"
```

手动流程需事先准备私有命名空间和仓库，并在 ACR 绑定上述 VPC，等待 `RUNNING`。三个网络字段需一起填写，仅用于平台拉取镜像；公网 Docker 登录不能证明 VPC 拉取可用。自动查询、复用和写前确认可交给 `./start.sh`，见[账号准备](bootstrap.md)。

按 ACR 指引完成 Docker 登录。已有登录可复用；需要脚本登录时，复制 `config/.env.registry.example` 为 `config/.env.registry`，填写 `ACR_USERNAME`、`ACR_PASSWORD`，密码经标准输入传给 Docker。账号仍需具备服务开通、模板创建、仓库和网络权限；新建、购买与资源存续可能产生费用。

```bash
qoder-byoc-sandbox-example doctor
```

输出 `doctor_pass` 仅说明 Qoder 模型列表可访问；Sandbox、模板与 ACR 路径由后续步骤验证。真实凭证不提交，见 [SECURITY](../SECURITY.md)。

## 3. 准备模板

```bash
qoder-byoc-sandbox-example templates browser
qoder-byoc-sandbox-example templates analysis
```

Browser 基于官方 Chromium 镜像；Analysis 构建含 Worker、Python 依赖与数据服务的自定义镜像，推送 ACR 后等待模板 `ready`。配置在 `config/templates.toml`，ID 与创建记录保存在 `var/state/templates.json`。

以上手动命令每次创建新模板。已有就绪且匹配源码的模板时无需重复；向导会先检查再决定是否构建。修改运行源码或镜像内容后重新构建分析模板。构建尚未返回 ID 就被中断，应先核对控制台，避免重复提交。

## 4. 创建运行并打开观察页

```bash
qoder-byoc-sandbox-example setup
qoder-byoc-sandbox-example status
```

`setup` 创建两个 Sandbox，核对镜像源码与服务，上传并核对 Skill，创建 Agent、Self-hosted Environment、Session，随后在 Analysis 启动 Worker。看到 `setup_ready`、会话地址和两个不同的 Sandbox ID 后继续。`status` 是本地运行记录，不代替实时健康检查。

另开终端，激活 `.venv` 后运行：

```bash
qoder-byoc-sandbox-example preview
```

打开输出的本机地址，默认 [http://127.0.0.1:8765](http://127.0.0.1:8765)。左侧选择当前 Session 的用户输入，主区看 QCA、Environment 与两个 Sandbox，右侧看调用详情；明暗主题保持相同布局，详见[观察页规范](live-observability.md)。没有报表或文件下载入口；每条已结束输入可回放已记录的事件，回放不重新执行任务。

## 5. 核对本次使用的资源

在页面查看“使用 Agent vN”、Skills、Session 与完整实例 ID，并与本轮终端输出核对。使用能够验证的 Session 地址进入 Qoder；其他资源通过控制台入口和复制 ID 查找，不根据 ID 猜测直达链接。

Analysis 运行 Worker 和 Python，Browser 单独运行 Chromium。Environment 表达 Analysis 执行端的逻辑映射；Agent Loop、WorkQueue 与 Session events 位于 QCA。不要在 Environment 的 Setup script 中输入任务。

## 6. 第一轮：下载并分析

在终端执行，命令发送 `config/demo.toml` 的第一轮消息，等待结束并独立验收：

```bash
qoder-byoc-sandbox-example task --phase collect
```

消息要求按 2026 年 10 月、华东筛选合成数据，真实翻页、下载 CSV 并传给 Analysis，再运行生成的 Python、发布和导出。观察页显示实际工具请求、CDP 操作和结果回传；终端核对两端哈希和导出文件。默认 **100 条、45,392.18**，`verification` 全部为 `true` 才继续。

## 7. 第二轮：连续追问

```bash
qoder-byoc-sandbox-example task --phase revise
```

沿用 `input.csv`，排除金额最高的分类并重新计算。默认排除“计算”，剩余 **65 条、28,508.77**；输入哈希不变，仍使用同一 Session 与两个实例。终端验收通过后再继续。

## 8. 第三轮：追加记录

```bash
qoder-byoc-sandbox-example task --phase append
```

恢复全部分类，在工作副本追加一条“现场演示”、金额 6,666 的记录，重新执行分析并更新产物。默认 **101 条、52,058.18**；原始数据源不变，仅增加一条记录。三轮全部验收通过后输出 `LIVE_PASS`。

三轮完整消息在 `config/demo.toml`。若改用 Qoder 控制台，逐轮复制该配置中的消息发送，再将对应命令换为 `observe --phase collect|revise|append`。同一轮只选 `task` 或 `observe`，等上一轮验收通过再提交；超时后用 `resume` 接续，尤其不要重复追加记录。

## 9. 查看真实结果

```bash
qoder-byoc-sandbox-example verify --phase append
```

`verify` 重读远端文件核算，不再发任务，也不用来等待正在执行的任务。检查全部通过后，本地 `var/results/<run_id>/` 保存各轮验收 JSON、分析代码、Worker 记录、Excel 和浏览器截图；可直接打开 `analysis.xlsx` 核对数据。

需要检查远端时，在 Analysis 实例查看 `/workspace/input.csv`、`/workspace/analysis.py`、`/workspace/artifacts/` 与 `/workspace/evidence/transfer.json`。Browser 下载文件的真实路径以 `transfer.json` 的 `browser_path` 为准。其余产物如需保留，应在释放实例前用文件 API 另行保存。

## 10. 三轮后继续对话

```bash
qoder-byoc-sandbox-example chat
qoder-byoc-sandbox-example chat --message "沿用当前 CSV，恢复全部分类，并解释计算过程。"
```

也可在同一 Qoder Session 发送；观察页只跟踪这些消息的执行。终端输入 `/exit` 结束 chat 返回主菜单；提交或等待回复期间也有效，退出只结束本机对话界面，已提交任务继续执行并可在 Web 观察。再次发送消息前先确认上一轮结束。发送结果不明确时，先核对会话，再输入 `/retry` 或执行 `chat --retry` 沿用原消息编号重试；`/discard` 只放弃本地重试记录，不撤回消息。自由对话不套用默认三轮数字验收，发布产物仍需核算；显式 `verify --phase append` 仍检查第三轮固定规则。

需要命令行从零连续执行三轮时，可用 `qoder-byoc-sandbox-example all --keep`；`--keep` 保留运行供核对，结束后仍需手动回收。已有未回收资源时，不重复 `setup`。

## 11. 排障与回收

- **授权或 Key 错误**：核对账号、Team 与 Endpoint。配置文件中的非空值优先于环境变量，不打印凭证排障。
- **镜像推送中断**：检查 Docker 网络、代理或 VPN，再重跑；不要删除状态记录。权限和证书错误需先修正。
- **模板拉取或源码核对失败**：检查 ACR 地域、镜像 tag、Team 与 VPC 拉取配置；源码变化后重建分析模板，不临时覆盖旧实例来完成验收。
- **端口占用**：在原预览终端 `Ctrl+C`，或修改 `config/demo.toml` 的 `preview_port`。向导不会停止未知进程。
- **观测不可用**：看具体字段的不可用或过期提示；不把队列查询失败当作空队列，不把断线当作实例已销毁。
- **任务超时或提交结果不明确**：先查看 Qoder 会话和 `logs`；引导任务用 `resume` 接续，不重复提交。不要并行启动第二个 Worker。
- **setup 中途失败**：先 `status`。仅在尚无 Qoder 资源记录且两套服务恢复时用 `resume-setup`；否则 `cleanup` 后重建。
- **创建结果待核对**：创建前已保存 `pending_creates`。超时或断线不能证明云端未创建；向导不会重复创建，`cleanup` 会先处理已知资源，但不会输出 `cleanup_pass`。保留 `var/state/run.json`，在相同账号、地域和 Endpoint 按 `metadata.demo_run=<run_id>` 查找，Sandbox 另核对 `role=browser/analysis`。找到资源后将 ID 补入对应运行字段用于按归属清理；只有确认待核对资源不存在或已回收，才能清除对应 `pending_creates` 条目并重新执行 `cleanup`。不要删除账本或直接改为 `cleaned=true`。
- **实例到期**：默认超时在 `config/app.toml`。Work 心跳不延长实例寿命；无持久卷，不承诺文件或进程恢复。

```bash
qoder-byoc-sandbox-example cleanup
# 需要进一步卸载准备资源时，在 cleanup 成功后执行
.venv/bin/python scripts/bootstrap.py --uninstall
```

`cleanup_pass` 表示本轮两个 Sandbox 和 Qoder 资源回收完成，镜像、模板及本地结果保留。卸载仅处理有归属记录的准备资源；未知模板保留并停止拆除依赖。ACR 购买项、默认 OSS 与共享服务角色不自动退订或删除，需另行核对费用。不要先删除 `var/state/`，本机预览由启动它的终端关闭。

## 参考资料

- [通过 SDK 使用 Agent Sandbox](https://help.aliyun.com/zh/agent-sandbox/using-the-cloud-sandbox-via-the-sdk)、[Browser Sandbox](https://help.aliyun.com/zh/agent-sandbox/use-browser-use-sandbox)、[通过代码构建模板](https://help.aliyun.com/zh/agent-sandbox/build-and-manage-custom-templates-with-code)。
- 本指引的操作、成功判定与证据组织参考[Agent Sandbox 实操](https://alidocs.dingtalk.com/i/nodes/KGZLxjv9VGkoG9YwHqLDNyjPV6EDybno)和[Agent 平台体验指引](https://alidocs.dingtalk.com/i/nodes/93NwLYZXWyxXroNzC07KqzkP8kyEqBQm)，不表示本例复现其中其他平台能力。
