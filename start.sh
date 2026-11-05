#!/usr/bin/env bash
# Interactive BYOC setup. Completed stages stay in the terminal history.

set -euo pipefail

# Parse the complete wizard before executing it. An update to start.sh while a
# cloud build is running must not change Bash's subsequent file read offsets.
run_wizard() {

if [[ -t 1 && -z "${NO_COLOR+x}" ]] && command -v tput >/dev/null 2>&1 && [[ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]]; then
  BOLD=$(tput bold); DIM=$(tput dim); RESET=$(tput sgr0)
  BLUE=$(tput setaf 4); GREEN=$(tput setaf 2); YELLOW=$(tput setaf 3)
else
  BOLD=""; DIM=""; RESET=""; BLUE=""; GREEN=""; YELLOW=""
fi

TOTAL_STAGES=6
_STAGE_INDEX=0

# Append headings without clearing terminal history.
stage() {
  _STAGE_INDEX=$((_STAGE_INDEX + 1))
  printf '\n%s%s▸ Stage %s/%s · %s%s\n' \
    "$BOLD" "$BLUE" "$_STAGE_INDEX" "$TOTAL_STAGES" "$1" "$RESET"
}

# say "..." prints a plain instruction line.
say()  { printf '  %s\n' "$1"; }
note() { printf '  %s%s%s\n' "$DIM" "$1" "$RESET"; }
warn() { printf '  %s⚠ %s%s\n' "$YELLOW" "$1" "$RESET"; }

# open_url URL opens it in the human's browser, cross-platform incl. WSL.
open_url() {
  local url="$1"
  printf '  %s↗ 打开%s %s\n' "$GREEN" "$RESET" "$url"
  { if   command -v wslview     >/dev/null 2>&1; then wslview "$url"
    elif command -v explorer.exe >/dev/null 2>&1; then explorer.exe "$url"
    elif command -v xdg-open    >/dev/null 2>&1; then xdg-open "$url"
    elif command -v open        >/dev/null 2>&1; then open "$url"
    else false; fi
  } >/dev/null 2>&1 || warn "无法打开浏览器，请手动访问 $url"
}

# pause "msg" waits for the human to confirm they've done the manual part.
pause() {
  printf '  %s%s%s ' "$DIM" "$1" "$RESET"
  while ! IFS= read -r _; do
    printf '  输入未完成；按回车继续，Ctrl+C 退出。\n'
    sleep 0.2
  done
}

# The same concise stories drive the live wizard and the read-only preview.
step_copy() {
  case "$1" in
    1) STEP_NAME="准备本机"; STEP_WHY="让电脑能操作 Qoder、构建 Sandbox 镜像。"; STEP_FLOW="检查 Python → 安装 example/build 依赖 → 验证命令"; STEP_OUTPUT="本机命令可用；这一步不创建云资源。" ;;
    2) STEP_NAME="连接账号与基础资源"; STEP_WHY="用 AK/SK 查询阿里云资源；Qoder 使用独立 PAT。"; STEP_FLOW="验证身份 → 准备阿里云 Agent Sandbox 极速版 Team、ACR 和网络 → 保存配置"; STEP_OUTPUT="逐项选择，已有可用配置优先；新购 ACR 单独确认。" ;;
    3) STEP_NAME="准备两套工具模板"; STEP_WHY="把网页采集和 Python 分析环境分别准备好。"; STEP_FLOW="核对现有模板 → 构建并推送缺失镜像 → 等待 ready"; STEP_OUTPUT="两个可复用模板，供下一步创建执行实例。" ;;
    4) STEP_NAME="连接运行现场"; STEP_WHY="让 Qoder 的工具调用能在两个 Sandbox 中真正执行。"; STEP_FLOW="模板 → 两个实例 → Worker 接入 Qoder → 打开演示页"; STEP_OUTPUT="一个 Qoder 会话、两个 Sandbox ID 和一个可观察的执行现场。" ;;
    5) STEP_NAME="运行三轮对话"; STEP_WHY="观察同一会话如何采集数据、连续追问和追加记录。"; STEP_FLOW="首次分析 → 复用 CSV 追问 → 追加 6,666 并重新交付"; STEP_OUTPUT="默认数据依次为 100、65、101 条；数据条数不含表头。" ;;
    6) STEP_NAME="验收与收尾"; STEP_WHY="用实际文件证明结果，再决定保留哪些资源。"; STEP_FLOW="核对文件 → 下载结果 → 回收本轮运行或保留"; STEP_OUTPUT="运行资源与基础设施分开回收，明确仍保留的资源。" ;;
  esac
}

