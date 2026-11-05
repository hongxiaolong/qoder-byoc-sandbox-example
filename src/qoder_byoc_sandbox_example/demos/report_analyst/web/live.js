(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const make = (tag, className, text) => {
    const e = document.createElement(tag);
    if (className) e.className = className;
    if (text !== undefined) e.textContent = text;
    return e;
  };
  const resourceNodes = {
    input: "input-node",
    agent: "agent-node",
    skill: "skills-node",
    session: "session-node",
    environment: "environment-node",
    analysis: "analysis-node",
    browser: "browser-node",
    loop: "loop-node",
    queue: "queue-node",
    events: "events-node",
    worker: "worker-node",
    python: "python-node",
  };
  const roles = {
    qoder: "QCA 云端",
    worker: "Worker · Analysis",
    analysis: "Analysis",
    browser: "Browser",
    transfer: "Browser → Analysis",
  };
  const storage = {
    get(key) {
      try {
        return localStorage.getItem(key);
      } catch {
        return null;
      }
    },
    set(key, value) {
      try {
        localStorage.setItem(key, value);
      } catch {}
    },
  };
  let data = {
    status: {},
    session: { turns: [], tools: [] },
    execution: { events: [], runtime: [] },
    queue: {},
    events: [],
    worker: {},
  };
  let selected = "",
    selectedInteraction = "",
    drawnInteraction = "",
    selectedTurn = "",
    selectedResource = "session",
    tab = "io",
    traceView = "interactions",
    selectedSpan = "",
    filter = "all",
    following = true;
  // Default focus, automatic observation and an explicitly pinned conversation
  // are different states. Inspecting an event must not pin future messages.
  let pinnedTurn = false,
    liveInitialized = false,
    liveMessage = "",
    liveSeen = 0,
    liveCurrent = null,
    livePending = [],
    liveTimer;
  let lastSignature = "",
    turnsSignature = "",
    networkError = "",
    detailSignature = "",
    detailKey = "",
    timer,
    refreshing = false,
    rows = [],
    heartbeatDrawn = null;
  let replay = null,
    replayTimer,
    replayGeneration = 0,
    replayLoading = "",
    replayError = "";
  const groupExpansion = new Map(),
    rowExpansion = new Map();
  const timeline = () => (replay ? replay.events : data.events);
  const executions = () =>
    (replay ? replay.execution.events : data.execution.events) || [];
  const now = () => Date.now() / 1000;
  const clock = (value) =>
    Number.isFinite(value)
      ? new Date(value * 1000).toLocaleTimeString("zh-CN", { hour12: false })
      : "未提供";
  const duration = (value) =>
    Number.isFinite(value)
      ? value < 1000
        ? Math.round(value) + "ms"
        : (value / 1000).toFixed(1) + "s"
      : "—";
  const age = (value) =>
    Number.isFinite(value)
      ? Math.max(0, Math.round(now() - value)) + "s 前"
      : "尚无记录";
  const icon = (name) => {
    const img = make("img", "icon");
    img.src = "/assets/icons/" + name + ".svg";
    img.alt = "";
    return img;
  };
  const text = (id, value) => {
    $(id).textContent = value;
  };
  const jsonText = (value) =>
    typeof value === "string" ? value : JSON.stringify(value, null, 2);
  function toast(message) {
    text("toast", message);
    $("toast").hidden = false;
    clearTimeout(timer);
    timer = setTimeout(() => ($("toast").hidden = true), 3000);
  }
  async function copy(value) {
    try {
      await navigator.clipboard.writeText(String(value));
      toast("已复制");
    } catch {
      const input = make("textarea");
      input.value = String(value);
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.append(input);
      input.select();
      const ok = document.execCommand("copy");
      input.remove();
      toast(ok ? "已复制" : "复制不可用，请选择文字手动复制");
    }
  }
  function theme(value) {
    document.documentElement.dataset.theme = value;
    storage.set("qoder-observer-theme", value);
    ["light", "dark"].forEach((t) =>
      $("theme-" + t).setAttribute("aria-pressed", String(t === value)),
    );
    requestAnimationFrame(draw);
  }
  ["light", "dark"].forEach((t) => ($("theme-" + t).onclick = () => theme(t)));
  theme(
    storage.get("qoder-observer-theme") ||
      (matchMedia("(prefers-color-scheme:dark)").matches ? "dark" : "light"),
  );

  function sessionUrl() {
    try {
      const url = new URL(data.status.session_url);
      return !networkError &&
        data.status.state === "ready" &&
        url.protocol === "https:" &&
        url.hostname === "qoder.com" &&
        url.pathname.startsWith("/cloud/sessions/") &&
        !url.search &&
        !url.hash
        ? url.href
        : "";
    } catch {
      return "";
    }
  }
  function region() {
    return /^[a-z]+-[a-z0-9-]+$/.test(data.status.sandbox_region || "")
      ? data.status.sandbox_region
      : "";
  }
  function resource(kind) {
    const s = data.status,
      identity = data.execution.identity || {},
      session = sessionUrl();
    const items = {
      agent: {
        id: s.agent_id,
        name: "Agent",
        description:
          "Agent 声明模型、通用指令、可用工具和 Skills；当前 Session 使用指定版本。",
      },
      skill: {
        id: s.skill_id,
        name: "Skills · report-analysis",
        description:
          "业务操作指引。模型请求 Read 后，Worker 读取 SKILL.md 并把内容返回云端，影响后续工具选择。",
      },
      session: {
        id: s.session_id,
        name: "Session",
        description:
          "承载同一会话的多轮消息、工具请求和结果；发送消息通过 Session Events API。",
      },
      environment: {
        id: s.environment_id,
        name: "Environment · self_hosted",
        description:
          "QCA 的逻辑执行配置。虚线框表示本次案例关联的自托管执行实例，实例由本项目创建。",
      },
      analysis: {
        id: s.analysis_sandbox_id || identity.analysis_sandbox_id,
        name: "Analysis Sandbox",
        description:
          "实际执行实例，包含 Worker 与 Python 工作目录。Work 心跳不会延长 Sandbox 有效期。",
      },
      browser: {
        id: s.browser_sandbox_id || identity.browser_sandbox_id,
        name: "Browser Sandbox",
        description:
          "独立 Chromium 实例，由 Analysis 内的 Worker 通过 CDP 操作；CSV 被读取到 Analysis 工作目录。",
      },
      worker: {
        id:
          identity.worker_id ||
          [...(data.execution.runtime || [])].reverse().find((e) => e.worker_id)
            ?.worker_id,
        name: "Worker",
        description:
          "接管 Session Work、维持租约、读取工具事件并执行、回传结果。Worker 本身不运行第二个 LLM。",
      },
      python: {
        id: s.analysis_sandbox_id,
        name: "Python · /workspace",
        description:
          "在 Analysis Sandbox 执行模型生成的 Python；执行输入、输出和耗时由实际工具记录提供。",
      },
      loop: {
        name: "Managed Agent Loop",
        description:
          "运行在 QCA 云端：调用 LLM，结合消息、配置、Skills 和已返回的工具结果决定下一步。",
      },
      queue: {
        id: data.queue.work?.id,
        name: "QCA Work Queue",
        description:
          "排队的是 Session 工作项。poll、ack、heartbeat 管理领取和租约；公开文档未明确 Work 的精确创建时机。",
      },
      events: {
        id: s.session_id,
        name: "Session Events API",
        description:
          "Worker 使用 GET 读取 agent.tool_use / agent.custom_tool_use；执行后 POST 对应的 user.tool_result / user.custom_tool_result。",
      },
    };
    const item = items[kind] || {};
    if (["analysis", "browser", "python"].includes(kind)) {
      item.url = region()
        ? "https://console.aliyun.com/sandbox/" +
          region() +
          "/sandbox-instances"
        : "https://console.aliyun.com/sandbox/";
      item.urlLabel = "复制 ID 并打开 Sandbox 实例列表";
    } else {
      item.url = session;
      item.urlLabel =
        kind === "session"
          ? "打开 Qoder Session"
          : "打开 Qoder 会话中的关联资源详情";
    }
    return item;
  }
  function idView(host, item) {
    const key = (item.id || "") + "|" + (item.url || "");
    if (host.dataset.key === key) return;
    host.dataset.key = key;
    host.replaceChildren();
    if (!item.id) {
      host.append(make("span", "muted", "ID 尚未记录"));
      return;
    }
    const link = make(item.url ? "a" : "span", "id-text", item.id);
    link.title = (item.urlLabel || "资源 ID") + " · " + item.id;
    if (item.url) {
      link.href = item.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      if (item.url.includes("aliyun.com"))
        link.addEventListener("click", () => copy(item.id));
    }
    host.append(link);
    const button = make("button", "copy");
    button.type = "button";
    button.title = "复制完整 ID";
    button.setAttribute("aria-label", "复制 " + item.name + " ID");
    button.append(icon("copy"));
    button.onclick = (event) => {
      event.stopPropagation();
      copy(item.id);
    };
    host.append(button);
  }
  function renderResources() {
    ["agent", "skill", "session", "environment", "analysis", "browser"].forEach(
      (kind) =>
        idView($(kind === "skill" ? "skill-id" : kind + "-id"), resource(kind)),
    );
    idView($("input-session"), resource("session"));
    const work = data.queue.work,
      runtime = data.execution.runtime || [];
    const workId =
      work?.id || [...runtime].reverse().find((e) => e.work_id)?.work_id;
    idView($("work-id"), {
      id: workId,
      name: "Work",
      url: sessionUrl(),
      urlLabel: "打开关联 Qoder Session",
    });
    const link = $("session-link"),
      url = sessionUrl();
    if (url && data.status.state !== "expired") {
      link.href = url;
      link.setAttribute("aria-disabled", "false");
    } else {
      link.removeAttribute("href");
      link.setAttribute("aria-disabled", "true");
    }
    text(
      "agent-version",
      data.status.agent_version
        ? "使用 Agent v" + data.status.agent_version
        : "使用 Agent · 版本未记录",
    );
    const queue = data.queue,
      stale = queue.stale || queue.state !== "ready";
    [
      ["queue-depth", "depth"],
      ["queue-pending", "pending"],
      ["queue-pollers", "workers_polling"],
    ].forEach(([id, key]) => {
      const value = queue.stats?.[key];
      text(id, Number.isInteger(value) ? String(value) : "—");
      $(id).classList.toggle("unknown", stale);
    });
    text(
      "queue-time",
      queue.observed_at
        ? (stale ? "最后已观察 · " : "采样于 ") + clock(queue.observed_at)
        : "尚未取得真实队列统计",
    );
    text(
      "lease-status",
      work
        ? "租约 " +
            work.state +
            " · " +
            (data.worker.heartbeat_at
              ? "心跳 " + age(data.worker.heartbeat_at)
              : "无心跳记录")
        : "租约状态待确认",
    );
    $("lease-status").classList.toggle("muted", !work || stale);
    const health = {
      healthy: "心跳 " + age(data.worker.heartbeat_at),
      expired: "租约已超时",
      stopped: "Worker 已停止",
      unknown: "状态待确认",
    };
    text("worker-status", health[data.worker.state] || "等待心跳");
    const active = data.session.active_message_id;
    text(
      "loop-status",
      data.session.stale
        ? "事件同步中断，保留已观察记录"
        : active
          ? "当前 Session 正在处理消息"
          : "等待会话输入",
    );
    const running = (data.execution.events || []).find(
      (e) => e.state === "running" && e.tool === "download_report",
    );
    text(
      "browser-status",
      running && data.worker.state === "healthy"
        ? "浏览器工具执行中"
        : "远程浏览器工具",
    );
  }
  function pauseReplay() {
    clearTimeout(replayTimer);
    if (replay) replay.playing = false;
  }
  function stopLiveFlow() {
    clearTimeout(liveTimer);
    liveTimer = null;
    livePending = [];
  }
  function liveDelay() {
    return livePending.length > 8 ? 180 : 450;
  }
  function advanceLiveFlow() {
    liveTimer = null;
    if (!following || pinnedTurn || replay) return;
    const step = livePending.shift();
    if (!step) return;
    liveCurrent = step;
    selectedResource = "";
    selectedTurn = "";
    selectedSpan = "";
    const event = step.event_ids
      .map((id) => data.events.find((e) => e.id === id))
      .find(Boolean);
    selected = event?.id || "";
    selectedInteraction = step.id;
    render();
    // Keep the current handoff visible even if another poll arrives immediately.
    liveTimer = setTimeout(advanceLiveFlow, liveDelay());
  }
  function syncLiveFlow() {
    const message =
      data.session.turns?.at(-1)?.id || data.session.active_message_id || "";
    const steps =
      data.interactions?.conversations?.find((c) => c.message_id === message)
        ?.steps || [];
    if (!liveInitialized) {
      liveInitialized = true;
      liveMessage = message;
      liveSeen = Math.max(0, ...steps.map((step) => step.number));
      return; // Opening the page focuses Session; existing history is not replayed.
    }
    if (message !== liveMessage) {
      // A new user message takes over replay, including paused playback and
      // an in-flight history fetch. Invalidate late history responses as well.
      if (replay || replayLoading) endReplay(true);
      stopLiveFlow();
      liveMessage = message;
      liveSeen = 0;
      liveCurrent = null;
      if (!pinnedTurn && !replay && !replayLoading) {
        following = true;
        selectedTurn = "";
        selected = selectedInteraction = "";
        selectedResource = "session";
        $("turns").scrollTop = $("turns").scrollHeight;
      }
    }
    const added = steps.filter((step) => step.number > liveSeen);
    liveSeen = Math.max(liveSeen, ...steps.map((step) => step.number));
    if (!following || pinnedTurn || replay || replayLoading) return;
    livePending.push(...added);
    if (!liveTimer && livePending.length) advanceLiveFlow();
  }
  function followLatest() {
    endReplay(true);
    stopLiveFlow();
    pinnedTurn = false;
    following = true;
    liveMessage =
      data.session.turns?.at(-1)?.id || data.session.active_message_id || "";
    selectedTurn = "";
    const steps = conversation(liveMessage)?.steps || [];
    liveCurrent = steps.at(-1) || null;
    liveSeen = Math.max(0, ...steps.map((step) => step.number));
    selectedResource = liveCurrent ? "" : "session";
    selected = selectedInteraction = "";
    lastSignature = "";
    render();
  }
  function endReplay(clearScope = false) {
    pauseReplay();
    replay = null;
    selectedInteraction = "";
    replayLoading = "";
    replayError = "";
    replayGeneration++;
    if (clearScope) {
      selectedTurn = "";
      pinnedTurn = false;
    }
    turnsSignature = lastSignature = "";
  }
  function advanceReplay() {
    if (!replay || !replay.playing) return;
    if (replay.cursor >= replay.frames.length - 1) {
      replay.playing = false;
      render();
      return;
    }
    replay.cursor++;
    selected = replay.frames[replay.cursor].id;
    selectedInteraction = replay.steps[replay.cursor].id;
    selectedResource = "";
    render();
    replayTimer = setTimeout(advanceReplay, 850);
  }
  async function startReplay(turn) {
    stopLiveFlow();
    endReplay();
    const generation = replayGeneration;
    replayLoading = turn.id;
    selectedTurn = turn.id;
    pinnedTurn = true;
    following = false;
    selectedResource = "";
    filter = "all";
    $("event-search").value = "";
    document
      .querySelectorAll("[data-filter]")
      .forEach((e) =>
        e.setAttribute("aria-pressed", String(e.dataset.filter === "all")),
      );
    render();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(
        "/api/observer?message_id=" + encodeURIComponent(turn.id),
        { cache: "no-store", signal: controller.signal },
      );
      if (!response.ok) throw new Error();
      const history = await response.json();
      if (
        history.message_id !== turn.id ||
        !Array.isArray(history.events) ||
        !history.events.length
      )
        throw new Error();
      if (generation !== replayGeneration) return;
      const entry = history.interactions?.conversations?.find(
        (item) => item.message_id === turn.id,
      );
      const records = new Map(history.events.map((event) => [event.id, event]));
      const steps = (entry?.steps || []).filter((step) =>
        step.event_ids.some((id) => records.has(id)),
      );
      const frames = steps
        .map((step) => {
          const record = step.event_ids
            .map((id) => records.get(id))
            .find(Boolean);
          return (
            record && { ...record, interaction_id: step.id, label: step.label }
          );
        })
        .filter(Boolean);
      if (history.interactions?.version !== 4 || !frames.length)
        throw new Error("Task flow history unavailable");
      replay = {
        interactions: history.interactions,
        steps,
        frames,
        messageId: turn.id,
        events: history.events,
        execution: history.execution || { events: [] },
        session: history.session || { trace: [] },
        tracing: history.tracing,
        api_trace: history.api_trace,
        cursor: 0,
        playing: true,
        incomplete: history.history_incomplete,
      };
      selected = replay.frames[0].id;
      selectedInteraction = replay.steps[0].id;
      replayTimer = setTimeout(advanceReplay, 850);
    } catch {
      if (generation === replayGeneration)
        replayError =
          "历史记录暂不可用，请重试；若刚更新了代码，请重启本机 preview。不会重新发送任务。";
    } finally {
      clearTimeout(timeout);
      if (generation === replayGeneration) {
        replayLoading = "";
        turnsSignature = "";
        render();
      }
    }
  }
  function conversations() {
    const catalog = replay ? replay.interactions : data.interactions;
    return catalog?.version === 4 ? catalog.conversations || [] : [];
  }
  function conversation(messageId) {
    return conversations().find((item) => item.message_id === messageId);
  }
  function stepsForEvent(event) {
    if (!event) return [];
    return conversations().flatMap((entry) =>
      entry.steps
        .filter((step) => step.event_ids.includes(event.id))
        .map((step) => ({ ...step, conversation: entry })),
    );
  }
  function stepForEvent(event) {
    const steps = stepsForEvent(event);
    const explicit =
      event?.interaction_id ||
      (event?.id === selected ? selectedInteraction : "");
    return steps.find((step) => step.id === explicit) || steps.at(-1) || null;
  }
  function eventProgress(event) {
    const step = stepForEvent(event);
    if (!step) return null;
    return {
      messageId: step.conversation.message_id,
      index: step.number,
      total: step.conversation.total_steps,
      partial: step.conversation.partial,
      label: step.conversation.partial ? "交互（部分记录）" : "交互",
    };
  }
  function flowLabel(events) {
    const numbers = [
      ...new Set(
        events.flatMap((event) =>
          stepsForEvent(event).map((step) => step.number),
        ),
      ),
    ].sort((a, b) => a - b);
    return numbers.length
      ? numbers.length > 1
        ? numbers.length + " 次交互"
        : "交互 " + numbers[0]
      : events.some((event) =>
            ["heartbeat", "work", "resource"].includes(event.category),
          )
        ? "后台 / 准备"
        : events.some((event) =>
              ["tool", "model", "session"].includes(event.category),
            )
          ? "组件内部"
          : "诊断记录";
  }
  function latestInputEvent(events) {
    const messageId =
      selectedTurn ||
      (following && liveMessage) ||
      data.session.active_message_id ||
      data.session.turns?.at(-1)?.id;
    let steps = conversation(messageId)?.steps || [];
    if (!replay && following && liveCurrent && messageId === liveMessage)
      steps = steps.filter((step) => step.number <= liveCurrent.number);
    const visible = new Map(events.map((event) => [event.id, event]));
    for (const step of [...steps].reverse())
      for (const id of [...step.event_ids].reverse())
        if (visible.has(id))
          return { ...visible.get(id), interaction_id: step.id };
    return events.at(-1);
  }
  function replayFuture(event) {
    if (!replay) return false;
    const candidates = stepsForEvent(event);
    // A legacy completion can prove both invocation and return. Its output is
    // visible at the return boundary, never at the earlier inferred invocation.
    if (event.kind === "tool_execution" && event.tool_id) {
      const returned = replay.steps.findIndex(
        (step) =>
          step.tool_id === event.tool_id && step.flow === "python-result",
      );
      if (returned >= 0) return returned > replay.cursor;
      const posted = replay.steps.findIndex(
        (step) => step.tool_id === event.tool_id && step.flow === "result",
      );
      return posted < 0 || posted > replay.cursor;
    }
    if (candidates.length)
      return candidates.every(
        (step) =>
          replay.steps.findIndex((item) => item.id === step.id) > replay.cursor,
      );
    const anchor = replay.events.findIndex(
      (item) => item.id === replay.frames[replay.cursor]?.id,
    );
    return replay.events.indexOf(event) > anchor;
  }
  function replayStepIndex() {
    const step = stepForEvent(replay?.frames[replay.cursor]);
    return step ? replay.steps.findIndex((item) => item.id === step.id) : -1;
  }
  function seekReplayStep(delta) {
    const next = replay.steps[replayStepIndex() + delta];
    if (!next) return;
    pauseReplay();
    replay.cursor = replay.steps.indexOf(next);
    selected = replay.frames[replay.cursor].id;
    selectedInteraction = replay.steps[replay.cursor].id;
    selectedResource = "";
    render();
  }
  function renderTurns() {
    const turns = data.session.turns || [],
      progress =
        !replay && !selectedResource
          ? eventProgress(data.events.find((event) => event.id === selected))
          : null,
      signature = JSON.stringify([
        progress,
        turns.map((t) => [t.id, t.number, t.text, t.source, t.state]),
        selectedTurn,
        replay && [replay.messageId, replay.cursor, replay.playing],
        replayLoading,
        replayError,
        filter,
        $("event-search").value,
      ]);
    if (signature === turnsSignature) return;
    turnsSignature = signature;
    const host = $("turns"),
      scroll = host.scrollTop;
    const focused = document.activeElement?.dataset.focusKey;
    const atBottom = host.scrollHeight - host.clientHeight - scroll < 30;
    host.replaceChildren();
    if (!turns.length) {
      host.append(
        make(
          "p",
          "empty",
          "在 start.sh 或 Qoder 控制台发送消息，输入记录将在这里出现。",
        ),
      );
      return;
    }
    for (const turn of turns) {
      const card = make(
        "article",
        "turn" +
          ((selectedTurn || (following && liveMessage)) === turn.id
            ? " selected"
            : ""),
      );
      card.dataset.turn = turn.id;
      const button = make("button", "turn-select");
      button.type = "button";
      button.dataset.focusKey = turn.id + ":select";
      const meta = make("div", "turn-meta");
      meta.append(
        make(
          "time",
          "",
          (turn.number ? "对话 " + turn.number + " · " : "对话 · ") +
            clock(turn.time || turn.observed_at),
        ),
        make(
          "span",
          "turn-source",
          turn.source === "terminal"
            ? "终端"
            : turn.source === "start.sh"
              ? "start.sh"
              : "Qoder 控制台",
        ),
      );
      button.append(meta, make("p", "", turn.text));
      card.onclick = () => {
        stopLiveFlow();
        endReplay();
        selectedTurn = turn.id;
        pinnedTurn = true;
        selectedResource = "";
        following = false;
        selected = "";
        render();
      };
      const footer = make("div", "turn-controls");
      const playing = replay?.messageId === turn.id;
      const replayButton = make(
        "button",
        "replay-button",
        playing
          ? replay.playing
            ? "暂停回放"
            : replay.cursor === replay.frames.length - 1
              ? "重新回放"
              : "继续回放"
          : replayLoading === turn.id
            ? "读取历史…"
            : "回放轨迹",
      );
      replayButton.type = "button";
      replayButton.dataset.focusKey = turn.id + ":replay";
      replayButton.disabled =
        !!replayLoading || ["running", "waiting_tools"].includes(turn.state);
      replayButton.title = replayButton.disabled
        ? "等待本轮结束后回放；回放不会重新执行任务。"
        : "只重现已记录事件，不会提交消息或调用工具。";
      replayButton.onclick = (event) => {
        event.stopPropagation();
        if (!playing) {
          startReplay(turn);
          return;
        }
        if (replay.playing) pauseReplay();
        else {
          if (replay.cursor === replay.frames.length - 1) replay.cursor = 0;
          replay.playing = true;
          selected = replay.frames[replay.cursor].id;
          selectedInteraction = replay.steps[replay.cursor].id;
          replayTimer = setTimeout(advanceReplay, 850);
        }
        render();
      };
      footer.append(
        make(
          "span",
          "turn-state",
          turn.state === "completed"
            ? "已完成"
            : turn.state === "waiting_tools"
              ? "工具执行中"
              : turn.state === "running"
                ? "处理中"
                : "已记录",
        ),
        replayButton,
      );
      if (progress?.messageId === turn.id) {
        const summary = make(
          "span",
          "live-progress",
          progress.label +
            " " +
            progress.index +
            " · 已发生 " +
            progress.total +
            " 次交互",
        );
        summary.title =
          "仅统计组件之间的实际传递；组件内部状态、模型计算与后台心跳不增加编号";
        footer.append(summary);
      }
      if (playing) {
        const progress = make("div", "replay-controls");
        const previous = make("button", "", "上一步"),
          next = make("button", "", "下一步"),
          live = make("button", "", "返回实时");
        previous.dataset.focusKey = turn.id + ":previous";
        next.dataset.focusKey = turn.id + ":next";
        live.dataset.focusKey = turn.id + ":live";
        previous.disabled = replayStepIndex() <= 0;
        next.disabled = replayStepIndex() === replay.steps.length - 1;
        for (const [control, delta] of [
          [previous, -1],
          [next, 1],
        ])
          control.onclick = (event) => {
            event.stopPropagation();
            seekReplayStep(delta);
          };
        live.onclick = (event) => {
          event.stopPropagation();
          followLatest();
        };
        progress.append(
          make(
            "span",
            "",
            (replay.incomplete ? "部分历史 · " : "") +
              "交互 " +
              (eventProgress(replay.frames[replay.cursor])?.index || "—") +
              " / " +
              (conversation(replay.messageId)?.total_steps || "—") +
              " · " +
              replay.frames[replay.cursor].label +
              (filteredEvents().some((event) => event.id === selected)
                ? ""
                : " · 当前步骤被筛选隐藏"),
          ),
          previous,
          next,
          live,
        );
        footer.append(progress);
      }
      if (replayError && selectedTurn === turn.id) {
        const error = make("p", "turn-error", replayError);
        error.setAttribute("role", "alert");
        footer.append(error);
      }
      card.append(button, footer);
      host.append(card);
    }
    host.scrollTop = replay ? scroll : atBottom ? host.scrollHeight : scroll;
    if (focused) {
      const controls = [...host.querySelectorAll("[data-focus-key]")];
      const target = controls.find(
        (element) => element.dataset.focusKey === focused,
      );
      const fallbackKey =
        focused.slice(0, focused.lastIndexOf(":")) + ":replay";
      const fallback =
        controls.find((element) => element.dataset.focusKey === fallbackKey) ||
        $("follow-latest");
      (target && !target.disabled ? target : fallback)?.focus({
        preventScroll: true,
      });
    }
  }
  const traceModel = () => (replay ? replay.tracing : data.tracing);
  const apiRecords = () =>
    (replay ? replay.api_trace : data.api_trace)?.requests || [];
  const operationRows = () =>
    (traceModel()?.groups || []).flatMap((group) => group.rows);
  function currentOperation() {
    if (traceView === "events") return null;
    const candidates = operationRows().filter((row) =>
      row.event_ids.includes(selected),
    );
    return (
      candidates.find((row) => row.id === selectedSpan) ||
      candidates.find((row) => row.parent_id) ||
      candidates[0] ||
      null
    );
  }
  function recordedRequest(event) {
    const id =
      event?.api_request_id ||
      (event?.id.startsWith("api:") ? event.id.slice(4) : null);
    return id ? apiRecords().find((record) => record.id === id) : null;
  }
  function timingLabel(row) {
    if (row.future) return "尚未发生";
    if (row.type === "event") return "◇ 时间点";
    if (row.timing_state === "measured" && Number.isFinite(row.elapsed_ms))
      return duration(row.elapsed_ms);
    return (
      { running: "执行中", incomplete: "记录不完整", uncollected: "未采集" }[
        row.timing_state
      ] || "未采集"
    );
  }
  function failed(row) {
    return /error|fail|denied|cancelled/.test(row.state || "");
  }
  function displayedOperation(original) {
    if (!replay || !original.event_ids.length) return original;
    const past = new Set(
      replay.events
        .filter((event) => !replayFuture(event))
        .map((event) => event.id),
    );
    const phases = replay.events.filter(
      (event) => original.event_ids.includes(event.id) && past.has(event.id),
    );
    const row = { ...original };
    row.future = !phases.length;
    if (row.future) {
      row.state = "future";
      row.timing_state = "uncollected";
      delete row.started_at;
      delete row.finished_at;
      delete row.elapsed_ms;
      return row;
    }
    if (
      row.type === "span" &&
      phases.length &&
      phases.length < original.event_ids.length
    ) {
      const final = phases.at(-1);
      row.state = final.state;
      if (
        !phases.some(
          (event) =>
            ["tool_execution", "http.request"].includes(event.kind) ||
            /model_request_end$/.test(event.kind),
        )
      ) {
        row.timing_state =
          final.state === "running" || /model_request_start$/.test(final.kind)
            ? "running"
            : "uncollected";
        delete row.finished_at;
        delete row.elapsed_ms;
      }
    }
    return row;
  }
  function renderEvents() {
    if (traceView === "interactions") {
      renderInteractions();
      return;
    }
    if (traceView === "events" || !traceModel()?.groups) {
      setTraceColumns(false);
      renderRawEvents();
      return;
    }
    setTraceColumns(true);
    rows = filteredEvents();
    const allowed = new Set(rows.map((event) => event.id));
    const groups = traceModel()
      .groups.map((group) => ({
        ...group,
        rows: group.rows.filter((row) =>
          row.event_ids.some((id) => allowed.has(id)),
        ),
      }))
      .filter((group) => group.rows.length);
    const active =
      selectedTurn ||
      (following && liveMessage) ||
      data.session.active_message_id ||
      groups.filter((group) => group.scope === "turn").at(-1)?.message_id;
    if (following && !selectedResource) {
      const group =
        groups.find((item) => item.message_id === active) ||
        groups.find((item) => item.scope === "setup") ||
        groups.at(-1);
      const ids = new Set(group?.rows.flatMap((row) => row.event_ids) || []);
      const latest = latestInputEvent(
        rows.filter((event) => ids.has(event.id)),
      );
      selected = latest?.id || "";
      selectedInteraction = latest?.interaction_id || "";
      const matches =
        group?.rows.filter((row) => row.event_ids.includes(selected)) || [];
      selectedSpan =
        (matches.find((row) => row.parent_id) || matches[0])?.id || "";
    } else if (!rows.some((event) => event.id === selected) && !replay) {
      selected = "";
      selectedSpan = "";
    }
    const op = currentOperation();
    const signature = JSON.stringify([
      groups,
      selected,
      selectedResource,
      selectedInteraction,
      replay?.cursor,
      following,
      [...groupExpansion],
      [...rowExpansion],
    ]);
    if (signature === lastSignature) return;
    lastSignature = signature;
    const body = $("event-rows"),
      scroll = $("event-list").scrollTop;
    body.replaceChildren();
    const visible = [];
    for (const group of groups) {
      const failure = group.rows.some((row) => failed(displayedOperation(row)));
      const selectedHere = group.rows.some((row) =>
        row.event_ids.includes(selected),
      );
      const open = groupExpansion.has(group.id)
        ? groupExpansion.get(group.id)
        : selectedHere ||
          failure ||
          (group.scope === "turn" && group.message_id === active) ||
          (group.scope === "setup" && !active) ||
          (filter === "resource" && group.scope === "setup") ||
          (["work", "heartbeat", "http"].includes(filter) &&
            group.scope === "background");
      const header = make("tr", "trace-group" + (failure ? " has-error" : "")),
        cell = make("td");
      cell.colSpan = 5;
      const toggle = make("button", "group-toggle");
      const turn = (data.session.turns || []).find(
        (turn) => turn.id === group.message_id,
      );
      toggle.textContent =
        (open ? "▾ " : "▸ ") +
        (group.scope === "turn"
          ? "对话 " +
            (conversation(group.message_id)?.number || turn?.number || "") +
            " · " +
            (turn?.text || group.message_id).slice(0, 48)
          : group.label) +
        "  ·  " +
        (group.scope === "turn" && conversation(group.message_id)
          ? conversation(group.message_id).total_steps + " 次交互"
          : group.rows.length + " 条记录");
      toggle.title = group.message_id || group.label;
      toggle.setAttribute("aria-expanded", String(open));
      toggle.onclick = () => {
        groupExpansion.set(group.id, !open);
        lastSignature = "";
        render();
      };
      cell.append(toggle);
      header.append(cell);
      body.append(header);
      if (!open) continue;
      const ids = new Set(group.rows.map((row) => row.id)),
        visited = new Set();
      const stamps = group.rows
        .flatMap((row) => [row.started_at, row.finished_at])
        .filter(Number.isFinite);
      const start = Math.min(...stamps),
        end = Math.max(...stamps),
        extent = Math.max(0.001, end - start);
      const append = (original, depth = 0) => {
        if (visited.has(original.id) || depth > 10) return;
        visited.add(original.id);
        const row = displayedOperation(original);
        const selectedRow = !selectedResource && op?.id === row.id;
        const element = make(
          "tr",
          (selectedRow ? "selected " : "") +
            (row.future ? "future " : "") +
            "operation-row",
        );
        element.tabIndex = 0;
        element.setAttribute("role", "button");
        element.setAttribute("aria-selected", String(selectedRow));
        element.dataset.operation = row.id;
        element.dataset.event = row.event_ids.at(-1) || "";
        element.setAttribute(
          "aria-label",
          row.label + "，" + (roles[row.role] || row.role),
        );
        const children = group.rows.filter((item) => item.parent_id === row.id);
        const childSelected = children.some((child) =>
          child.event_ids.includes(selected),
        );
        const expanded = rowExpansion.get(row.id) ?? childSelected;
        const name = make("td", "operation-name"),
          line = make("div", "operation-label");
        line.style.paddingLeft = depth * 16 + "px";
        if (children.length) {
          const button = make("button", "row-toggle", expanded ? "▾" : "▸");
          button.setAttribute(
            "aria-label",
            (expanded ? "收起" : "展开") + row.label + "的阶段",
          );
          button.setAttribute("aria-expanded", String(!!expanded));
          button.onclick = (event) => {
            event.stopPropagation();
            rowExpansion.set(row.id, !expanded);
            lastSignature = "";
            render();
          };
          line.append(button);
        } else line.append(make("span", "row-toggle-spacer"));
        line.append(
          make("i", "event-state " + (failed(row) ? "error" : row.state)),
          make("span", "operation-text", row.label),
        );
        const stepLabel = flowLabel(
          timeline().filter((event) => row.event_ids.includes(event.id)),
        );
        line.append(make("span", "flow-step-label", stepLabel));
        name.append(line);
        name.title = row.label + " · " + stepLabel;
        const state = make(
          "td",
          "operation-state",
          {
            future: "尚未发生",
            completed: "完成",
            success: "完成",
            posted: "已回传",
            running: "执行中",
            observed: "已记录",
            unknown: "待确认",
            error: "失败",
            denied: "拒绝",
            cancelled: "已取消",
          }[row.state] ||
            row.state ||
            "已记录",
        );
        if (failed(row)) state.classList.add("error");
        const timelineCell = make("td", "timeline-cell"),
          track = make("div", "trace-track");
        track.title = "记录时间轴；不同执行端时钟不可用于计算排队耗时";
        if (Number.isFinite(row.started_at)) {
          const bar = make(
            "span",
            row.type === "event"
              ? "trace-point"
              : "trace-bar" +
                  (row.timing_state === "running" ? " running" : "") +
                  (failed(row) ? " error" : ""),
          );
          bar.style.left =
            Math.max(
              0,
              Math.min(99, ((row.started_at - start) / extent) * 100),
            ) + "%";
          if (row.type === "span")
            bar.style.width =
              Math.max(
                1,
                Math.min(
                  100,
                  ((Number.isFinite(row.elapsed_ms)
                    ? row.elapsed_ms / 1000
                    : Math.max(
                        0,
                        (row.finished_at || row.started_at) - row.started_at,
                      )) /
                    extent) *
                    100,
                ),
              ) + "%";
          track.append(bar);
        }
        timelineCell.append(track);
        element.append(
          name,
          make("td", "", roles[row.role] || row.role),
          state,
          timelineCell,
          make("td", "", timingLabel(row)),
        );
        const choose = () => {
          const canonical = row.event_ids
            .filter((id) => timeline().some((e) => e.id === id))
            .at(-1);
          if (canonical) selectEvent(canonical, false, row.id);
        };
        element.onclick = choose;
        element.onkeydown = (event) => {
          if (["Enter", " "].includes(event.key)) {
            event.preventDefault();
            choose();
          }
          if (["ArrowDown", "ArrowUp"].includes(event.key)) {
            event.preventDefault();
            const index =
              visible.findIndex((item) => item.id === row.id) +
              (event.key === "ArrowDown" ? 1 : -1);
            const next = visible[index];
            if (next) {
              selectEvent(next.event_ids.at(-1), false, next.id);
              $("event-rows")
                .querySelector(
                  '[data-operation="' +
                    (globalThis.CSS?.escape
                      ? CSS.escape(next.id)
                      : next.id.replace(/[^a-zA-Z0-9_-]/g, "\\$&")) +
                    '"]',
                )
                ?.focus();
            }
          }
        };
        body.append(element);
        visible.push(original);
        if (expanded) children.forEach((child) => append(child, depth + 1));
      };
      group.rows
        .filter((row) => !row.parent_id || !ids.has(row.parent_id))
        .forEach((row) => append(row));
    }
    $("event-list").scrollTop = following
      ? $("event-list").scrollHeight
      : scroll;
    $("events-empty").hidden = !!groups.length;
    text(
      "event-count",
      groups.reduce(
        (count, group) =>
          count + group.rows.filter((row) => row.type === "span").length,
        0,
      ) +
        " 操作 · " +
        rows.length +
        " 记录",
    );
    $("trace-scope").value = selectedTurn ? "input" : "session";
    $("trace-scope").querySelector('option[value="input"]').disabled =
      !selectedTurn;
    $("follow-latest").setAttribute("aria-pressed", String(following));
    $("history-note").hidden = !(replay
      ? replay.incomplete
      : data.history_incomplete);
    if (replay)
      body.querySelector(".selected")?.scrollIntoView?.({ block: "nearest" });
  }
  function renderInteractions() {
    setTraceColumns(true);
    ["交互", "发送 → 接收", "关联工具", "记录时间", "状态"].forEach(
      (label, i) =>
        (document.querySelectorAll("#event-list th")[i].textContent = label),
    );
    rows = filteredEvents();
    const visible = new Map(rows.map((event) => [event.id, event]));
    const entries = conversations().flatMap((entry) =>
      entry.steps
        .filter((step) => step.event_ids.some((id) => visible.has(id)))
        .map((step) => ({ ...step, conversation: entry })),
    );
    if (following && !selectedResource) {
      const latest = latestInputEvent(rows);
      selected = latest?.id || "";
      selectedInteraction = latest?.interaction_id || "";
    } else if (!replay && !visible.has(selected)) {
      selected = selectedInteraction = "";
    }
    const signature = JSON.stringify([
      "interactions",
      entries,
      selectedInteraction,
      selectedResource,
      replay?.cursor,
      following,
      rows.map((event) => [event.id, event.state]),
    ]);
    if (signature === lastSignature) return;
    lastSignature = signature;
    const body = $("event-rows"),
      scroll = $("event-list").scrollTop;
    body.replaceChildren();
    const names = {
      input: "会话输入",
      session: "Session",
      loop: "Agent Loop",
      events: "Events API",
      worker: "Worker",
      python: "Python",
      browser: "Browser",
    };
    for (const step of entries) {
      const event = step.event_ids.map((id) => visible.get(id)).find(Boolean);
      const future =
        replay &&
        replay.steps.findIndex((item) => item.id === step.id) > replay.cursor;
      const active =
        !selectedResource &&
        selectedInteraction === step.id &&
        step.event_ids.includes(selected);
      const row = make(
        "tr",
        (active ? "selected " : "") + (future ? "future" : ""),
      );
      row.dataset.event = event.id;
      row.dataset.interaction = step.id;
      row.tabIndex = 0;
      row.setAttribute("role", "button");
      row.setAttribute("aria-selected", String(active));
      const label =
        "对话 " + step.conversation.number + " · 交互 " + step.number;
      row.setAttribute("aria-label", label + " · " + step.label);
      row.append(
        make(
          "td",
          "operation-name",
          label +
            " · " +
            (step.label || step.source + " → " + step.target)
              .split("·")
              .at(-1)
              .trim(),
        ),
        make(
          "td",
          "",
          (names[step.source] || step.source) +
            " → " +
            (names[step.target] || step.target),
        ),
        make(
          "td",
          "",
          step.tool_ids?.length > 1
            ? step.tool_ids.length + " 项工具任务"
            : step.tool_id || "—",
        ),
        make(
          "td",
          "",
          Number.isFinite(event.time) && !future ? clock(event.time) : "—",
        ),
        make("td", "", future ? "尚未发生" : "已记录"),
      );
      row.title = step.label;
      row.children[2].title =
        step.tool_ids?.join("\n") || step.tool_id || "没有关联工具";
      const choose = () => selectEvent(event.id, false, "", step.id);
      row.onclick = choose;
      row.onkeydown = (key) => {
        if (["Enter", " "].includes(key.key)) {
          key.preventDefault();
          choose();
        } else if (["ArrowUp", "ArrowDown"].includes(key.key)) {
          key.preventDefault();
          const next =
            entries[entries.indexOf(step) + (key.key === "ArrowDown" ? 1 : -1)];
          const fact = next?.event_ids
            .map((id) => visible.get(id))
            .find(Boolean);
          if (fact) selectEvent(fact.id, true, "", next.id);
        }
      };
      body.append(row);
    }
    $("event-list").scrollTop = following
      ? $("event-list").scrollHeight
      : scroll;
    $("events-empty").hidden = entries.length > 0;
    text(
      "events-empty",
      "当前没有组件交互；内部状态和后台活动可切换执行视图查看。",
    );
    text("event-count", entries.length + " 次交互");
    $("trace-scope").value = selectedTurn ? "input" : "session";
    $("trace-scope").querySelector('option[value="input"]').disabled =
      !selectedTurn;
    $("follow-latest").setAttribute("aria-pressed", String(following));
    $("history-note").hidden = !(replay
      ? replay.incomplete
      : data.history_incomplete);
    if (replay)
      body.querySelector(".selected")?.scrollIntoView?.({ block: "nearest" });
  }
  function setTraceColumns(operations) {
    const labels = operations
      ? ["操作 / 阶段", "执行位置", "状态", "记录时间线", "耗时"]
      : ["时间", "原始事件", "关联 API", "执行位置", "耗时"];
    document
      .querySelectorAll("#event-list th")
      .forEach((heading, index) => (heading.textContent = labels[index]));
    $("event-list").classList.toggle("operation-table", operations);
    $("event-list").classList.toggle(
      "interaction-table",
      traceView === "interactions",
    );
  }
  function filteredEvents() {
    const search = $("event-search").value.trim().toLocaleLowerCase();
    const selectedTools = new Set(
      (data.session.tools || [])
        .filter((e) => e.message_id === selectedTurn)
        .map((e) => e.id),
    );
    let events = (timeline() || []).filter(
      (e) =>
        (!selectedTurn ||
          replay ||
          e.message_id === selectedTurn ||
          selectedTools.has(e.tool_id) ||
          ["resource", "work", "heartbeat"].includes(e.category)) &&
        (filter === "all" || e.category === filter),
    );
    if (search)
      events = events.filter((e) =>
        [
          e.kind,
          e.label,
          roles[e.role],
          e.tool_id,
          e.input,
          e.output,
          protocolFor(e)?.path,
          protocolFor(e)?.method,
        ]
          .join(" ")
          .toLocaleLowerCase()
          .includes(search),
      );
    return events;
  }
  function selectEvent(
    identifier,
    focus = false,
    operationId = "",
    interactionId = "",
  ) {
    stopLiveFlow();
    pauseReplay();
    const event = timeline().find((item) => item.id === identifier);
    // Raw records may prove consecutive handoffs (delivery, then Python call).
    // Manual inspection chooses the most specific boundary; playback carries
    // an explicit interaction ID so those two steps never collapse together.
    selectedInteraction =
      interactionId || stepsForEvent(event).at(-1)?.id || "";
    if (replay && selectedInteraction) {
      const index = replay.steps.findIndex(
        (step) => step.id === selectedInteraction,
      );
      if (index >= 0) replay.cursor = index;
    }
    selected = identifier;
    selectedSpan = operationId;
    selectedResource = "";
    following = false;
    lastSignature = "";
    render();
    if (focus) $("event-rows").querySelector(".selected")?.focus();
  }
  function renderRawEvents() {
    rows = filteredEvents();
    const keep = rows.find((e) => e.id === selected);
    if (following && !selectedResource) {
      const latest = latestInputEvent(rows);
      selected = latest?.id || "";
      selectedInteraction = latest?.interaction_id || "";
    } else if (!keep && !replay) selected = "";
    const signature = JSON.stringify([
      rows.map((e) => [
        e.id,
        e.kind,
        e.state,
        e.elapsed_ms,
        e.label,
        protocolFor(e),
      ]),
      selected,
      selectedInteraction,
      following,
      replay?.cursor,
    ]);
    if (signature === lastSignature) return;
    lastSignature = signature;
    const body = $("event-rows"),
      scroll = $("event-list").scrollTop;
    body.replaceChildren();
    for (const event of rows) {
      const future = replayFuture(event);
      const row = make(
        "tr",
        (event.id === selected ? "selected" : "") + (future ? " future" : ""),
      );
      row.tabIndex = 0;
      row.setAttribute("role", "button");
      row.setAttribute(
        "aria-label",
        event.label + "，" + (roles[event.role] || event.role),
      );
      row.setAttribute("aria-selected", String(event.id === selected));
      row.dataset.event = event.id;
      const when = make(
        "td",
        "",
        Number.isFinite(event.time) ? clock(event.time) : "—",
      );
      when.title = Number.isFinite(event.time)
        ? "事件提供的时间"
        : "未提供事件时间；采样时刻见 Timing";
      const label = make("td");
      const call = executions().find((item) => item.id === event.tool_id);
      const name = event.kind + (call?.tool ? " · " + call.tool : "");
      label.append(
        make("i", "event-state " + (future ? "future" : event.state)),
        document.createTextNode(name),
      );
      label.append(make("span", "flow-step-label", flowLabel([event])));
      label.title = event.label;
      const api = protocolFor(event),
        endpoint = make("td", "api-cell");
      endpoint.textContent = api ? api.method + " " + api.template : "—";
      endpoint.title = api
        ? api.method + " " + api.path
        : "本地记录；没有对应的 HTTP 请求";
      row.append(
        when,
        label,
        endpoint,
        make("td", "", roles[event.role] || event.role),
        make(
          "td",
          "",
          future
            ? "尚未发生"
            : Number.isFinite(event.elapsed_ms)
              ? duration(event.elapsed_ms)
              : event.kind === "tool_execution"
                ? "未采集"
                : "◇",
        ),
      );
      row.onclick = () => selectEvent(event.id);
      row.onkeydown = (key) => {
        if (key.key === "Enter" || key.key === " ") {
          key.preventDefault();
          selectEvent(event.id, true);
        }
        if (["ArrowDown", "ArrowUp"].includes(key.key)) {
          key.preventDefault();
          const index =
            rows.findIndex((e) => e.id === event.id) +
            (key.key === "ArrowDown" ? 1 : -1);
          if (rows[index]) selectEvent(rows[index].id, true);
        }
      };
      body.append(row);
    }
    $("event-list").scrollTop = following
      ? $("event-list").scrollHeight
      : scroll;
    $("events-empty").hidden = rows.length > 0;
    text(
      "events-empty",
      data.session.stale
        ? "会话同步暂不可用，等待重新连接。"
        : "当前筛选没有事件；页面不会触发任务。",
    );
    text("event-count", rows.length);
    $("trace-scope").value = selectedTurn ? "input" : "session";
    $("trace-scope").querySelector('option[value="input"]').disabled =
      !selectedTurn;
    $("follow-latest").setAttribute("aria-pressed", String(following));
    $("history-note").hidden = !(replay
      ? replay.incomplete
      : data.history_incomplete);
    if (replay)
      body.querySelector(".selected")?.scrollIntoView?.({ block: "nearest" });
  }
  function facts(items) {
    const dl = make("dl", "detail-facts");
    for (const [name, value] of items) {
      if (value === undefined || value === null || value === "") continue;
      dl.append(make("dt", "", name), make("dd", "", String(value)));
    }
    return dl;
  }
  function codeBlock(title, value) {
    const section = make("section", "code-section"),
      heading = make("div", "code-heading");
    section.dataset.section = title;
    heading.append(make("strong", "", title));
    const button = make("button");
    button.type = "button";
    button.setAttribute("aria-label", "复制" + title);
    button.append(icon("copy"));
    button.disabled = value === "" || value == null;
    button.onclick = () => copy(String(value));
    heading.append(button);
    section.append(
      heading,
      make("pre", "", value === "" || value == null ? "未记录" : String(value)),
    );
    return section;
  }
  // API mappings describe this project's protocol, not captured HTTP transactions.
  function protocolFor(event, kind = "") {
    const actual = recordedRequest(event);
    if (actual)
      return {
        ...actual,
        template: actual.path,
        recorded: true,
        source: "http capture",
      };
    if (event?.category === "resource") {
      const resource = (data.status.lifecycle || []).find(
        (item) => item.resource_id === event.input,
      );
      const key =
        resource?.resource ||
        [
          "agent",
          "environment",
          "session",
          "skill",
          "analysis",
          "browser",
        ].find(
          (kind) =>
            data.status[kind + "_id"] === event.input ||
            data.status[kind + "_sandbox_id"] === event.input,
        );
      if (key && event.kind === "resource_created")
        return {
          method: ["analysis", "browser"].includes(key) ? "SDK" : "POST",
          path: ["analysis", "browser"].includes(key)
            ? "Sandbox.create"
            : "/" + key + "s",
          template: "创建 " + key,
          source: "代码对应 · 未采集请求",
        };
    }
    const sid = data.status.session_id,
      eid = data.status.environment_id;
    const route = (method, path, template, source) => ({
      method,
      path,
      template,
      source,
    });
    if (kind === "queue" && eid)
      return route(
        "GET",
        "/environments/" + eid + "/work/stats",
        "/environments/{id}/work/stats",
        "Work queue statistics",
      );
    if (kind === "events" && sid)
      return route(
        "GET",
        "/sessions/" + sid + "/events",
        "/sessions/{id}/events",
        "Session events",
      );
    if (["agent", "skill", "session", "environment"].includes(kind)) {
      const id = resource(kind).id;
      return id
        ? route(
            "GET",
            "/" + kind + "s/" + id,
            "/" + kind + "s/{id}",
            "Resource API",
          )
        : null;
    }
    if (!event) return null;
    if (
      (event.id.startsWith("qca:") ||
        event.id.startsWith("qca-request:") ||
        event.id.startsWith("qca-result:")) &&
      sid
    )
      return route(
        "GET",
        "/sessions/" + sid + "/events",
        "/sessions/{id}/events",
        "Session events · 公开事件读取接口",
      );
    if (event.kind === "work_poll" && eid)
      return route(
        "GET",
        "/environments/" + eid + "/work/poll",
        "/environments/{id}/work/poll",
        "Worker work_poll 日志",
      );
    const suffix =
      event.kind === "work_ack"
        ? "ack"
        : [
              "heartbeat_confirmed",
              "heartbeat_failed",
              "heartbeat_stopped",
            ].includes(event.kind)
          ? "heartbeat"
          : null;
    if (suffix && eid && event.work_id)
      return route(
        "POST",
        "/environments/" + eid + "/work/" + event.work_id + "/" + suffix,
        "/environments/{id}/work/{work_id}/" + suffix,
        "Worker " + event.kind + " 日志",
      );
    return null;
  }
  function sourceRecord(event) {
    if (event.saved_evidence)
      return { label: "Worker 交互证据 · 仅保存元数据", value: event };
    const captured = recordedRequest(event);
    if (captured) return { label: "HTTP attempt · 脱敏采集", value: captured };
    const session = replay ? replay.session : data.session;
    const record = (session?.trace || []).find(
      (item) => "qca:" + item.id === event.id,
    );
    if (record) return { label: "QCA event · 已保存公开字段", value: record };
    const execution = replay ? replay.execution : data.execution;
    const runtime = (execution?.runtime || []).find(
      (item) =>
        item.kind === event.kind &&
        item.time === event.time &&
        (item.tool_use_id || null) === (event.tool_id || null) &&
        (item.work_id || null) === (event.work_id || null),
    );
    if (runtime)
      return { label: "Worker record · 已保存公开字段", value: runtime };
    return { label: "Observer record · 公开投影", value: event };
  }
  function visiblePhases(row) {
    const events = timeline();
    const visible = replay
      ? events.filter((event) => !replayFuture(event))
      : events;
    return row
      ? visible.filter((event) => row.event_ids.includes(event.id))
      : [];
  }
  function detailView() {
    if (selectedResource) {
      const item = resource(selectedResource);
      return {
        key: "resource:" + selectedResource,
        item,
        api: protocolFor(null, selectedResource),
        record:
          selectedResource === "queue"
            ? data.queue
            : selectedResource === "worker"
              ? data.worker
              : item,
      };
    }
    const original = timeline().find((item) => item.id === selected);
    if (!original) return { key: "empty" };
    const event = { ...original };
    const concealed = replay && replayFuture(original);
    if (concealed) {
      delete event.output;
      delete event.elapsed_ms;
      event.state = "future";
    }
    const row = currentOperation(),
      phases = row ? visiblePhases(row) : concealed ? [] : [event];
    const call = {
      ...(executions().find((item) => item.id === event.tool_id) || {}),
    };
    if (replay && event.tool_id) {
      const known = timeline().filter(
        (item) => item.tool_id === event.tool_id && !replayFuture(item),
      );
      const finished = known.some(
        (item) =>
          (item.kind === "tool_execution" && item.state !== "running") ||
          (item.kind === "tool_stage" && item.stage === "python_return"),
      );
      if (!finished) {
        delete call.output;
        delete call.finished_at;
        delete call.elapsed_ms;
      }
      if (
        !known.some((item) =>
          [
            "user.tool_result",
            "user.custom_tool_result",
            "agent.tool_result",
          ].includes(item.kind),
        )
      )
        delete call.posted_at;
    }
    const captured = recordedRequest(event);
    return {
      key: row?.id || event.id,
      event,
      row: row && displayedOperation(row),
      phases,
      call,
      captured,
      api: protocolFor(event),
      source: sourceRecord(event),
    };
  }
  function renderDetail() {
    const host = $("event-detail"),
      view = detailView();
    const fingerprint =
      tab === "raw"
        ? [
            view.key,
            tab,
            view.captured || view.phases?.map(sourceRecord) || view.record,
          ]
        : tab === "io"
          ? [
              view.key,
              tab,
              view.captured,
              view.call?.input,
              view.call?.code,
              view.call?.output,
              view.event?.input,
              view.event?.code,
              view.event?.output,
              view.row?.timing_state,
              view.item,
              view.record?.stats,
            ]
          : [view, view.item ? data.observed_at : null];
    const signature = JSON.stringify([tab, selectedInteraction, fingerprint]);
    if (signature === detailSignature) return;
    const same = detailKey === view.key + ":" + tab,
      scroll = host.scrollTop;
    const codeScroll = new Map(
      same
        ? [...host.querySelectorAll(".code-section")].map((section) => [
            section.dataset.section,
            [
              section.querySelector("pre").scrollLeft,
              section.querySelector("pre").scrollTop,
            ],
          ])
        : [],
    );
    detailSignature = signature;
    detailKey = view.key + ":" + tab;
    host.replaceChildren();
    host.setAttribute("aria-labelledby", "detail-tab-" + tab);
    populateDetail(host, view);
    host.scrollTop = same ? scroll : 0;
    host.querySelectorAll(".code-section").forEach((section) => {
      const previous = codeScroll.get(section.dataset.section);
      if (previous)
        [
          section.querySelector("pre").scrollLeft,
          section.querySelector("pre").scrollTop,
        ] = previous;
    });
  }
  function populateDetail(host, view) {
    if (view.key === "empty") {
      host.append(
        make("p", "empty", "选择操作，查看输入输出、执行位置与原始记录。"),
      );
      return;
    }
    const { event, row, item, call = {}, captured, api, phases = [] } = view;
    const title = make("div", "detail-title");
    title.append(
      make(
        "h3",
        "",
        item?.name || stepForEvent(event)?.label || row?.label || event.kind,
      ),
    );
    host.append(title);
    const interaction = !item && stepForEvent(event);
    if (interaction?.tool_ids?.length > 1) {
      const tasks = make("div", "batch-tasks");
      tasks.setAttribute("aria-label", "本批工具任务");
      for (const id of interaction.tool_ids) {
        const fact = timeline().find(
          (record) =>
            interaction.event_ids.includes(record.id) && record.tool_id === id,
        );
        const tool =
          executions().find((call) => call.id === id) ||
          data.session.tools?.find((call) => call.id === id);
        const button = make("button", "text-link", tool?.tool || id);
        button.title = id;
        button.setAttribute("aria-pressed", String(event.tool_id === id));
        button.disabled = !fact;
        button.onclick = () => selectEvent(fact.id, false, "", interaction.id);
        tasks.append(button);
      }
      host.append(
        make(
          "p",
          "detail-note",
          interaction.tool_ids.length +
            " 项工具任务 · 同一轮发布，点击查看各自输入输出",
        ),
        tasks,
      );
    }
    if (row)
      host.append(
        make(
          "p",
          "detail-note",
          (roles[row.role] || row.role) + " · " + timingLabel(row),
        ),
      );
    if (tab === "io") {
      if (captured) {
        host.append(
          make(
            "p",
            "detail-note",
            "实测 HTTP 尝试 · " +
              captured.method +
              " " +
              captured.path +
              " · status " +
              (captured.status_code ?? "未收到响应") +
              " · " +
              duration(captured.elapsed_ms),
          ),
        );
        const grid = make("div", "io-grid");
        grid.append(
          codeBlock("Request · 脱敏采集", jsonText(captured.request)),
          codeBlock("Response · 脱敏采集", jsonText(captured.response)),
        );
        host.append(grid);
        host.append(
          make(
            "p",
            "detail-note",
            captured.capture?.size_limited
              ? "部分正文因大小限制已省略；原始记录保留采集标记。"
              : "按端点白名单保留字段，认证信息和隐藏内容未记录。",
          ),
        );
      } else if (item) {
        host.append(make("p", "detail-note", item.description));
        if (selectedResource === "queue")
          host.append(
            codeBlock("Work stats · 公开字段", jsonText(view.record.stats)),
          );
        else if (item.id) {
          const link = make("div", "resource-id");
          idView(link, item);
          host.append(link);
        }
      } else {
        const input = call.input || event.input,
          code = call.code || event.code;
        const output = call.output || event.output;
        if (input || code || output) {
          const grid = make("div", "io-grid");
          if (input || code) {
            const left = make("div", "io-column");
            if (input) left.append(codeBlock("输入 · 已保存字段", input));
            if (code) left.append(codeBlock("代码 · Worker记录", code));
            grid.append(left);
          }
          if (output) grid.append(codeBlock("输出 · 已保存字段", output));
          host.append(grid);
        } else
          host.append(
            make(
              "p",
              "detail-note",
              row?.type === "event"
                ? "这是时间点事件；没有独立请求体或执行耗时。"
                : "该操作的输入输出未采集；执行详情中可查看已有时点与关联。",
            ),
          );
      }
    } else if (tab === "execution") {
      const related = row?.parent_id
        ? operationRows().find((item) => item.id === row.parent_id)
        : null;
      const nativeApi = api?.recorded;
      host.append(
        facts([
          [
            "记录类型",
            row?.type === "span"
              ? "持续操作 Span"
              : row?.type === "event"
                ? "时间点 Event"
                : event?.kind || "资源",
          ],
          ["状态", row?.state || event?.state],
          [
            "耗时",
            row
              ? timingLabel(row)
              : Number.isFinite(event?.elapsed_ms)
                ? duration(event.elapsed_ms)
                : event?.kind === "tool_execution"
                  ? "未采集"
                  : "◇ 时间点",
          ],
          ["计量来源", row?.measurement || "记录时点"],
          [
            "执行位置",
            row?.role
              ? roles[row.role]
              : event && (roles[event.role] || event.role),
          ],
          ["操作 ID", row?.id],
          ["事件 ID", event?.id],
          ["资源 ID", item?.id],
          ["Session ID", data.status.session_id],
          ["Work ID", event?.work_id],
          ["工具请求 ID", row?.tool_id || event?.tool_id],
          ["父操作", related?.label],
          [
            nativeApi ? "API" : "关联接口（映射）",
            api && api.method + " " + api.path,
          ],
          [
            "API证据",
            api && (nativeApi ? "实际采集" : "代码对应 · 未采集请求"),
          ],
          ["HTTP状态", captured?.status_code],
          ["请求尝试", captured?.attempt],
          ["事件时间", event ? clock(event.time) : null],
          [
            "观察时间",
            event ? clock(event.observed_at) : clock(data.observed_at),
          ],
          ["开始", clock(row?.started_at ?? call.started_at)],
          ["结束", clock(row?.finished_at ?? call.finished_at)],
          ["结果回传", call.posted_at && clock(call.posted_at)],
        ]),
      );
      if (related) {
        const link = make("button", "text-link", "查看关联操作 →");
        link.onclick = () =>
          selectEvent(related.event_ids.at(-1), false, related.id);
        host.append(link);
      }
      if (api && !nativeApi)
        host.append(
          make(
            "p",
            "detail-note",
            "旧记录只确认接口对应关系；不补造请求、响应或HTTP耗时。",
          ),
        );
      if (row?.measurement === "qca_paired_timestamps")
        host.append(
          make(
            "p",
            "detail-note",
            "模型区间来自QCA同源起止记录；不代表GPU执行时间，也不用于推算跨端排队耗时。",
          ),
        );
    } else {
      if (captured)
        host.append(
          codeBlock("HTTP attempt · 脱敏原始字段", jsonText(captured)),
        );
      else if (phases.length)
        host.append(
          codeBlock(
            "原始公开记录",
            jsonText(
              phases.map((phase) => ({
                source: sourceRecord(phase).label,
                record: sourceRecord(phase).value,
              })),
            ),
          ),
        );
      else host.append(codeBlock("公开资源记录", jsonText(view.record)));
    }
  }
  function render() {
    const unavailable =
      networkError ||
      data.status.state !== "ready" ||
      data.session.stale ||
      data.execution.stale ||
      data.queue.stale ||
      data.queue.state !== "ready";
    const notice =
      networkError ||
      data.status.preview_message ||
      (data.status.state !== "ready" && data.status.message) ||
      data.session.error ||
      data.execution.error ||
      data.queue.error ||
      "";
    text(
      "connection-status",
      networkError
        ? "连接中断"
        : replay
          ? "历史回放"
          : data.status.state === "cleaned"
            ? "已回收"
            : data.status.state === "expired"
              ? "已过期"
              : unavailable
                ? "部分离线"
                : "实时连接",
    );
    $("connection-status").classList.toggle("muted", !!unavailable || !!replay);
    $("connection-status").title = [
      notice,
      data.observed_at && "最后观察 " + clock(data.observed_at),
    ]
      .filter(Boolean)
      .join(" · ");
    renderResources();
    renderEvents();
    renderTurns();
    renderDetail();
    if (replay) {
      text("loop-status", "历史回放");
      text("worker-status", "历史观测模式");
    }
    draw();
  }
  // A single mapping drives event selection, the active flow and replay step badges.
  function eventContext(event) {
    if (!event) return { target: "", flow: "" };
    const interaction = stepForEvent(event);
    if (interaction)
      return { target: interaction.target, flow: interaction.flow };
    const captured = recordedRequest(event);
    if (captured) {
      const id = captured.response?.body?.id;
      const target = ["agent", "environment", "session", "skill"].find(
        (kind) => resource(kind).id === id && id,
      );
      if (target) return { target, flow: "" };
      return {
        target: captured.path.includes("/work/") ? "worker" : "events",
        flow: captured.path.includes("/work/")
          ? "work"
          : captured.method === "POST"
            ? "result"
            : "request",
      };
    }

    if (event.category === "resource") {
      const target = [
        "agent",
        "skill",
        "session",
        "environment",
        "analysis",
        "browser",
      ].find((key) => resource(key).id && resource(key).id === event.input);
      return { target: target || "", flow: "" };
    }
    if (event.kind === "user.message")
      return { target: "session", flow: "input" };
    if (["work", "heartbeat"].includes(event.category))
      return { target: "worker", flow: "work" };
    if (event.category === "model") return { target: "loop", flow: "model" };
    if (["agent.tool_use", "agent.custom_tool_use"].includes(event.kind))
      return { target: "events", flow: "request" };
    if (
      [
        "user.tool_result",
        "user.custom_tool_result",
        "agent.tool_result",
      ].includes(event.kind)
    )
      return { target: "events", flow: "result" };
    const call = executions().find((item) => item.id === event.tool_id);
    if (event.role === "transfer") return { target: "python", flow: "csv" };
    if (event.role === "browser" || call?.tool === "download_report")
      return { target: "browser", flow: "cdp" };
    if (call?.tool === "run_python")
      return { target: "python", flow: "python" };
    return {
      target: event.role === "analysis" ? "worker" : "session",
      flow: "",
    };
  }
  function draw() {
    const svg = $("flow-svg"),
      layer = $("flow-paths");
    const root = $("architecture").getBoundingClientRect();
    if (!root.width) return;
    svg.setAttribute("viewBox", "0 0 " + root.width + " " + root.height);
    layer.replaceChildren();
    const ns = "http://www.w3.org/2000/svg";
    let pulses = $("flow-pulses");
    if (!pulses) {
      pulses = document.createElementNS(ns, "g");
      pulses.id = "flow-pulses";
      svg.append(pulses);
    }
    const point = (id, side = "right") => {
      const r = $(id)?.getBoundingClientRect();
      if (!r) return null;
      return {
        x:
          (side === "left"
            ? r.left
            : side === "right"
              ? r.right
              : r.left + r.width / 2) - root.left,
        y:
          (side === "top"
            ? r.top
            : side === "bottom"
              ? r.bottom
              : r.top + r.height / 2) - root.top,
      };
    };
    const event = timeline().find((e) => e.id === selected);
    const { target, flow } = eventContext(event);
    const interaction = !selectedResource && stepForEvent(event);
    document
      .querySelectorAll("[data-resource].selected")
      .forEach((element) => element.classList.remove("selected"));
    document
      .querySelectorAll(".sending")
      .forEach((element) => element.classList.remove("sending"));
    if (interaction)
      $(resourceNodes[interaction.source])?.classList.add("sending");
    document
      .querySelectorAll(".trace-step")
      .forEach((element) => element.remove());
    const node = $(resourceNodes[selectedResource || target]);
    node?.classList.add("selected");
    const progress = !selectedResource && eventProgress(event);
    if (node && progress) {
      const step = make("span", "trace-step", String(progress.index));
      step.title =
        progress.label +
        " " +
        progress.index +
        "/" +
        progress.total +
        " · " +
        stepForEvent(event).label;
      step.setAttribute("aria-label", step.title);
      node.append(step);
    }
    const paths = {};
    function path(d, color, key) {
      const line = document.createElementNS(ns, "path");
      line.setAttribute("d", d);
      line.setAttribute(
        "class",
        "flow-line " + color + (flow === key ? " selected" : ""),
      );
      line.dataset.flow = key;
      layer.append(line);

      paths[key] = d;
    }
    function curve(a, b, color, key) {
      if (!a || !b) return;
      const gap = Math.max(18, Math.abs(b.x - a.x) * 0.4);
      path(
        "M" +
          a.x +
          "," +
          a.y +
          " C" +
          (a.x + gap) +
          "," +
          a.y +
          " " +
          (b.x - gap) +
          "," +
          b.y +
          " " +
          b.x +
          "," +
          b.y,
        color,
        key,
      );
    }
    const session = point("session-node", "left"),
      input = point("input-node", "right");
    if (session && input) {
      // The port belongs to the panel, not to a scrollable message element.
      input.y = session.y;
      curve(
        input,
        session,
        (data.session.turns || []).length ? "tool" : "tool waiting",
        "input",
      );
    }
    const modelStart = point("session-node", "bottom"),
      modelEnd = point("loop-node", "top");
    if (modelStart && modelEnd)
      path(
        "M" +
          modelStart.x +
          "," +
          modelStart.y +
          " L" +
          modelEnd.x +
          "," +
          modelEnd.y,
        "binding",
        "model",
      );
    curve(
      point("session-node", "right"),
      point("queue-node", "left"),
      "binding",
      "session-work",
    );
    const loopOut = point("loop-node", "right"),
      eventsIn = point("events-node", "left");
    if (loopOut && eventsIn) {
      curve(
        { ...loopOut, y: loopOut.y - 5 },
        { ...eventsIn, y: eventsIn.y - 5 },
        "tool",
        "loop-events",
      );
      curve(
        { ...eventsIn, y: eventsIn.y + 5 },
        { ...loopOut, y: loopOut.y + 5 },
        "result",
        "events-loop",
      );
    }
    const worker = point("worker-node", "left"),
      analysis = point("analysis-node", "left");
    const queue = point("queue-node", "right"),
      events = point("events-node", "right");
    if (worker && analysis && queue && events) {
      const gap = analysis.x - Math.max(queue.x, events.x);
      for (const [key, target, shift, lane, color] of [
        ["work", queue, -18, 0.22, "work"],
        ["request", events, 0, 0.5, "tool"],
        ["result", events, 18, 0.78, "result"],
      ]) {
        const start = { x: worker.x, y: worker.y + shift };
        const end = {
          x: target.x,
          y: target.y + (key === "request" ? -12 : key === "result" ? 12 : 0),
        };
        const x = Math.max(queue.x, events.x) + gap * lane;
        const forward = key === "request";
        const a = forward ? end : start,
          b = forward ? start : end;
        path(
          "M" + a.x + "," + a.y + " H" + x + " V" + b.y + " H" + b.x,
          color,
          key,
        );
      }
    }
    const workerOut = point("worker-node", "right"),
      pythonIn = point("python-node", "left");
    if (workerOut && pythonIn) {
      curve(
        { ...workerOut, y: workerOut.y - 5 },
        { ...pythonIn, y: pythonIn.y - 5 },
        "tool",
        "python",
      );
      curve(
        { ...pythonIn, y: pythonIn.y + 5 },
        { ...workerOut, y: workerOut.y + 5 },
        "result",
        "python-result",
      );
    }
    const replyStart = point("loop-node", "top"),
      replyEnd = point("session-node", "bottom");
    if (replyStart && replyEnd)
      curve(
        { ...replyStart, x: replyStart.x + 8 },
        { ...replyEnd, x: replyEnd.x + 8 },
        "result",
        "reply",
      );
    const bottom = point("worker-node", "bottom"),
      browser = point("browser-node", "top");
    if (bottom && browser) {
      const left = point("worker-node", "left"),
        right = point("worker-node", "right");
      const ports = [
        left.x + (right.x - left.x) * 0.28,
        left.x + (right.x - left.x) * 0.75,
      ];
      const border = point("analysis-node", "bottom");
      const mid = ((border?.y || bottom.y) + browser.y) / 2;
      path(
        "M" +
          ports[0] +
          "," +
          bottom.y +
          " V" +
          mid +
          " H" +
          (browser.x - 35) +
          " V" +
          browser.y,
        "binding",
        "cdp",
      );
      path(
        "M" +
          (browser.x + 35) +
          "," +
          browser.y +
          " V" +
          (mid + 15) +
          " H" +
          ports[1] +
          " V" +
          bottom.y,
        "binding",
        "csv",
      );
    }
    const pulseKey = interaction
      ? (replay ? "replay:" : "live:") +
        interaction.conversation.message_id +
        ":" +
        interaction.id
      : "";
    const newPulse = pulseKey !== drawnInteraction;
    if (newPulse) {
      pulses.replaceChildren();
      drawnInteraction = pulseKey;
    }
    function particle(key, color, repeat = 1) {
      if (!paths[key] || matchMedia("(prefers-reduced-motion:reduce)").matches)
        return;
      const dot = document.createElementNS(ns, "circle");
      dot.setAttribute("r", "3");
      dot.setAttribute("class", "flow-particle " + color);
      const motion = document.createElementNS(ns, "animateMotion");
      // SVG's default begin=0 is relative to page load. Newly inserted motions
      // otherwise finish immediately once the page has been open for a while.
      motion.setAttribute("begin", "indefinite");
      motion.setAttribute("path", paths[key]);
      motion.setAttribute(
        "dur",
        replay ? "0.65s" : (liveDelay() - 80) / 1000 + "s",
      );
      motion.setAttribute("repeatCount", repeat);
      motion.addEventListener("endEvent", () => dot.remove(), { once: true });
      dot.append(motion);
      pulses.append(dot);
      motion.beginElement?.();
    }
    if (replay) {
      if (replay.playing && newPulse)
        particle(flow, flow === "work" ? "" : "tool");
      return;
    }
    if (!following || pinnedTurn || unavailableAnimation(flow)) {
      pulses.replaceChildren();
      return;
    }
    if (
      data.worker.state === "healthy" &&
      data.worker.heartbeat_at !== heartbeatDrawn &&
      now() - data.worker.heartbeat_at < 5
    ) {
      particle("work", "");
      heartbeatDrawn = data.worker.heartbeat_at;
    }
    if (interaction && newPulse) particle(interaction.flow, "tool");
  }
  function unavailableAnimation(flow) {
    return (
      !!networkError ||
      data.status.state !== "ready" ||
      data.session.stale ||
      (["request", "result", "python", "python-result", "cdp", "csv"].includes(
        flow,
      ) &&
        data.execution.stale)
    );
  }
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    const controller = new AbortController(),
      timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch("/api/observer", {
        cache: "no-store",
        signal: controller.signal,
      });
      if (!response.ok) throw new Error();
      const next = await response.json();
      if (next.version !== 1 || !Array.isArray(next.events)) throw new Error();
      data = next;
      networkError = "";
      syncLiveFlow();
    } catch {
      networkError =
        "观察接口暂不可用，保留最后已观察的内容；不会自动重发任务。";
      data.worker = { ...data.worker, state: "unknown", stale: true };
      data.queue = { ...data.queue, stale: true };
      data.execution = { ...data.execution, stale: true };
    } finally {
      clearTimeout(timeout);
      refreshing = false;
      render();
    }
  }
  $("refresh").onclick = refresh;
  $("trace-view").onchange = () => {
    traceView = $("trace-view").value;
    selectedSpan = "";
    lastSignature = "";
    render();
  };
  $("event-search").oninput = () => {
    pauseReplay();
    lastSignature = "";
    render();
  };
  $("follow-latest").onclick = () => {
    followLatest();
  };
  $("trace-scope").onchange = () => {
    if ($("trace-scope").value === "session") {
      followLatest();
      return;
    }
    turnsSignature = lastSignature = "";
    render();
  };
  document.querySelectorAll("[data-filter]").forEach(
    (button) =>
      (button.onclick = () => {
        pauseReplay();
        filter = button.dataset.filter;
        document
          .querySelectorAll("[data-filter]")
          .forEach((e) => e.setAttribute("aria-pressed", String(e === button)));
        lastSignature = "";
        render();
      }),
  );
  document.querySelectorAll("[data-tab]").forEach(
    (button) =>
      (button.onclick = () => {
        tab = button.dataset.tab;
        document
          .querySelectorAll("[data-tab]")
          .forEach((e) =>
            e.setAttribute("aria-selected", String(e === button)),
          );
        renderDetail();
      }),
  );
  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.onkeydown = (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
        return;
      event.preventDefault();
      const tabs = [...document.querySelectorAll("[data-tab]")],
        index = tabs.indexOf(button);
      const next =
        event.key === "Home"
          ? 0
          : event.key === "End"
            ? tabs.length - 1
            : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) %
              tabs.length;
      tabs[next].click();
      tabs[next].focus();
    };
  });
  document.querySelectorAll("[data-resource]").forEach((element) => {
    element.addEventListener("click", (event) => {
      if (event.target.closest("a,.copy")) return;
      stopLiveFlow();
      pauseReplay();
      selectedResource = element.dataset.resource;
      following = false;
      render();
      event.stopPropagation();
    });
    if (element.tagName !== "BUTTON") {
      const heading = element.querySelector(
        ":scope > .node-heading h3, :scope > .group-heading h2",
      );
      if (heading) {
        const button = make("button", "node-title", heading.textContent);
        button.type = "button";
        button.setAttribute(
          "aria-label",
          "查看" + resource(element.dataset.resource).name + "详情",
        );
        heading.replaceChildren(button);
      }
    }
  });
  const resizer = $("dock-resize");
  let dockAdjusted = false;
  function dockSpace() {
    const main = document.querySelector("main"),
      style = getComputedStyle(main);
    return Math.max(
      0,
      (main.clientHeight || innerHeight - 70) -
        parseFloat(style.paddingTop || 0) -
        parseFloat(style.paddingBottom || 0) -
        resizer.offsetHeight,
    );
  }
  function resize(value) {
    const available = dockSpace();
    const minimum = Math.min(
      innerWidth < 650 ? 380 : 220,
      Math.round(available * 0.5),
    );
    const maximum = Math.max(minimum, available - 180);
    const height = Math.max(minimum, Math.min(maximum, value));
    document.documentElement.style.setProperty("--dock-height", height + "px");
    resizer.setAttribute("aria-valuemin", String(minimum));
    resizer.setAttribute("aria-valuemax", String(maximum));
    resizer.setAttribute("aria-valuenow", String(Math.round(height)));
  }
  function naturalCardHeight(card) {
    const style = getComputedStyle(card);
    const boxes = [...card.children]
      .filter((child) => !child.classList.contains("trace-step"))
      .map((child) => child.getBoundingClientRect())
      .filter((box) => box.height);
    const content = boxes.length
      ? Math.max(...boxes.map((box) => box.bottom)) -
        Math.min(...boxes.map((box) => box.top))
      : 0;
    return (
      content +
      [
        "paddingTop",
        "paddingBottom",
        "borderTopWidth",
        "borderBottomWidth",
      ].reduce((height, key) => height + (parseFloat(style[key]) || 0), 0)
    );
  }
  // Fit the real cards first; give spare vertical space to the trace dock.
  // After an explicit adjustment, keep the user's choice on subsequent updates.
  function fitDock() {
    // Measure intrinsic card heights, not distributed positions, so fitting
    // the dock cannot feed back into the new space-between column layout.
    const columns = [
      ".agent-resources",
      ".cloud-interfaces",
      ".runtime-instances",
    ];
    const contentHeight = Math.max(
      ...columns.map((selector) => {
        const column = document.querySelector(selector);
        const cards = [...column.children];
        const gap = parseFloat(getComputedStyle(column).rowGap) || 0;
        const heights = cards.map(naturalCardHeight);
        const stackHeight =
          selector === ".cloud-interfaces"
            ? Math.max(0, ...heights) * cards.length
            : heights.reduce((sum, height) => sum + height, 0);
        return stackHeight + gap * Math.max(0, cards.length - 1);
      }),
    );
    const group = document.querySelector(".cloud"),
      style = getComputedStyle(group);
    const chromeHeight =
      group.querySelector(":scope > .group-heading").getBoundingClientRect()
        .height +
      [
        "rowGap",
        "paddingTop",
        "paddingBottom",
        "borderTopWidth",
        "borderBottomWidth",
      ].reduce((height, key) => height + (parseFloat(style[key]) || 0), 0);
    const diagramHeight = Math.max(
      400,
      Math.ceil(contentHeight + chromeHeight),
    );
    resize(
      dockAdjusted ? $("trace-dock").offsetHeight : dockSpace() - diagramHeight,
    );
  }
  resizer.onpointerdown = (event) => {
    if (event.button !== 0) return;
    dockAdjusted = true;
    event.preventDefault();
    const start = event.clientY,
      height = $("trace-dock").offsetHeight;
    resizer.setPointerCapture(event.pointerId);
    resizer.onpointermove = (move) => resize(height + start - move.clientY);
    const finish = () => {
      resizer.onpointermove =
        resizer.onpointerup =
        resizer.onpointercancel =
          null;
      if (resizer.hasPointerCapture(event.pointerId))
        resizer.releasePointerCapture(event.pointerId);
    };
    resizer.onpointerup = resizer.onpointercancel = finish;
  };
  resizer.onkeydown = (event) => {
    if (["ArrowUp", "ArrowDown"].includes(event.key)) {
      dockAdjusted = true;
      event.preventDefault();
      resize(
        $("trace-dock").offsetHeight + (event.key === "ArrowUp" ? 30 : -30),
      );
    }
  };
  const layoutObserver = new ResizeObserver(fitDock);
  [
    document.querySelector("main"),
    document.querySelector(".cloud-grid"),
    $("environment-node"),
    $("browser-node"),
  ].forEach((element) => layoutObserver.observe(element));
  new ResizeObserver(() => requestAnimationFrame(draw)).observe(
    $("architecture"),
  );
  refresh();
  setInterval(refresh, 2500);
})();