step_story() {
  printf '  %s目的%s  %s\n' "$DIM" "$RESET" "$STEP_WHY"
  printf '  %s路径%s  %s\n' "$DIM" "$RESET" "$STEP_FLOW"
  printf '  %s得到%s  %s\n\n' "$DIM" "$RESET" "$STEP_OUTPUT"
}

begin_step() {
  step_copy "$((_STAGE_INDEX + 1))"
  stage "$STEP_NAME"
  show_route
  note "$STEP_WHY"
}

preview_steps() {
  printf '\n  Qoder Cloud Agents × Agent Sandbox · BYOC 向导预览\n'
  printf '  仅展示操作逻辑；不安装、不连接账号、不创建资源。\n'
  local n
  for n in 1 2 3 4 5 6; do
    step_copy "$n"
    printf '\n  ━━ %s / 6 · %s ━━\n\n' "$n" "$STEP_NAME"
    step_story
  done
}

# Project-specific presentation.
show_route() {
  local position=0 name
  printf '  %s' "$DIM"
  for name in 依赖 配置 模板 资源 演示 收尾; do
    position=$((position + 1))
    if [[ "$position" -lt "$_STAGE_INDEX" ]]; then
      printf '✓ %s  ' "$name"
    elif [[ "$position" -eq "$_STAGE_INDEX" ]]; then
      printf '%s● %s%s  ' "$BLUE$BOLD" "$name" "$RESET$DIM"
    else
      printf '○ %s  ' "$name"
    fi
  done
  printf '%s\n' "$RESET"
}

usage() {
  cat <<'USAGE'
Qoder Cloud Agents × Agent Sandbox 极速版：BYOC 接入实战

用法：./start.sh
  --help  显示本说明，不安装依赖或创建资源
  --preview  预览六阶段的操作逻辑，不执行实际操作

六阶段引导：安装 → 配置 → 仓库与模板 → 运行资源 → 三轮演示 → 验收与回收。
已有配置可保留；重复运行会检查现有资源和任务进度。
阿里云仅需 AK/SK、region（临时凭证另需 STS Token）；Qoder PAT 独立填写。
逐项选择 Team、API Key、ACR 和网络。已有配置可优先沿用，新建和购买需明确确认。
安装时显示真实 pip 阶段与活动条，完整日志保存在 var/logs/install.log。
填写配置时输入 ? 查看示例和来源，输入 open 打开对应配置页面。
Ctrl+C 停止向导及本次启动的预览，保留云端运行记录，稍后可再次 ./start.sh。
普通输入不会退出；报错后停在现场，修正后 Ctrl+C 退出再重跑，不自动重复云端操作。
收尾可仅回收本轮 Qoder/Sandbox 运行资源，也可同时回收本项目创建的准备资源。
复用资源保留，预付费 ACR 实例需手动退订，OSS 另行核对。
USAGE
}

if [[ "$#" -gt 0 ]]; then
  case "$1" in --help|-h) usage; exit 0 ;; --preview) preview_steps; exit 0 ;; *) usage; exit 2 ;; esac
fi
if [[ ! -t 0 ]]; then
  printf '%s\n' '请在交互式终端中运行 ./start.sh；查看说明可运行 ./start.sh --help。' >&2
  exit 2
fi

PROJECT_ROOT=$(cd -- "$(dirname -- "$0")" && pwd)
cd "$PROJECT_ROOT"
if [[ ! -f pyproject.toml || ! -f scripts/onboarding.py || ! -f scripts/terminal_progress.py ]]; then
  printf '%s\n' '请在完整项目中运行 start.sh，不要只下载这一个脚本。' >&2
  exit 2
fi
export QODER_SANDBOX_HOME="$PROJECT_ROOT"
export PYTHONDONTWRITEBYTECODE=1
umask 077
HELPER="$PROJECT_ROOT/scripts/onboarding.py"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
CLI="$PROJECT_ROOT/.venv/bin/qoder-byoc-sandbox-example"
LOCK_DIR="$PROJECT_ROOT/var/onboarding.lock"
PREVIEW_PID=""
REUSED_PREVIEW=0
KEEP_CONFIG=0
RESTORE_COMPLETED=0
CLEANED_THIS_RUN=0
SIGNAL_EXIT=0
LOCK_HELD=0

read_choice() {
  printf '  %s ' "$1"
  while ! IFS= read -r REPLY; do
    say "输入未完成；请继续选择，Ctrl+C 退出。"
    sleep 0.2
  done
}

confirm_cloud_action() {
  while true; do
    read_choice "$1 · 输入 yes 确认，Ctrl+C 退出："
    if [[ "$REPLY" =~ ^[[:space:]]*yes[[:space:]]*$ ]]; then return; fi
    warn "尚未执行，请输入 yes 确认。"
  done
}

choose_menu() {
  local index=0 label
  for label in "$@"; do
    index=$((index + 1))
    say "  $index. $label"
  done
  while true; do
    read_choice "选择编号 [1]："
    REPLY=${REPLY:-1}
    for ((index=1; index<=$#; index++)); do
      if [[ "$REPLY" == "$index" ]]; then return; fi
    done
    warn "请选择列出的项目；Ctrl+C 退出。"
  done
}

wait_for_interrupt() {
  say "$1"
  while true; do
    if IFS= read -r REPLY; then
      note "${2:-当前阶段已暂停；Ctrl+C 退出后可重新运行 ./start.sh。}"
    else
      sleep 0.2
    fi
  done
}

stop_preview() {
  if [[ -n "$PREVIEW_PID" ]] && kill -0 "$PREVIEW_PID" 2>/dev/null; then
    kill "$PREVIEW_PID" 2>/dev/null || true
    wait "$PREVIEW_PID" 2>/dev/null || true
  fi
  PREVIEW_PID=""
}

release_local() {
  stop_preview
  if [[ "$LOCK_HELD" -eq 1 ]]; then
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || true
    LOCK_HELD=0
  fi
}

signal_exit() {
  SIGNAL_EXIT=1
  trap '' INT TERM
  release_local
  say "向导已停止；配置和资源记录保留，下次运行 ./start.sh 可继续。"
  exit "$1"
}

on_exit() {
  local code="$1"
  # Keep errexit enabled: stop at the failed command, then wait here rather
  # than retrying a stage or advancing past an unverified cloud operation.
  if [[ "$code" -ne 0 && "$SIGNAL_EXIT" -eq 0 ]]; then
    warn "第 $_STAGE_INDEX 阶段出现错误，流程已暂停；已有配置和资源记录保留。"
    say "请检查上面的错误；不会跳过检查或自动重复云端操作。不要删除 var/state/。"
    if [[ -f "$PROJECT_ROOT/var/state/run.json" ]]; then
      say "需回收本轮运行时：.venv/bin/qoder-byoc-sandbox-example cleanup"
    elif [[ -f "$PROJECT_ROOT/var/state/bootstrap.json" ]]; then
      say "基础资源记录：var/state/bootstrap.json；退出不会自动撤销已创建的账号资源。"
    fi
    wait_for_interrupt "修正后按 Ctrl+C 退出，再运行 ./start.sh 从记录继续。"
  fi
  release_local
}

mkdir -p "$PROJECT_ROOT/var/logs"
while ! mkdir "$LOCK_DIR" 2>/dev/null; do
  lock_pid=""
  if [[ -f "$LOCK_DIR/pid" ]]; then IFS= read -r lock_pid < "$LOCK_DIR/pid" || true; fi
  if [[ "$lock_pid" =~ ^[0-9]+$ ]] && ! kill -0 "$lock_pid" 2>/dev/null; then
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR"
  else
    warn "另一个向导正在运行，或锁记录尚未写完；请先回到原终端完成或退出。"
    note "如果当前终端是旧向导打开的 shell，请先按 Ctrl+C 停止这次启动，再输入 exit 返回原向导。"
    pause "原向导退出后按回车重检，Ctrl+C 停止本次启动。"
  fi
done
LOCK_HELD=1
printf '%s\n' "$$" > "$LOCK_DIR/pid"
trap 'on_exit "$?"' EXIT
trap 'signal_exit 130' INT
trap 'signal_exit 143' TERM
trap 'signal_exit 129' HUP

helper() { "$VENV_PYTHON" "$HELPER" "$@"; }

run_cli() {
  case "$1" in
    task|observe|resume|verify)
      "$CLI" "$@" | helper display-events
      ;;
    templates|setup|resume-setup|doctor|cleanup)
      local label log_name
      case "$1" in
        templates) label="准备 $2 工具模板"; log_name="template-$2" ;;
        setup|resume-setup) label="创建实例并连接 Qoder"; log_name="setup" ;;
        doctor) label="验证 Qoder 连接"; log_name="connection" ;;
        cleanup) label="回收本轮运行资源"; log_name="cleanup" ;;
      esac
      "$VENV_PYTHON" "$PROJECT_ROOT/scripts/terminal_progress.py" --mode operation \
        --log "$PROJECT_ROOT/var/logs/$log_name.log" --label "$label" -- "$CLI" "$@"
      ;;
    *) "$CLI" "$@" ;;
  esac
}

find_python() {
  local candidate
  for candidate in python3.13 python3.12 python3.11 python3.14 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
      BOOTSTRAP_PYTHON=$(command -v "$candidate")
      return 0
    fi
  done
  return 1
}

say "Qoder Cloud Agents × Agent Sandbox · 六阶段 BYOC 实操向导"
note "可随时按 Ctrl+C，重跑时继续已有进度。完整流程见 ./start.sh --preview。"

begin_step
until find_python; do
  warn "需要 Python 3.11+；安装完成后回来继续。"
  open_url "https://www.python.org/downloads/"
  pause "安装后按回车重新检查，Ctrl+C 退出。"
done
if [[ ! -x "$VENV_PYTHON" ]]; then
  "$BOOTSTRAP_PYTHON" -m venv .venv
fi
"$VENV_PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), "现有 .venv 需要 Python 3.11+"'
"$VENV_PYTHON" "$PROJECT_ROOT/scripts/terminal_progress.py" \
  --log "$PROJECT_ROOT/var/logs/install.log" \
  --label "准备 Qoder × Sandbox 的演示与构建依赖" \
  -- "$VENV_PYTHON" -m pip install --progress-bar off -e ".[example,build]"
"$CLI" --help >/dev/null
say "✓ 本地命令已就绪，无需手动激活虚拟环境。"

begin_step
if "$VENV_PYTHON" -c 'import json, pathlib, sys; p=pathlib.Path("var/state/run.json"); sys.exit(0 if p.exists() and not json.loads(p.read_text()).get("cleaned") else 1)'; then
  helper summary
  if ! existing_status=$(helper run-status); then exit 1; fi
  while true; do
    if [[ "$existing_status" == "ready" ]]; then
      say "当前运行可继续；沿用连接配置即可，不重复创建资源。"
      choose_menu "继续当前运行" "回收旧运行并重新开始"
    else
      warn "旧运行状态：${existing_status}，无法直接续接。需先回收这一轮，再用新模板重建。"
      choose_menu "保留旧运行并查看记录" "回收旧运行并重新开始"
    fi
    case "$REPLY" in
      2)
        run_cli cleanup
        break
        ;;
      1)
        if [[ "$existing_status" != "ready" ]]; then
          helper summary
          note "Ctrl+C 退出并保留旧运行。"
          continue
        fi
        KEEP_CONFIG=1
        say "将沿用当前运行及连接配置，跳过修改凭证。"
        break
        ;;
    esac
  done
fi
if [[ "$KEEP_CONFIG" -eq 0 ]]; then
  until command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 && docker buildx version >/dev/null 2>&1; do
    warn "请先安装并启动 Docker 与 Buildx，再准备云资源；本机就绪前不新建收费资源。"
    open_url "https://www.docker.com/products/docker-desktop/"
    pause "安装并启动后按回车重检；Ctrl+C 退出。"
  done
  "$VENV_PYTHON" "$PROJECT_ROOT/scripts/bootstrap.py"
fi
helper check-config >/dev/null
if [[ "$KEEP_CONFIG" -eq 1 ]]; then
  run_cli doctor
  RESTORE_COMPLETED=1
  for phase in collect revise append; do
    phase_status=$(helper phase-state "$phase")
    case "$phase_status" in
      verified) ;;
      new|submitted|done) RESTORE_COMPLETED=0 ;;
      *) warn "无法识别任务状态：$phase_status"; exit 1 ;;
    esac
  done
fi
if [[ "$RESTORE_COMPLETED" -eq 0 ]]; then
  begin_step
  if [[ "$KEEP_CONFIG" -eq 1 ]]; then
    say "✓ 当前运行已就绪，沿用其现有模板和实例，不重复构建。"
  else
    say "先核对私有仓库和镜像拉取通路；已就绪且匹配当前代码的模板直接复用。"
    pause "现在准备仓库和两个 Sandbox 模板：按回车继续，Ctrl+C 退出。"
    helper template-team-check
    helper prepare-registry --verify-only
    for kind in browser analysis; do
      case "$kind" in browser) role_name="网页采集 · Browser" ;; analysis) role_name="报表分析 · Python" ;; esac
      say "$role_name"
      CHECK_LOG="$PROJECT_ROOT/var/logs/check-$kind.log"
      if helper template-check "$kind" >"$CHECK_LOG" 2>&1; then
        say "✓ 云端就绪且配置匹配，复用。"
      else
        check_code=$?
        if [[ "$check_code" -ne 1 ]]; then
          tail -n 3 "$CHECK_LOG"
          warn "模板尚不能安全复用；请按上面的状态提示处理后重跑，不自动创建重复模板。"
          exit "$check_code"
        fi
        note "$(tail -n 1 "$CHECK_LOG")"
        say "开始准备；详细构建输出写入日志，终端只显示当前阶段。"
        if [[ "$kind" == "browser" ]]; then
          note "首次构建请等待 Template ID；ID 返回前中断，重跑前需先核对云端模板，避免重复创建。"
        fi
        if [[ "$kind" == "analysis" ]]; then
          helper docker-login
          run_cli templates "$kind" --use-docker-login
        else
          run_cli templates "$kind"
        fi
        if helper template-check "$kind" >"$CHECK_LOG" 2>&1; then
          :
        else
          check_code=$?
          tail -n 3 "$CHECK_LOG"
          warn "刚创建的模板尚未核对通过；ID 已保留，请核对模板状态、阿里云 Agent Sandbox 极速版 Team 及该 Team 下的 API Key。"
          exit "$check_code"
        fi
        say "✓ 模板已就绪，镜像与当前代码一致。"
      fi
      helper template-summary "$kind"
    done
  fi
  begin_step
  current_status=$(helper run-status)
  case "$current_status" in
    empty|cleaned)
      say "将创建 Qoder Agent、Skill、Self-hosted Environment、Session，以及两个 Sandbox。"
      confirm_cloud_action "创建本轮云端运行资源"
      run_cli setup
      ;;
    ready) say "✓ 当前 Session、双 Sandbox 和 Worker 可用，继续本轮，不重复创建。" ;;
    partial|expired)
      warn "检测到 $current_status 运行，需回收已有记录后重新创建。"
      confirm_cloud_action "回收这轮已有 Qoder/Sandbox 资源并重建"
      run_cli cleanup
      run_cli setup
      ;;
    *) warn "无法识别运行状态：$current_status"; exit 1 ;;
  esac
  helper summary
else
  # The run may have expired while the operator was choosing continue. Keep
  # the same live/source checks as the normal runtime stage before restoring.
  current_status=$(helper run-status)
  if [[ "$current_status" != "ready" ]]; then
    warn "当前运行已不再可用（${current_status}）；停止恢复，不自动回收或重建。"
    exit 1
  fi
  say "✓ 三轮引导均已验收；恢复本轮观察页，直接进入对话与回收菜单。"
fi
session_url=$(helper show-links | "$VENV_PYTHON" -c 'import json, sys; print(json.load(sys.stdin)["session_url"])')
preview_url=$(helper show-links | "$VENV_PYTHON" -c 'import json, sys; print(json.load(sys.stdin)["preview_url"])')
PREVIEW_LOG="$PROJECT_ROOT/var/onboarding-preview.log"
while true; do
  preview_status=$(helper preview-status)
  case "$preview_status" in
    ready)
      REUSED_PREVIEW=1
      say "✓ 已有预览与当前 run_id 一致，直接复用。"
      break
      ;;
    free)
      "$CLI" preview > "$PREVIEW_LOG" 2>&1 &
      PREVIEW_PID=$!
      preview_deadline=$((SECONDS + 30))
      preview_status="free"
      while [[ "$SECONDS" -lt "$preview_deadline" ]]; do
        if ! kill -0 "$PREVIEW_PID" 2>/dev/null; then
          cat "$PREVIEW_LOG"
          exit 1
        fi
        preview_status=$(helper preview-status)
        if [[ "$preview_status" == "ready" ]]; then break; fi
        sleep 0.3
      done
      if [[ "$preview_status" != "ready" ]]; then
        warn "预览未在预期时间内就绪，详情见 var/onboarding-preview.log。"
        exit 1
      fi
      say "✓ 本机预览已启动：$preview_url"
      break
      ;;
    occupied)
      warn "预览端口被其他服务或旧运行占用；向导不会停止未知进程。"
      say "请在旧预览终端 Ctrl+C，或修改 config/demo.toml 的 preview_port。"
      pause "处理后按回车重新检查。"
      preview_url=$(helper show-links | "$VENV_PYTHON" -c 'import json, sys; print(json.load(sys.stdin)["preview_url"])')
      ;;
    *) warn "无法识别预览状态：$preview_status"; exit 1 ;;
  esac
done
open_url "$session_url"
open_url "$preview_url"
say "✓ 观察页已连接，可查看 Agent Loop、两套 Sandbox 与调用追踪。"
if [[ "$RESTORE_COMPLETED" -eq 0 ]]; then
  begin_step
  say "Agent 决策和工具调用由 Qoder 完成。"
  choose_menu "由向导发送提示词" "在 Qoder 页面手动发送"
  TASK_MODE="task"
  if [[ "$REPLY" == 2 ]]; then TASK_MODE="observe"; fi
  for phase in collect revise append; do
    phase_status=$(helper phase-state "$phase")
    helper round-intro "$phase" --status "$phase_status"
    case "$phase_status" in
      verified) say "✓ 本轮已验收，沿用结果；不重复发送消息。"; continue ;;
      done) say "该轮已执行结束，继续独立验收，不再发消息。"; run_cli verify --phase "$phase" ;;
      submitted) say "该轮任务已提交，继续等待与验收，不再发送。"; run_cli resume ;;
      new)
        if [[ "$TASK_MODE" == "observe" ]]; then
          say "在本次 Qoder 会话发送上方的提示词。若已发送，无需重发。"
          pause "确认只发送一条新消息后，按回车观察并验收。"
        else
          pause "按回车发送对话框中的提示词给 Qoder；Ctrl+C 退出。"
        fi
        run_cli "$TASK_MODE" --phase "$phase"
        ;;
      *) warn "无法识别任务状态：$phase_status"; exit 1 ;;
    esac
  done
  helper summary
fi

_STAGE_INDEX=5
begin_step
helper finish-summary
say "✓ 默认三轮已完成验收，本地记录在 var/results/。"
note "终端与 Qoder 控制台共用同一会话，观察页持续跟踪。"
note "复用资源保留。项目新购的预付费 ACR 实例须到「费用与成本 → 订单管理 → 退订管理」手动退订，OSS 另行核对。"
while true; do
  choose_menu "继续自由对话" "回收本轮 Qoder 和 Sandbox" "回收运行和准备资源（含历史）" "保留云资源" "查看观察页"
  case "$REPLY" in
    1)
      if ! run_cli chat; then
        warn "对话暂未完成；可查看 Qoder 状态后继续，或在此回收运行资源。"
      fi
      ;;
    2)
      run_cli cleanup
      CLEANED_THIS_RUN=1
      say "✓ 本轮运行资源已回收，模板和镜像保留，下次运行 ./start.sh 可复用。"
      break
      ;;
    3)
      if [[ ! -f "$PROJECT_ROOT/var/state/bootstrap.json" ]]; then
        warn "当前配置没有自动准备账本。请选择 2 回收本轮运行资源。"
        continue
      fi
      run_cli cleanup
      CLEANED_THIS_RUN=1
      "$VENV_PYTHON" "$PROJECT_ROOT/scripts/bootstrap.py" --uninstall
      break
      ;;
    4)
      say "云资源保留至实例到期或手动回收；本次启动的本机预览将在退出时关闭。"
      say "向导仍在运行，可继续查看或回收；Ctrl+C 退出并保留云资源。"
      ;;
    5) pause "可在观察页查看执行；在 Qoder 控制台继续对话，完成后按回车返回。" ;;
  esac
done
if [[ "$REUSED_PREVIEW" -eq 1 && "$CLEANED_THIS_RUN" -eq 1 ]]; then
  say "复用的预览由原终端管理；请在原终端 Ctrl+C 关闭。"
fi
say "本轮运行已回收。配置保存在 config/，运行与验收记录保存在 var/。"
wait_for_interrupt "按 Ctrl+C 结束向导并关闭本次启动的本机预览。" "本轮已完成；Ctrl+C 结束向导。"
}

run_wizard "$@"
