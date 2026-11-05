// DOM interaction checks; this does not replace rendered browser/layout verification.
// NODE_PATH=<directory containing jsdom> node --test tests/observer-ui.cjs
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");
const assets = path.join(
  __dirname,
  "../src/qoder_byoc_sandbox_example/demos/report_analyst/web",
);
async function waitFor(check) {
  const deadline = Date.now() + 2500;
  while (!check()) {
    assert.ok(Date.now() < deadline, "expected UI state was not reached");
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

function fixture() {
  const stamp = Date.now() / 1000;
  const event = (id, category, message, extra = {}) => ({
    id,
    kind: category === "message" ? "user.message" : category,
    category,
    message_id: message,
    label: id,
    role: "qoder",
    time: stamp,
    observed_at: stamp,
    state: "observed",
    ...extra,
  });
  return {
    version: 1,
    observed_at: stamp,
    status: {
      state: "ready",
      run_id: "run-a",
      agent_id: "agent-a",
      agent_version: 3,
      skill_id: "skill-a",
      session_id: "sess-a",
      environment_id: "env-a",
      session_url: "https://qoder.com/cloud/sessions/sess-a",
      sandbox_region: "cn-hangzhou",
      analysis_sandbox_id: "sbx-analysis-long-identity",
      browser_sandbox_id: "sbx-browser-long-identity",
    },
    session: {
      stale: false,
      active_message_id: "m2",
      tools: [],
      turns: [
        {
          id: "m1",
          text: "首次分析",
          source: "start.sh",
          time: stamp - 30,
          state: "idle",
        },
        {
          id: "m2",
          text: "解释当前结果",
          source: "console",
          time: stamp,
          state: "busy",
        },
      ],
    },
    execution: {
      stale: false,
      identity: { worker_id: "worker-a" },
      events: [
        {
          id: "tool-a",
          tool: "run_python",
          state: "running",
          role: "analysis",
        },
      ],
      runtime: [],
    },
    queue: {
      state: "ready",
      stale: false,
      observed_at: stamp,
      stats: { depth: 0, pending: 0, workers_polling: 0 },
      work: { id: "work-current", state: "active" },
    },
    worker: { state: "healthy", heartbeat_at: stamp, ttl_seconds: 60 },
    events: [
      event("message-one", "message", "m1", { output: "首次分析" }),
      event("execution:tool-a", "tool", "m1", {
        kind: "tool_execution",
        role: "analysis",
        tool_id: "tool-a",
        input: "input.csv",
        code: "print(42)",
        output: "42",
        work_id: "work-original",
        elapsed_ms: 123,
      }),
      event("message-two", "message", "m2", { output: "解释当前结果" }),
      event("model-two", "model", "m2", {
        kind: "model_request_start",
        label: "模型开始",
      }),
    ],
  };
}

// Supply the backend DTO in browser-only tests. Production numbering comes
// from the persistent SQLite sequence, tested independently in test_interactions.
function withInteractions(payload) {
  if (payload.interactions) return payload;
  const parents = new Map(),
    names = new Map();
  for (const tool of [
    ...(payload.session?.tools || []),
    ...(payload.execution?.events || []),
  ]) {
    if (tool.message_id) parents.set(tool.id, tool.message_id);
    if (tool.tool) names.set(tool.id, tool.tool);
  }
  for (const event of payload.events)
    if (event.tool_id && event.message_id)
      parents.set(event.tool_id, event.message_id);
  const conversations = (payload.session?.turns || []).map((turn, i) => {
    turn.number ||= i + 1;
    const steps = [];
    const add = (event, key, source, target, flow) => {
      let step = steps.find((item) => item.id === key);
      if (!step) {
        step = {
          id: key,
          kind: "interaction",
          source_kind: event.kind,
          tool_id: event.tool_id,
          source,
          target,
          flow,
          label: source + " → " + target,
          number: steps.length + 1,
          event_ids: [],
        };
        steps.push(step);
      }
      step.event_ids.push(event.id);
    };
    for (const event of payload.events) {
      if ((event.message_id || parents.get(event.tool_id)) !== turn.id)
        continue;
      const tool = event.tool_id;
      if (event.kind === "user.message")
        add(event, event.id, "input", "session", "input");
      else if (event.kind === "agent.message" && event.output?.trim())
        add(event, event.id, "loop", "session", "reply");
      else if (["agent.tool_use", "agent.custom_tool_use"].includes(event.kind))
        add(event, "request:" + tool, "loop", "events", "loop-events");
      else if (["tool_started", "tool_received"].includes(event.kind)) {
        add(event, "receive:" + tool, "events", "worker", "request");
        if (event.kind === "tool_started" && names.get(tool) === "run_python")
          add(event, "python-call:" + tool, "worker", "python", "python");
      } else if (
        event.kind === "tool_execution" &&
        names.get(tool) === "run_python" &&
        event.state !== "running"
      )
        add(
          event,
          "python-return:" + tool,
          "python",
          "worker",
          "python-result",
        );
      else if (
        [
          "user.tool_result",
          "user.custom_tool_result",
          "agent.tool_result",
        ].includes(event.kind)
      )
        add(event, "result:" + tool, "worker", "events", "result");
      else if (event.kind === "tool_stage" && event.stage === "csv_transfer")
        add(event, event.id, "browser", "worker", "csv");
      else if (
        ["session.status_running", "session.status_busy"].includes(event.kind)
      )
        add(event, "activate", "session", "loop", "model");
    }
    return {
      message_id: turn.id,
      number: turn.number,
      total_steps: steps.length,
      partial: false,
      steps,
    };
  });
  payload.interactions = { version: 4, conversations };
  return payload;
}

async function page(
  t,
  payload = fixture(),
  reduced = false,
  view = "operations",
  followExisting = true,
) {
  const dom = new JSDOM(
    fs.readFileSync(path.join(assets, "portal.html"), "utf8"),
    {
      url: "http://127.0.0.1:8799/",
      runScripts: "outside-only",
      pretendToBeVisual: true,
    },
  );
  t.after(() => dom.window.close());
  const w = dom.window,
    errors = [],
    requests = [];
  w.addEventListener("error", (e) => errors.push(e.error));
  let current = payload,
    failure = false;
  w.fetch = async (url, options) => {
    requests.push({ url, options });
    if (failure) throw new Error("network failure");
    const result = structuredClone(current);
    if (url.includes("?message_id=")) {
      result.message_id = decodeURIComponent(url.split("=")[1]);
      result.events = result.events.filter(
        (e) => e.message_id === result.message_id,
      );
      result.events.push({
        id: "history-reply",
        kind: "agent.message",
        category: "message",
        message_id: result.message_id,
        label: "历史回复",
        role: "qoder",
        state: "observed",
        output: "历史完整回复",
      });
    }
    return { ok: true, json: async () => withInteractions(result) };
  };
  w.matchMedia = (query) => ({
    matches: reduced && query.includes("reduced-motion"),
  });
  w.ResizeObserver = class {
    observe() {}
  };
  w.HTMLElement.prototype.getBoundingClientRect = function () {
    const width = this.id === "architecture" ? 1500 : 100;
    return { left: 0, top: 0, right: width, bottom: 80, width, height: 80 };
  };
  w.requestAnimationFrame = (callback) => {
    callback();
    return 0;
  };
  w.setInterval = () => 0;
  const copies = [];
  Object.defineProperty(w.navigator, "clipboard", {
    value: { writeText: async (s) => copies.push(s) },
  });
  w.eval(fs.readFileSync(path.join(assets, "live.js"), "utf8"));
  await new Promise(setImmediate);
  if (view) {
    w.document.getElementById("trace-view").value = view;
    w.document
      .getElementById("trace-view")
      .dispatchEvent(new w.Event("change"));
  }
  // Most fixtures represent an already-running Session. Explicitly join it;
  // first-load behavior is checked separately without this user action.
  if (followExisting) w.document.getElementById("follow-latest").click();
  assert.deepEqual(errors, [], "initial DOM render must not throw");
  return {
    w,
    d: w.document,
    copies,
    requests,
    errors,
    async refresh(next, fail = false) {
      current = next;
      failure = fail;
      await w.document.getElementById("refresh").onclick();
    },
  };
}

test("read-only portal renders identities, true zero queue counts and themes", async (t) => {
  const { d, requests } = await page(t);
  assert.equal(d.getElementById("agent-version").textContent, "使用 Agent v3");
  assert.equal(d.getElementById("queue-depth").textContent, "0");
  assert.match(d.getElementById("worker-status").textContent, /心跳/);
  assert.equal(
    d.querySelector("#analysis-id .id-text").textContent,
    "sbx-analysis-long-identity",
  );
  d.getElementById("theme-dark").click();
  assert.equal(d.documentElement.dataset.theme, "dark");
  assert.equal(
    d.defaultView.localStorage.getItem("qoder-observer-theme"),
    "dark",
  );
  assert.equal(d.querySelector("textarea"), null);
  assert.ok(
    requests.every((r) => r.url === "/api/observer" && !r.options.method),
  );
});

test("event selection shows I/O and the recorded Work, not the current Work", async (t) => {
  const { d, copies } = await page(t);
  d.querySelector('[data-event="execution:tool-a"]').click();
  const detail = d.getElementById("event-detail");
  d.querySelector('[data-tab="execution"]').click();
  assert.match(detail.textContent, /work-original/);
  d.querySelector('[data-tab="io"]').click();
  assert.match(detail.textContent, /input.csv/);
  assert.match(detail.textContent, /print\(42\)/);
  assert.doesNotMatch(detail.textContent, /work-current/);
  d.querySelector('[data-tab="io"]').click();
  const output = [...detail.querySelectorAll(".code-section")].find((section) =>
    section.dataset.section.startsWith("输出"),
  );
  assert.equal(output.querySelector("pre").textContent, "42");
  output.querySelector("button").click();
  await new Promise(setImmediate);
  assert.equal(copies.at(-1), "42");
});

test("message selection scopes events and maintains one input connector", async (t) => {
  const { d } = await page(t);
  d.querySelector('[data-turn="m1"]').click();
  const connection = d.querySelector('[data-flow="input"]');
  assert.ok(connection);
  const before = connection.getAttribute("d");
  assert.ok(d.querySelectorAll("#event-rows tr").length === 2);
  d.querySelector('[data-turn="m2"]').click();
  assert.equal(d.querySelectorAll('[data-flow="input"]').length, 1);
  assert.equal(
    d.querySelector('[data-flow="input"]').getAttribute("d"),
    before,
  );
  d.getElementById("turns").scrollTop = 500;
  d.getElementById("turns").dispatchEvent(new d.defaultView.Event("scroll"));
  assert.equal(
    d.querySelector('[data-flow="input"]').getAttribute("d"),
    before,
  );
  d.querySelector('[data-turn="m1"]').click();
  assert.equal(
    d.querySelector("animateMotion"),
    null,
    "history must not animate a current tool",
  );
});

test("search and filters update detail immediately and clear hidden selections", async (t) => {
  const { d, w } = await page(t);
  d.querySelector('[data-filter="tool"]').click();
  d.querySelector('[data-tab="io"]').click();
  assert.ok(d.getElementById("worker-node").classList.contains("selected"));
  assert.match(d.getElementById("event-detail").textContent, /print\(42\)/);
  const search = d.getElementById("event-search");
  search.value = "no-match";
  search.dispatchEvent(new w.Event("input"));
  assert.equal(d.getElementById("event-count").textContent, "0");
  assert.match(d.getElementById("event-detail").textContent, /选择操作/);
});

test("tool-free follow-up is followed and unsafe content stays text", async (t) => {
  const { d, refresh } = await page(t);
  const next = fixture();
  next.events.push({
    id: "reply",
    kind: "agent.message",
    category: "message",
    role: "qoder",
    label: "Agent 回复",
    message_id: "m2",
    output: '<img src=x onerror="alert(1)">这是解释',
    state: "observed",
  });
  await refresh(next);
  assert.equal(d.querySelector("#event-rows .selected").dataset.event, "reply");
  d.querySelector('[data-tab="io"]').click();
  assert.match(d.getElementById("event-detail").textContent, /这是解释/);
  assert.equal(d.querySelector('#event-detail img[src="x"]'), null);
});

test("network failure retains history but disables health, links and animation", async (t) => {
  const { d, refresh } = await page(t);
  await refresh(null, true);
  assert.equal(d.getElementById("connection-status").textContent, "连接中断");
  assert.equal(d.getElementById("worker-status").textContent, "状态待确认");
  assert.equal(d.getElementById("session-link").hasAttribute("href"), false);
  assert.equal(d.querySelector("animateMotion"), null);
  assert.equal(d.querySelectorAll("#turns .turn").length, 2);
});

test("queue failure is partial and reduced motion disables particles", async (t) => {
  const next = fixture();
  next.queue.stale = true;
  next.queue.state = "unavailable";
  const { d } = await page(t, next, true);
  assert.equal(d.getElementById("connection-status").textContent, "部分离线");
  assert.equal(d.querySelector("animateMotion"), null);
  d.querySelector('[data-resource="worker"]').click();
  assert.match(d.getElementById("event-detail").textContent, /worker-a/);
});

test("missing identity never creates a guessed Session link", async (t) => {
  const next = fixture();
  delete next.status.session_url;
  const { d } = await page(t, next);
  assert.equal(d.getElementById("session-link").hasAttribute("href"), false);
  assert.equal(d.querySelector("#session-id a"), null);
});

test("empty input keeps a waiting connection and resource links use instance list", async (t) => {
  const empty = fixture();
  empty.session.turns = [];
  const { d } = await page(t, empty);
  assert.ok(d.querySelector('[data-flow="input"].waiting'));
  assert.equal(
    d.querySelector("#analysis-id a").href,
    "https://console.aliyun.com/sandbox/cn-hangzhou/sandbox-instances",
  );
  assert.equal(
    d.querySelector("#browser-id a").href,
    "https://console.aliyun.com/sandbox/cn-hangzhou/sandbox-instances",
  );
  assert.equal(d.querySelector(".node-info"), null);
  assert.equal(d.querySelector("#all-turns"), null);
});

test("message replay fetches full history without sends and supports stepping back to live", async (t) => {
  const { d, requests, errors } = await page(t);
  d.querySelector('[data-turn="m1"] .replay-button').click();
  await new Promise(setImmediate);
  assert.equal(d.getElementById("connection-status").textContent, "历史回放");
  assert.equal(d.querySelectorAll(".trace-step").length, 1);
  assert.equal(d.querySelector("#session-node .trace-step").textContent, "1");
  assert.ok(
    d.querySelector('[data-event="history-reply"]'),
    "history response must be used instead of live window",
  );
  const step = (text) =>
    [...d.querySelectorAll(".replay-controls button")].find(
      (b) => b.textContent === text,
    );
  step("下一步").click();
  assert.equal(d.querySelector("#worker-node .trace-step").textContent, "2");
  assert.equal(d.querySelectorAll(".trace-step").length, 1);
  assert.equal(
    d.querySelector("#event-rows .selected").dataset.event,
    "execution:tool-a",
  );
  step("下一步").click();
  d.querySelector('[data-tab="io"]').click();
  assert.match(d.getElementById("event-detail").textContent, /历史完整回复/);
  assert.ok(requests.some((r) => r.url === "/api/observer?message_id=m1"));
  assert.ok(requests.every((r) => !r.options.method));
  step("返回实时").click();
  assert.equal(d.querySelector("#session-node .trace-step").textContent, "1");
  assert.equal(d.getElementById("connection-status").textContent, "实时连接");
  assert.equal(d.getElementById("trace-scope").value, "session");
  assert.deepEqual(errors, []);
});

test("returning live while history loads ignores the late history response", async (t) => {
  const { d, w } = await page(t);
  let resolve;
  w.fetch = () =>
    new Promise((done) => {
      resolve = done;
    });
  d.querySelector('[data-turn="m1"] .replay-button').click();
  d.getElementById("follow-latest").click();
  resolve({
    ok: true,
    json: async () => ({
      message_id: "m1",
      events: [{ id: "late-replay", category: "message" }],
    }),
  });
  await new Promise(setImmediate);
  assert.equal(d.querySelector("#session-node .trace-step").textContent, "1");
  assert.equal(d.getElementById("connection-status").textContent, "实时连接");
  assert.equal(d.querySelector('[data-event="late-replay"]'), null);
});

test("replay keeps exact-tool stages even when Worker did not record a message ID", async (t) => {
  const { d, w } = await page(t);
  w.fetch = async () => ({
    ok: true,
    json: async () =>
      withInteractions({
        message_id: "m1",
        session: { turns: [{ id: "m1", number: 1 }] },
        execution: { events: [] },
        events: [
          {
            id: "message",
            kind: "user.message",
            category: "message",
            message_id: "m1",
            label: "用户消息",
          },
          {
            id: "stage",
            kind: "tool_stage",
            category: "tool",
            tool_id: "tool-a",
            message_id: null,
            label: "CSV传输",
            role: "transfer",
          },
          {
            id: "reply",
            kind: "agent.message",
            category: "message",
            message_id: "m1",
            label: "Agent回复",
          },
        ],
      }),
  });
  d.querySelector('[data-turn="m1"] .replay-button').click();
  await new Promise(setImmediate);
  assert.equal(d.querySelectorAll("#event-rows tr").length, 3);
  assert.ok(d.querySelector('[data-event="stage"]'));
});

test("resource lifecycle selects Environment and clears it on the next event", async (t) => {
  const payload = fixture();
  payload.events.unshift({
    id: "env-created",
    category: "resource",
    kind: "resource_created",
    label: "Environment 创建",
    input: "env-a",
    role: "qoder",
  });
  const { d, errors } = await page(t, payload);
  d.querySelector('[data-event="env-created"]').click();
  assert.ok(d.querySelector("#environment-node.selected"));
  assert.equal(d.querySelector("#analysis-node.selected"), null);
  d.querySelector('[data-event="model-two"]').click();
  assert.equal(d.querySelector("#environment-node.selected"), null);
  assert.ok(d.querySelector("#loop-node.selected"));
  d.querySelector("#environment-node > .group-heading .node-title").click();
  assert.match(d.getElementById("event-detail").textContent, /env-a/);
  assert.ok(d.querySelector("#analysis-node > .node-heading .node-title"));
  assert.deepEqual(errors, []);
});

test("replay badges follow events, not manually inspected resources", async (t) => {
  const { d } = await page(t);
  assert.equal(
    d.querySelector(".flow-badge,.protocol-legend,.page-heading,#replay-note"),
    null,
  );
  d.querySelector('[data-turn="m1"] .replay-button').click();
  await new Promise(setImmediate);
  d.querySelector("#environment-node > .group-heading .node-title").click();
  assert.ok(d.querySelector("#environment-node.selected"));
  assert.equal(d.querySelector(".trace-step"), null);
  d.querySelector('[data-event="execution:tool-a"]').click();
  assert.equal(d.querySelector("#worker-node .trace-step").textContent, "2");
  assert.equal(d.querySelector("#environment-node.selected"), null);
});

test("unavailable replay is reported beside the message and can be retried", async (t) => {
  const { d, w } = await page(t);
  w.fetch = async () => {
    throw new Error("offline");
  };
  d.querySelector('[data-turn="m1"] .replay-button').click();
  await new Promise(setImmediate);
  assert.match(
    d.querySelector('[data-turn="m1"] [role="alert"]').textContent,
    /历史记录暂不可用/,
  );
  assert.equal(
    d.querySelector('[data-turn="m1"] .replay-button').disabled,
    false,
  );
});

test("replay keeps its selected frame and order while filters hide the current event", async (t) => {
  const { d } = await page(t);
  d.querySelector('[data-turn="m1"] .replay-button').click();
  await new Promise(setImmediate);
  d.querySelector('[data-filter="tool"]').click();
  const next = () =>
    [...d.querySelectorAll(".replay-controls button")].find(
      (b) => b.textContent === "下一步",
    );
  next().click();
  next().click();
  assert.equal(d.querySelector("#session-node .trace-step").textContent, "3");
  assert.match(
    d.querySelector(".replay-controls").textContent,
    /当前步骤被筛选隐藏/,
  );
  d.querySelector('[data-tab="io"]').click();
  assert.match(d.getElementById("event-detail").textContent, /历史完整回复/);
  d.querySelector('[data-filter="all"]').click();
  assert.ok(d.querySelector('[data-event="history-reply"].selected'));
  assert.equal(d.querySelectorAll(".trace-step").length, 1);
  assert.doesNotMatch(
    d.querySelector(".replay-controls").textContent,
    /筛选隐藏/,
  );
});

test("keyboard replay controls retain focus after rebuilding and at a boundary", async (t) => {
  const { d } = await page(t);
  d.querySelector('[data-turn="m1"] .replay-button').click();
  await new Promise(setImmediate);
  const next = () =>
    [...d.querySelectorAll(".replay-controls button")].find(
      (b) => b.textContent === "下一步",
    );
  next().focus();
  next().click();
  assert.equal(d.activeElement.dataset.focusKey, "m1:next");
  next().click();
  assert.equal(d.activeElement.dataset.focusKey, "m1:replay");
  const live = [...d.querySelectorAll(".replay-controls button")].find(
    (b) => b.textContent === "返回实时",
  );
  live.focus();
  live.click();
  assert.equal(d.activeElement.dataset.focusKey, "m1:replay");
});

test("inspector keeps the same selected content and scroll across polling", async (t) => {
  const payload = fixture();
  const { d, refresh } = await page(t, payload);
  d.querySelector('[data-event="execution:tool-a"]').click();
  d.querySelector('[data-tab="io"]').click();
  const host = d.getElementById("event-detail"),
    original = host.querySelector("pre");
  host.scrollTop = 135;
  await refresh({ ...payload, observed_at: payload.observed_at + 5 });
  assert.strictEqual(host.querySelector("pre"), original);
  assert.equal(host.scrollTop, 135);
  assert.equal(
    d.querySelector('[data-tab="io"]').getAttribute("aria-selected"),
    "true",
  );
  assert.equal(host.querySelector("details"), null);
  const next = structuredClone(payload);
  next.events.find((e) => e.id === "execution:tool-a").code = "print(43)";
  await refresh(next);
  assert.match(host.textContent, /print\(43\)/);
  assert.equal(host.scrollTop, 135);
});

test("API source and public event are shown without fabricating HTTP details", async (t) => {
  const payload = fixture();
  payload.session.trace = [
    {
      id: "evt-native",
      type: "agent.tool_use",
      tool: "run_python",
      permission: "allow",
      observed_at: 12,
    },
  ];
  payload.events.push({
    id: "qca:evt-native",
    kind: "agent.tool_use",
    category: "tool",
    label: "QCA 下发工具",
    role: "qoder",
    tool_id: "tool-a",
    input: "{}",
  });
  const { d, requests } = await page(t, payload);
  d.querySelector('[data-event="qca:evt-native"]').click();
  const host = d.getElementById("event-detail");
  d.querySelector('[data-tab="execution"]').click();
  assert.match(host.textContent, /GET \/sessions\/sess-a\/events/);
  assert.doesNotMatch(host.textContent, /200 OK|Authorization|Bearer/);
  d.querySelector('[data-tab="raw"]').click();
  assert.deepEqual(
    JSON.parse(host.querySelector("pre").textContent)[0].record,
    payload.session.trace[0],
  );
  assert.match(host.textContent, /原始公开记录/);
  assert.ok(requests.every((r) => !r.options.method));
});

test("Work API mapping uses the recorded work ID and does not invent extra polls", async (t) => {
  const payload = fixture();
  payload.events.push({
    id: "worker:ack",
    kind: "work_ack",
    category: "work",
    label: "确认 Work",
    work_id: "work-original",
    role: "worker",
  });
  const { d } = await page(t, payload);
  d.querySelector('[data-event="worker:ack"]').click();
  d.querySelector('[data-tab="execution"]').click();
  assert.match(
    d.getElementById("event-detail").textContent,
    /POST \/environments\/env-a\/work\/work-original\/ack/,
  );
  assert.doesNotMatch(
    d.getElementById("event-detail").textContent,
    /work-current/,
  );
  d.querySelector('[data-event="execution:tool-a"]').click();
  assert.doesNotMatch(
    d.getElementById("event-detail").textContent,
    /GET \/|POST \/|HTTP 200/,
  );
});

test("inspector tabs support keyboard navigation and BYOC labels deployment ownership", async (t) => {
  const { d, w } = await page(t);
  assert.equal(d.querySelector(".cloud > .group-heading .badge"), null);
  assert.match(d.querySelector(".byoc-badge").title, /用户自己的阿里云账号/);
  const headers = d.querySelector('[data-tab="execution"]');
  headers.dispatchEvent(
    new w.KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }),
  );
  assert.equal(d.activeElement.dataset.tab, "raw");
  assert.equal(
    d.getElementById("event-detail").getAttribute("aria-labelledby"),
    "detail-tab-raw",
  );
  d.activeElement.dispatchEvent(
    new w.KeyboardEvent("keydown", { key: "End", bubbles: true }),
  );
  assert.equal(d.activeElement.dataset.tab, "raw");
});

test("Worker source records keep their DOM and horizontal scroll when only observation time changes", async (t) => {
  const payload = fixture(),
    stamp = payload.observed_at;
  payload.execution.runtime = [
    { kind: "work_ack", time: stamp, work_id: "work-original" },
  ];
  payload.events.push({
    id: "worker:ack",
    kind: "work_ack",
    category: "work",
    label: "确认 Work",
    role: "worker",
    work_id: "work-original",
    time: stamp,
    observed_at: stamp,
  });
  const { d, refresh } = await page(t, payload);
  d.querySelector('[data-event="worker:ack"]').click();
  d.querySelector('[data-tab="raw"]').click();
  const host = d.getElementById("event-detail"),
    pre = host.querySelector("pre");
  host.scrollTop = 120;
  pre.scrollLeft = 100;
  const next = structuredClone(payload);
  next.events.at(-1).observed_at += 5;
  await refresh(next);
  assert.strictEqual(host.querySelector("pre"), pre);
  assert.equal(host.scrollTop, 120);
  assert.equal(pre.scrollLeft, 100);
  delete next.events.at(-1).time;
  await refresh(next);
  d.querySelector('[data-tab="execution"]').click();
  const header = host.firstElementChild;
  next.events.at(-1).observed_at += 5;
  await refresh(next);
  assert.equal(host.scrollTop, 0);
  assert.notEqual(host.firstElementChild, header);
});

test("changed public projections preserve code scrolling and resource Timing refreshes", async (t) => {
  const payload = fixture();
  const { d, refresh } = await page(t, payload);
  d.querySelector('[data-event="execution:tool-a"]').click();
  d.querySelector('[data-tab="raw"]').click();
  const host = d.getElementById("event-detail");
  host.querySelector("pre").scrollLeft = 100;
  const next = structuredClone(payload);
  next.events.find((e) => e.id === "execution:tool-a").observed_at += 5;
  await refresh(next);
  assert.equal(host.querySelector("pre").scrollLeft, 100);
  d.querySelector('[data-resource="agent"]').click();
  d.querySelector('[data-tab="execution"]').click();
  const before = host.textContent;
  await refresh({ ...next, observed_at: next.observed_at + 3600 });
  assert.notEqual(host.textContent, before);
});

function traceFixture() {
  const payload = fixture(),
    stamp = payload.observed_at;
  payload.session.active_message_id = "m1";
  payload.execution.events[0] = {
    ...payload.execution.events[0],
    state: "posted",
    message_id: "m1",
    started_at: stamp - 8,
    finished_at: stamp - 5,
    elapsed_ms: 3000,
    input: "input.csv",
    code: "print(42)",
    output: "42",
  };
  payload.events.push(
    {
      id: "start-a",
      kind: "tool_started",
      category: "tool",
      label: "开始分析",
      message_id: "m1",
      tool_id: "tool-a",
      state: "running",
      role: "analysis",
      time: stamp - 8,
      input: "input.csv",
    },
    {
      id: "result-a",
      kind: "user.tool_result",
      category: "tool",
      label: "工具回传",
      message_id: "m1",
      tool_id: "tool-a",
      state: "posted",
      role: "qoder",
      time: stamp - 4,
    },
    {
      id: "stage-a",
      kind: "tool_stage",
      stage: "csv_transfer",
      category: "tool",
      label: "CSV传输",
      message_id: "m1",
      tool_id: "tool-a",
      state: "observed",
      role: "transfer",
      time: stamp - 6,
    },
    {
      id: "resource-a",
      kind: "resource_created",
      category: "resource",
      label: "创建Agent",
      role: "qoder",
      state: "completed",
      input: "agent-a",
      time: stamp - 30,
    },
    {
      id: "lease-failed",
      kind: "heartbeat_failed",
      category: "heartbeat",
      label: "租约失败",
      role: "worker",
      state: "error",
      time: stamp - 2,
    },
  );
  const span = {
    id: "tool:tool-a",
    type: "span",
    operation: "tool",
    label: "run_python",
    role: "analysis",
    state: "posted",
    timing_state: "measured",
    measurement: "monotonic",
    started_at: stamp - 8,
    finished_at: stamp - 5,
    elapsed_ms: 3000,
    tool_id: "tool-a",
    message_id: "m1",
    event_ids: ["start-a", "execution:tool-a", "result-a"],
  };
  payload.tracing = {
    version: 1,
    groups: [
      {
        id: "setup",
        label: "准备资源",
        scope: "setup",
        rows: [
          {
            id: "event:resource-a",
            type: "event",
            operation: "resource",
            label: "创建Agent · POST /agents（未采集）",
            role: "qoder",
            state: "completed",
            timing_state: "instant",
            event_ids: ["resource-a"],
            started_at: stamp - 30,
          },
        ],
      },
      {
        id: "turn:m1",
        label: "对话",
        scope: "turn",
        message_id: "m1",
        rows: [
          span,
          {
            id: "event:stage-a",
            type: "event",
            operation: "stage",
            label: "CSV传输",
            role: "transfer",
            state: "observed",
            timing_state: "instant",
            parent_id: span.id,
            tool_id: "tool-a",
            message_id: "m1",
            event_ids: ["stage-a"],
            started_at: stamp - 6,
          },
        ],
      },
      {
        id: "turn:m2",
        label: "对话",
        scope: "turn",
        message_id: "m2",
        rows: [
          {
            id: "model:two",
            type: "span",
            operation: "model",
            label: "模型请求",
            role: "qoder",
            state: "unknown",
            timing_state: "incomplete",
            event_ids: ["model-two"],
            message_id: "m2",
          },
        ],
      },
      {
        id: "background",
        label: "会话与后台活动",
        scope: "background",
        rows: [
          {
            id: "event:lease-failed",
            type: "event",
            operation: "work",
            label: "租约失败",
            role: "worker",
            state: "error",
            timing_state: "instant",
            event_ids: ["lease-failed"],
            started_at: stamp - 2,
          },
        ],
      },
    ],
  };
  payload.events.find((e) => e.id === "message-one").time = stamp - 30;
  payload.events.find((e) => e.id === "execution:tool-a").time = stamp - 5;
  payload.events.sort((a, b) => (a.time || 0) - (b.time || 0));
  return payload;
}

test("operation tree merges phases, exposes stages and keeps a raw event view", async (t) => {
  const { d } = await page(t, traceFixture());
  d.querySelector('[data-turn="m1"]').click();
  assert.equal(d.querySelectorAll('[data-operation="tool:tool-a"]').length, 1);
  assert.equal(d.querySelector('[data-operation="event:stage-a"]'), null);
  d.querySelector('[aria-label="展开run_python的阶段"]').click();
  assert.ok(d.querySelector('[data-operation="event:stage-a"]'));
  d.querySelector('[data-operation="tool:tool-a"]').click();
  assert.match(d.getElementById("event-detail").textContent, /print\(42\)/);
  assert.match(d.getElementById("event-detail").textContent, /42/);
  assert.equal(d.querySelectorAll(".trace-bar").length, 1);
  const view = d.getElementById("trace-view");
  view.value = "events";
  view.dispatchEvent(new d.defaultView.Event("change"));
  assert.ok(d.querySelector('[data-event="start-a"]'));
  assert.ok(d.querySelector('[data-event="result-a"]'));
  assert.equal(d.querySelector("[data-operation]"), null);
});

test("background failures remain visible without pretending to be a Turn operation", async (t) => {
  const { d } = await page(t, traceFixture());
  d.querySelector('[data-turn="m1"]').click();
  assert.ok(d.querySelector(".trace-group.has-error"));
  assert.ok(d.querySelector('[data-operation="event:lease-failed"]'));
  assert.equal(
    d
      .querySelector('[data-operation="event:lease-failed"]')
      .textContent.includes("◇ 时间点"),
    true,
  );
  const resource = d.querySelector('.group-toggle[aria-expanded="false"]');
  resource.click();
  assert.match(d.getElementById("event-rows").textContent, /POST \/agents/);
});

test("missing span timing is explicit and does not become a zero duration", async (t) => {
  const { d } = await page(t, traceFixture());
  d.querySelector('[data-turn="m2"]').click();
  const row = d.querySelector('[data-operation="model:two"]');
  assert.match(row.textContent, /记录不完整/);
  assert.doesNotMatch(row.textContent, /0ms/);
  row.click();
  d.querySelector('[data-tab="execution"]').click();
  assert.match(d.getElementById("event-detail").textContent, /记录不完整/);
});

test("captured HTTP input and output includes real status and headers on their own side", async (t) => {
  const payload = traceFixture(),
    stamp = payload.observed_at;
  const record = {
    id: "http-1",
    kind: "http.request",
    method: "POST",
    path: "/agents",
    state: "success",
    status_code: 201,
    attempt: 1,
    started_at: stamp - 32,
    finished_at: stamp - 30,
    elapsed_ms: 2000,
    capture: { recorded: true },
    request: {
      headers: { "Content-Type": "application/json" },
      body: { name: "demo" },
    },
    response: {
      headers: { "Content-Type": "application/json" },
      body: { id: "agent-a" },
    },
  };
  payload.api_trace = { requests: [record] };
  payload.events.push({
    id: "api:http-1",
    api_request_id: "http-1",
    kind: "http.request",
    category: "http",
    label: "POST /agents",
    role: "qoder",
    state: "success",
    time: stamp - 30,
  });
  payload.tracing.groups[0].rows.unshift({
    id: "http:http-1",
    type: "span",
    operation: "http",
    label: "POST /agents",
    role: "qoder",
    state: "success",
    event_ids: ["api:http-1"],
    timing_state: "measured",
    measurement: "monotonic",
    started_at: stamp - 32,
    finished_at: stamp - 30,
    elapsed_ms: 2000,
  });
  const { d } = await page(t, payload);
  [...d.querySelectorAll(".group-toggle")]
    .find((b) => b.textContent.includes("准备资源"))
    .click();
  d.querySelector('[data-operation="http:http-1"]').click();
  const host = d.getElementById("event-detail");
  assert.match(host.textContent, /status 201/);
  const blocks = [...host.querySelectorAll("pre")];
  assert.deepEqual(JSON.parse(blocks[0].textContent), record.request);
  assert.deepEqual(JSON.parse(blocks[1].textContent), record.response);
  d.querySelector('[data-tab="execution"]').click();
  assert.match(host.textContent, /实际采集/);
});

test("replay conceals future operation outcome, duration and waterfall", async (t) => {
  const payload = traceFixture();
  payload.tracing.groups[1].rows[0].state = "error";
  const { d } = await page(t, payload);
  d.querySelector('[data-turn="m1"] .replay-button').click();
  await new Promise(setImmediate);
  const row = d.querySelector('[data-operation="tool:tool-a"]');
  assert.match(row.textContent, /尚未发生/);
  assert.ok(
    ![...d.querySelectorAll(".trace-group.has-error")].some((group) =>
      group.textContent.includes("首次分析"),
    ),
  );
  assert.doesNotMatch(row.textContent, /已回传|3\.0s|失败/);
  assert.equal(row.querySelector(".trace-bar"), null);
  [...d.querySelectorAll(".replay-controls button")]
    .find((b) => b.textContent === "下一步")
    .click();
  assert.match(
    d.querySelector('[data-operation="tool:tool-a"]').textContent,
    /执行中/,
  );
});

test("raw replay conceals future outcome and duration until its event", async (t) => {
  const payload = traceFixture();
  payload.events.find((e) => e.id === "execution:tool-a").state = "error";
  const { d } = await page(t, payload);
  d.querySelector('[data-turn="m1"] .replay-button').click();
  await new Promise(setImmediate);
  const view = d.getElementById("trace-view");
  view.value = "events";
  view.dispatchEvent(new d.defaultView.Event("change"));
  const future = d.querySelector('[data-event="execution:tool-a"]');
  assert.ok(future.classList.contains("future"));
  assert.match(future.textContent, /尚未发生/);
  assert.doesNotMatch(future.textContent, /123ms/);
  assert.ok(!future.querySelector(".event-state.error"));
  [...d.querySelectorAll(".replay-controls button")]
    .find((b) => b.textContent === "下一步")
    .click();
  for (
    let i = 0;
    i < 5 &&
    d
      .querySelector('[data-event="execution:tool-a"]')
      .classList.contains("future");
    i++
  )
    [...d.querySelectorAll(".replay-controls button")]
      .find((b) => b.textContent === "下一步")
      .click();
  const current = d.querySelector('[data-event="execution:tool-a"]');
  assert.ok(!current.classList.contains("future"));
  assert.match(current.textContent, /123ms/);
  assert.ok(current.querySelector(".event-state.error"));
});

test("live input steps advance, remain scoped and ignore background heartbeats", async (t) => {
  const payload = fixture();
  const { d, refresh, errors } = await page(t, payload);
  assert.equal(d.querySelector("#session-node .trace-step").textContent, "1");
  assert.match(
    d.querySelector('[data-turn="m2"] .live-progress').textContent,
    /交互 1 · 已发生 1/,
  );
  const next = structuredClone(payload);
  next.session.tools.push({ id: "live-tool", message_id: "m2" });
  next.execution.events.push({
    id: "live-tool",
    tool: "run_python",
    message_id: "m2",
    state: "running",
  });
  next.events.push({
    id: "live-start",
    kind: "tool_started",
    category: "tool",
    role: "analysis",
    tool_id: "live-tool",
    state: "running",
    label: "开始分析",
  });
  next.events.push({
    id: "heartbeat",
    kind: "heartbeat_confirmed",
    category: "heartbeat",
    role: "worker",
    state: "observed",
    label: "心跳",
  });
  await refresh(next);
  await waitFor(
    () => d.querySelector("#python-node .trace-step")?.textContent === "3",
  );
  assert.equal(d.querySelector("#python-node .trace-step").textContent, "3");
  assert.match(
    d.querySelector('[data-turn="m2"] .live-progress').textContent,
    /交互 3 · 已发生 3/,
  );
  assert.equal(d.querySelectorAll(".trace-step").length, 1);
  d.querySelector('[data-event="heartbeat"]').click();
  assert.equal(d.querySelector(".trace-step"), null);
  d.querySelector('[data-event="message-one"]').click();
  assert.equal(d.querySelector("#session-node .trace-step").textContent, "1");
  assert.match(
    d.querySelector('[data-turn="m1"] .live-progress').textContent,
    /交互 1 · 已发生 2/,
  );
  assert.equal(d.querySelector('[data-turn="m2"] .live-progress'), null);
  d.querySelector("#environment-node > .group-heading .node-title").click();
  assert.equal(d.querySelector(".trace-step"), null);
  assert.deepEqual(errors, []);
});

test("stable server step numbers survive filtering and partial windows", async (t) => {
  const payload = withInteractions(traceFixture());
  const entry = payload.interactions.conversations.find(
    (c) => c.message_id === "m1",
  );
  entry.number = 12;
  entry.total_steps = 8;
  entry.partial = true;
  entry.steps.at(-1).number = 8;
  payload.session.turns[0].number = 12;
  const { d } = await page(t, payload);
  d.querySelector('[data-filter="tool"]').click();
  assert.equal(d.querySelector(".trace-step").textContent, "8");
  assert.match(
    d.querySelector('[data-turn="m1"] .live-progress').textContent,
    /部分记录.*8.*已发生 8 次交互/,
  );
  assert.match(d.querySelector('[data-turn="m1"] time').textContent, /对话 12/);
  assert.ok(
    [...d.querySelectorAll(".group-toggle")].some((e) =>
      /对话 12/.test(e.textContent),
    ),
  );
  d.querySelector('[data-filter="model"]').click();
  assert.equal(
    d.querySelector(".trace-step"),
    null,
    "internal model records never get a handoff ordinal",
  );
  assert.match(d.querySelector("#event-rows").textContent, /组件内部/);
});

test("live operation view follows a tool stage instead of an older parent phase", async (t) => {
  const payload = traceFixture();
  payload.session.turns = [payload.session.turns[0]];
  payload.events.push({
    id: "stage-live",
    kind: "tool_stage",
    stage: "csv_transfer",
    category: "tool",
    role: "transfer",
    tool_id: "tool-a",
    state: "observed",
    label: "继续传输",
  });
  payload.tracing.groups[1].rows.push({
    id: "event:stage-live",
    type: "event",
    operation: "stage",
    parent_id: "tool:tool-a",
    role: "transfer",
    label: "继续传输",
    state: "observed",
    event_ids: ["stage-live"],
    tool_id: "tool-a",
  });
  const { d } = await page(t, payload);
  assert.equal(
    d.querySelector("#event-rows .selected").dataset.event,
    "stage-live",
  );
  assert.equal(d.querySelector("#worker-node .trace-step").textContent, "7");
  assert.match(
    d.querySelector('[data-turn="m1"] .live-progress').textContent,
    /7 · 已发生 7/,
  );
});

test("component handoffs share endpoints and ordinals in live, replay and manual inspection", async (t) => {
  const payload = traceFixture();
  // Deliberately different phase timestamps; flow catalog controls replay order.
  const phaseIds = [
    "message-one",
    "start-a",
    "stage-a",
    "execution:tool-a",
    "result-a",
  ];
  payload.events.sort((a, b) => {
    const ai = phaseIds.indexOf(a.id),
      bi = phaseIds.indexOf(b.id);
    return (ai < 0 ? 999 : ai) - (bi < 0 ? 999 : bi);
  });
  payload.events.push({
    id: "visible-reply",
    kind: "agent.message",
    category: "message",
    message_id: "m1",
    role: "qoder",
    label: "Agent回复",
    output: "完成分析",
  });
  for (let i = 0; i < 100; i++)
    payload.events.push({
      id: "noise-" + i,
      kind: i % 2 ? "heartbeat_confirmed" : "http.request",
      category: i % 2 ? "heartbeat" : "http",
      message_id: "m1",
      tool_id: "tool-a",
      role: "worker",
      label: "后台记录",
    });
  const data = withInteractions(payload);
  const entry = data.interactions.conversations.find(
    (c) => c.message_id === "m1",
  );
  assert.equal(entry.total_steps, 7);
  const { d, w } = await page(t, data);
  d.getElementById("trace-view").value = "events";
  d.getElementById("trace-view").dispatchEvent(
    new d.defaultView.Event("change"),
  );
  // A source fact can prove delivery and Python invocation; manual selection
  // uses its most specific handoff. Replay visits both explicit boundaries.
  const checked = new Set();
  for (const step of [...entry.steps].reverse()) {
    const id = step.event_ids[0];
    if (checked.has(id)) continue;
    checked.add(id);
    d.querySelector('[data-event="' + id + '"]').click();
    assert.equal(d.querySelectorAll(".trace-step").length, 1);
    assert.equal(
      d.querySelector(".trace-step").textContent,
      String(step.number),
    );
    assert.equal(
      d.querySelector(".trace-step").parentElement.id,
      step.target + "-node",
    );
  }
  d.querySelector('[data-event="noise-1"]').click();
  assert.equal(d.querySelector(".trace-step"), null);
  assert.match(d.querySelector('[data-event="noise-1"]').textContent, /后台/);
  const history = structuredClone(data);
  history.message_id = "m1";
  history.events = history.events.filter(
    (e) => e.message_id === "m1" || e.tool_id === "tool-a",
  );
  w.fetch = async () => ({
    ok: true,
    json: async () => structuredClone(history),
  });
  d.querySelector('[data-turn="m1"] .replay-button').click();
  await new Promise(setImmediate);
  const control = (name) =>
    [...d.querySelectorAll(".replay-controls button")].find(
      (b) => b.textContent === name,
    );
  for (const [i, step] of entry.steps.entries()) {
    if (i) control("下一步").click();
    assert.equal(
      d.querySelector(".trace-step").textContent,
      String(step.number),
    );
    assert.equal(
      d.querySelector(".trace-step").parentElement.id,
      step.target + "-node",
    );
    assert.match(
      d.querySelector(".replay-controls").textContent,
      new RegExp("交互 " + step.number + " / 7"),
    );
    assert.ok(d.querySelector('[data-flow="' + step.flow + '"].selected'));
  }
});

test("missing task flow history never falls back to numbering raw logs", async (t) => {
  const { d, w } = await page(t);
  w.fetch = async () => ({
    ok: true,
    json: async () => ({ message_id: "m1", events: fixture().events }),
  });
  d.querySelector('[data-turn="m1"] .replay-button').click();
  await new Promise(setImmediate);
  assert.match(
    d.querySelector('[data-turn="m1"] [role="alert"]').textContent,
    /重启本机 preview/,
  );
  assert.notEqual(
    d.getElementById("connection-status").textContent,
    "历史回放",
  );
});

test("replay output visibility follows phases even when cloud and worker clocks disagree", async (t) => {
  const payload = traceFixture();
  payload.events.unshift({
    id: "request-skew",
    kind: "agent.custom_tool_use",
    category: "tool",
    message_id: "m1",
    tool_id: "tool-a",
    label: "工具请求",
    role: "qoder",
    state: "observed",
    time: payload.observed_at + 10,
  });
  const data = withInteractions(payload);
  const entry = data.interactions.conversations.find(
    (c) => c.message_id === "m1",
  );
  const ordered = [
    "message-one",
    "request-skew",
    "start-a",
    "stage-a",
    "execution:tool-a",
    "result-a",
  ];
  entry.steps.sort(
    (a, b) => ordered.indexOf(a.event_ids[0]) - ordered.indexOf(b.event_ids[0]),
  );
  entry.steps.forEach((step, index) => (step.number = index + 1));
  const { d, w } = await page(t, data);
  const history = structuredClone(data);
  history.message_id = "m1";
  w.fetch = async () => ({
    ok: true,
    json: async () => structuredClone(history),
  });
  d.querySelector('[data-turn="m1"] .replay-button').click();
  await new Promise(setImmediate);
  [...d.querySelectorAll(".replay-controls button")]
    .find((b) => b.textContent === "下一步")
    .click();
  d.querySelector('[data-tab="io"]').click();
  assert.doesNotMatch(
    d.getElementById("event-detail").textContent,
    /输出 · 已保存字段/,
  );
  d.querySelector('[data-tab="raw"]').click();
  assert.doesNotMatch(
    d.getElementById("event-detail").textContent,
    /"output"\s*:\s*"42"/,
  );
  d.getElementById("trace-view").value = "events";
  d.getElementById("trace-view").dispatchEvent(
    new d.defaultView.Event("change"),
  );
  d.querySelector('[data-event="execution:tool-a"]').click();
  d.querySelector('[data-tab="io"]').click();
  assert.match(
    d.getElementById("event-detail").textContent,
    /输出 · 已保存字段/,
  );
  assert.equal(d.querySelector(".trace-step").textContent, "6");
});

test("mirrored result records keep one frame and one number across manual selection", async (t) => {
  const payload = fixture();
  const input = payload.events[0];
  const result = {
    id: "result-mirror",
    kind: "user.custom_tool_result",
    category: "tool",
    message_id: "m1",
    tool_id: "tool-a",
    label: "QCA 接收结果",
    role: "qoder",
  };
  const alias = { ...result, id: "result-official" };
  payload.events = [input, result, alias];
  payload.interactions = {
    version: 4,
    conversations: [
      {
        message_id: "m1",
        number: 1,
        total_steps: 2,
        partial: false,
        steps: [
          {
            id: "input",
            number: 1,
            source: "input",
            target: "session",
            flow: "input",
            event_ids: [input.id],
          },
          {
            id: "result",
            number: 2,
            source: "worker",
            target: "events",
            flow: "result",
            event_ids: [result.id, alias.id],
          },
        ],
      },
    ],
  };
  const { d, w } = await page(t, payload);
  w.fetch = async () => ({
    ok: true,
    json: async () => ({ ...structuredClone(payload), message_id: "m1" }),
  });
  d.querySelector('[data-turn="m1"] .replay-button').click();
  await new Promise(setImmediate);
  const next = () =>
    [...d.querySelectorAll(".replay-controls button")].find(
      (b) => b.textContent === "下一步",
    );
  next().click();
  assert.equal(d.querySelectorAll(".trace-step").length, 1);
  assert.equal(d.querySelector(".trace-step").textContent, "2");
  assert.equal(
    next().disabled,
    true,
    "result aliases must not repeat a playback step",
  );
  d.querySelector('[data-event="result-official"]').click();
  assert.equal(d.querySelector(".trace-step").textContent, "2");
  assert.equal(d.querySelector(".trace-step").parentElement.id, "events-node");
  assert.match(d.querySelector(".replay-controls").textContent, /交互 2 \/ 2/);
  assert.equal(next().disabled, true);
});

test("default component view exposes each handoff separately even when evidence is shared", async (t) => {
  const payload = withInteractions(traceFixture());
  const { d } = await page(t, payload, false, null);
  assert.equal(d.getElementById("trace-view").value, "interactions");
  const entry = payload.interactions.conversations.find(
    (c) => c.message_id === "m1",
  );
  const receipt = entry.steps.find((step) => step.flow === "request");
  const invocation = entry.steps.find((step) => step.flow === "python");
  assert.equal(receipt.event_ids[0], invocation.event_ids[0]);
  for (const step of [receipt, invocation]) {
    d.querySelector('[data-interaction="' + step.id + '"]').click();
    assert.equal(d.querySelectorAll(".trace-step").length, 1);
    assert.equal(
      d.querySelector(".trace-step").textContent,
      String(step.number),
    );
    assert.equal(
      d.querySelector(".trace-step").parentElement.id,
      step.target + "-node",
    );
    assert.ok(d.querySelector('[data-flow="' + step.flow + '"].selected'));
    assert.equal(
      d.getElementById("event-detail").querySelector("h3").textContent,
      step.label,
    );
    assert.equal(d.querySelectorAll("#event-rows .selected").length, 1);
    assert.equal(
      d.querySelector("#event-rows .selected").dataset.interaction,
      step.id,
    );
  }
  assert.doesNotMatch(
    d.getElementById("event-rows").textContent,
    /模型开始|租约失败/,
  );
});

test("incremental live execution and replay use identical component ordinals, endpoints and paths", async (t) => {
  let payload = fixture();
  payload.session.turns = [payload.session.turns[0]];
  payload.session.active_message_id = "m1";
  payload.session.tools = [
    { id: "tool-a", message_id: "m1", tool: "run_python" },
  ];
  payload.execution.events = [
    { id: "tool-a", message_id: "m1", tool: "run_python", state: "running" },
  ];
  payload.events = [payload.events[0]];
  const { d, w, refresh } = await page(t, payload, false, null);
  const seen = [{ number: "1", target: "session-node", flow: "input" }];
  const facts = [
    ["agent.custom_tool_use", "qoder", "tool", "events-node", "loop-events"],
    ["tool_received", "worker", "tool", "worker-node", "request"],
    ["tool_started", "analysis", "tool", "python-node", "python"],
    ["tool_execution", "analysis", "tool", "worker-node", "python-result"],
    ["user.custom_tool_result", "qoder", "tool", "events-node", "result"],
    ["agent.message", "qoder", "message", "session-node", "reply"],
  ];
  for (const [index, [kind, role, category, target, flow]] of facts.entries()) {
    payload = structuredClone(payload);
    payload.events.push({
      id: "cross-" + index,
      message_id: "m1",
      tool_id: category === "tool" ? "tool-a" : undefined,
      kind,
      role,
      category,
      state: kind === "tool_execution" ? "completed" : "observed",
      label: kind,
      output: kind === "agent.message" ? "完成" : "",
      time: payload.observed_at + index,
    });
    payload.events.push({
      id: "internal-" + index,
      message_id: "m1",
      kind: "model_request_start",
      category: "model",
      role: "qoder",
      label: "模型内部",
    });
    payload.events.push({
      id: "heartbeat-" + index,
      kind: "heartbeat_confirmed",
      category: "heartbeat",
      role: "worker",
      label: "心跳",
    });
    await refresh(payload);
    await waitFor(
      () => d.querySelector(".trace-step")?.textContent === String(index + 2),
    );
    const badge = d.querySelector(".trace-step");
    assert.equal(d.querySelectorAll(".trace-step").length, 1);
    assert.equal(badge.textContent, String(index + 2));
    assert.equal(badge.parentElement.id, target);
    assert.ok(d.querySelector('[data-flow="' + flow + '"].selected'));
    seen.push({ number: badge.textContent, target, flow });
  }
  const history = withInteractions(structuredClone(payload));
  history.message_id = "m1";
  assert.equal(history.interactions.conversations[0].total_steps, 7);
  w.fetch = async () => ({
    ok: true,
    json: async () => structuredClone(history),
  });
  d.querySelector('[data-turn="m1"] .replay-button').click();
  await new Promise(setImmediate);
  for (const [i, expected] of seen.entries()) {
    if (i)
      [...d.querySelectorAll(".replay-controls button")]
        .find((b) => b.textContent === "下一步")
        .click();
    assert.equal(d.querySelector(".trace-step").textContent, expected.number);
    assert.equal(
      d.querySelector(".trace-step").parentElement.id,
      expected.target,
    );
    assert.ok(d.querySelector('[data-flow="' + expected.flow + '"].selected'));
    const step = history.interactions.conversations[0].steps[i];
    d.querySelector('[data-interaction="' + step.id + '"]').click();
    assert.equal(d.querySelector(".trace-step").textContent, expected.number);
    assert.equal(
      d.querySelector("#event-rows .selected").dataset.interaction,
      step.id,
    );
  }
});

test("local Worker completion remains internal and cannot expose its output before result handoff", async (t) => {
  const payload = traceFixture();
  payload.execution.events[0].tool = "Read";
  payload.events = payload.events.filter((event) => event.id !== "stage-a");
  const history = withInteractions(payload);
  history.message_id = "m1";
  const { d, w } = await page(t, payload, false, null);
  w.fetch = async () => ({
    ok: true,
    json: async () => structuredClone(history),
  });
  d.querySelector('[data-turn="m1"] .replay-button').click();
  await new Promise(setImmediate);
  const next = () =>
    [...d.querySelectorAll(".replay-controls button")].find(
      (b) => b.textContent === "下一步",
    );
  next().click();
  assert.equal(d.querySelector(".trace-step").parentElement.id, "worker-node");
  assert.doesNotMatch(
    d.getElementById("event-detail").textContent,
    /输出 · 已保存字段/,
  );
  next().click();
  assert.equal(d.querySelector(".trace-step").parentElement.id, "events-node");
  assert.match(
    d.getElementById("event-detail").textContent,
    /输出 · 已保存字段/,
  );
});

test("legacy completion evidence never exposes the result at the inferred Python submission", async (t) => {
  const payload = fixture();
  payload.events = payload.events.filter((event) => event.message_id === "m1");
  payload.events[1].state = "completed";
  payload.interactions = {
    version: 4,
    conversations: [
      {
        message_id: "m1",
        number: 1,
        total_steps: 3,
        partial: false,
        steps: [
          {
            id: "input",
            number: 1,
            source: "input",
            target: "session",
            flow: "input",
            label: "会话输入 → Session",
            event_ids: ["message-one"],
          },
          {
            id: "call",
            number: 2,
            source: "worker",
            target: "python",
            flow: "python",
            tool_id: "tool-a",
            label: "Worker → Python · 提交代码",
            event_ids: ["execution:tool-a"],
          },
          {
            id: "return",
            number: 3,
            source: "python",
            target: "worker",
            flow: "python-result",
            tool_id: "tool-a",
            label: "Python → Worker · 返回结果",
            event_ids: ["execution:tool-a"],
          },
        ],
      },
    ],
  };
  const { d, w } = await page(t, payload, false, null);
  w.fetch = async () => ({
    ok: true,
    json: async () => ({ ...structuredClone(payload), message_id: "m1" }),
  });
  d.querySelector('[data-turn="m1"] .replay-button').click();
  await new Promise(setImmediate);
  const next = () =>
    [...d.querySelectorAll(".replay-controls button")].find(
      (b) => b.textContent === "下一步",
    );
  next().click();
  assert.equal(d.querySelector(".trace-step").parentElement.id, "python-node");
  assert.doesNotMatch(
    d.getElementById("event-detail").textContent,
    /输出 · 已保存字段/,
  );
  d.querySelector('[data-tab="raw"]').click();
  assert.doesNotMatch(
    d.getElementById("event-detail").textContent,
    /"output"\s*:\s*"42"/,
  );
  next().click();
  d.querySelector('[data-tab="io"]').click();
  assert.equal(d.querySelector(".trace-step").parentElement.id, "worker-node");
  assert.match(
    d.getElementById("event-detail").textContent,
    /输出 · 已保存字段/,
  );
});

test("Browser control and CSV return connect to the Worker leaf rather than the Analysis container", async (t) => {
  const payload = fixture();
  const { d, w, refresh } = await page(t, payload, false, null);
  const boxes = {
    architecture: [0, 0, 1200, 700],
    "analysis-node": [750, 180, 300, 220],
    "worker-node": [780, 200, 90, 80],
    "python-node": [900, 200, 90, 80],
    "browser-node": [750, 500, 200, 120],
  };
  w.HTMLElement.prototype.getBoundingClientRect = function () {
    const [left, top, width, height] = boxes[this.id] || [10, 10, 100, 50];
    return {
      left,
      top,
      right: left + width,
      bottom: top + height,
      width,
      height,
    };
  };
  await refresh(payload);
  const cdp = d.querySelector('[data-flow="cdp"]').getAttribute("d");
  const csv = d.querySelector('[data-flow="csv"]').getAttribute("d");
  assert.match(cdp, /^M805\.2,280 /);
  assert.match(csv, /H847\.5 V280$/);
  assert.doesNotMatch(csv, /V400$/);
});

test("real-order startup and a two-tool model batch highlight Agent Loop and publish only once", async (t) => {
  const payload = fixture();
  payload.session.turns = [payload.session.turns[0]];
  payload.session.active_message_id = "m1";
  payload.execution.events = [
    { id: "tool-a", tool: "Read", message_id: "m1" },
    { id: "tool-b", tool: "runtime_evidence", message_id: "m1" },
  ];
  payload.events = [
    payload.events[0],
    {
      id: "first-model",
      kind: "span.model_request_start",
      category: "model",
      role: "qoder",
      message_id: "m1",
      label: "模型开始",
    },
    {
      id: "remark",
      kind: "agent.message",
      category: "message",
      role: "qoder",
      message_id: "m1",
      label: "Agent 回复",
      output: "先检查技能和环境",
    },
    {
      id: "request-a",
      kind: "agent.tool_use",
      category: "tool",
      role: "qoder",
      message_id: "m1",
      tool_id: "tool-a",
      label: "Read",
      input: "skill-input",
    },
    {
      id: "request-b",
      kind: "agent.custom_tool_use",
      category: "tool",
      role: "qoder",
      message_id: "m1",
      tool_id: "tool-b",
      label: "runtime_evidence",
      input: "runtime-input",
    },
  ];
  payload.interactions = {
    version: 4,
    conversations: [
      {
        message_id: "m1",
        number: 1,
        total_steps: 4,
        partial: false,
        steps: [
          {
            id: "input",
            number: 1,
            source: "input",
            target: "session",
            flow: "input",
            label: "会话输入 → Session",
            event_ids: ["message-one"],
          },
          {
            id: "activate",
            number: 2,
            source: "session",
            target: "loop",
            flow: "model",
            label: "Session → Agent Loop",
            event_ids: ["first-model"],
          },
          {
            id: "remark",
            number: 3,
            source: "loop",
            target: "session",
            flow: "reply",
            label: "Agent Loop → Session",
            event_ids: ["remark"],
          },
          {
            id: "batch",
            number: 4,
            source: "loop",
            target: "events",
            flow: "loop-events",
            label: "Agent Loop → Events API",
            tool_ids: ["tool-a", "tool-b"],
            event_ids: ["request-a", "request-b"],
          },
        ],
      },
    ],
  };
  const { d, w } = await page(t, payload, false, null);
  d.querySelector('[data-interaction="activate"]').click();
  assert.equal(d.querySelector(".trace-step").textContent, "2");
  assert.equal(d.querySelector(".trace-step").parentElement.id, "loop-node");
  assert.ok(d.querySelector("#session-node.sending"));
  d.querySelector('[data-interaction="batch"]').click();
  assert.equal(d.querySelector(".trace-step").textContent, "4");
  assert.match(d.getElementById("event-detail").textContent, /2 项工具任务/);
  [...d.querySelectorAll(".batch-tasks button")]
    .find((b) => b.textContent === "runtime_evidence")
    .click();
  assert.equal(d.querySelector(".trace-step").textContent, "4");
  assert.match(d.getElementById("event-detail").textContent, /runtime-input/);
  w.fetch = async () => ({
    ok: true,
    json: async () => ({ ...structuredClone(payload), message_id: "m1" }),
  });
  d.querySelector('[data-turn="m1"] .replay-button').click();
  await new Promise(setImmediate);
  const expected = ["session-node", "loop-node", "session-node", "events-node"];
  for (let i = 0; i < expected.length; i++) {
    if (i)
      [...d.querySelectorAll(".replay-controls button")]
        .find((b) => b.textContent === "下一步")
        .click();
    assert.equal(d.querySelector(".trace-step").textContent, String(i + 1));
    assert.equal(d.querySelector(".trace-step").parentElement.id, expected[i]);
    assert.equal(d.querySelectorAll(".sending").length, 1);
    assert.equal(
      d.querySelector(".sending").id,
      ["input-node", "session-node", "loop-node", "loop-node"][i],
    );
  }
});

function incomingTurn(payload, id, running = false) {
  const next = structuredClone(payload);
  delete next.interactions;
  next.session.turns.push({
    id,
    text: id,
    state: "running",
    time: next.observed_at,
  });
  next.session.active_message_id = id;
  next.events.push({
    id: "input-" + id,
    kind: "user.message",
    category: "message",
    message_id: id,
    role: "qoder",
    label: id,
    output: id,
  });
  if (running)
    next.events.push({
      id: "running-" + id,
      kind: "session.status_running",
      category: "session",
      message_id: id,
      role: "qoder",
      label: "开始处理",
    });
  return next;
}

test("initial page focuses Session without replaying existing history or pinning new input", async (t) => {
  const payload = fixture();
  const { d, refresh } = await page(t, payload, false, null, false);
  assert.ok(d.querySelector("#session-node.selected"));
  assert.equal(d.querySelector(".trace-step"), null);
  assert.equal(
    d.getElementById("follow-latest").getAttribute("aria-pressed"),
    "true",
  );
  await refresh(incomingTurn(payload, "m3"));
  assert.equal(d.querySelector(".turn.selected").dataset.turn, "m3");
  assert.equal(d.querySelector("#session-node .trace-step").textContent, "1");
});

test("live poll batches show every handoff and redraw preserves the active particle", async (t) => {
  const payload = fixture();
  payload.worker.state = "unknown"; // Cloud interaction does not require a healthy Worker.
  const { d, refresh } = await page(t, payload, false, null, false);
  const next = incomingTurn(payload, "m3", true);
  await refresh(next);
  assert.equal(d.querySelector("#session-node .trace-step").textContent, "1");
  const particle = d.querySelector("#flow-pulses .flow-particle");
  assert.ok(particle);
  await refresh(next);
  assert.strictEqual(d.querySelector("#flow-pulses .flow-particle"), particle);
  assert.equal(d.querySelector("#session-node .trace-step").textContent, "1");
  await waitFor(
    () => d.querySelector("#loop-node .trace-step")?.textContent === "2",
  );
  assert.ok(d.querySelector('[data-flow="model"].selected'));
  assert.equal(d.querySelectorAll(".trace-step").length, 1);
});

test("new messages auto-follow after event inspection but respect an explicitly pinned message", async (t) => {
  const { d, refresh } = await page(t, fixture(), false, null, false);
  let payload = incomingTurn(fixture(), "m3");
  await refresh(payload);
  d.querySelector('[data-interaction="input-m3"]').click();
  payload = incomingTurn(payload, "m4");
  await refresh(payload);
  assert.equal(d.querySelector(".turn.selected").dataset.turn, "m4");
  assert.equal(
    d.getElementById("follow-latest").getAttribute("aria-pressed"),
    "true",
  );
  d.querySelector('[data-turn="m3"] .turn-select').click();
  payload = incomingTurn(payload, "m5");
  await refresh(payload);
  assert.equal(d.querySelector(".turn.selected").dataset.turn, "m3");
  assert.equal(
    d.getElementById("follow-latest").getAttribute("aria-pressed"),
    "false",
  );
  d.getElementById("follow-latest").click();
  assert.equal(d.querySelector(".turn.selected").dataset.turn, "m5");
  assert.equal(d.querySelector(".trace-step").textContent, "1");
  payload = incomingTurn(payload, "m6");
  await refresh(payload);
  assert.equal(d.querySelector(".turn.selected").dataset.turn, "m6");
});

for (const paused of [false, true]) {
  test(`new input exits ${paused ? "paused" : "playing"} replay and follows its live handoffs`, async (t) => {
    const payload = fixture();
    const { d, refresh } = await page(t, payload, false, null, false);
    d.querySelector('[data-turn="m1"] .replay-button').click();
    await new Promise(setImmediate);
    if (paused) d.querySelector('[data-turn="m1"] .replay-button').click();
    assert.ok(d.querySelector(".replay-controls"));
    await refresh(payload);
    assert.ok(
      d.querySelector(".replay-controls"),
      "polling alone must not stop replay",
    );
    await refresh(incomingTurn(payload, "m3", true));
    assert.equal(d.querySelector(".replay-controls"), null);
    assert.equal(d.querySelector(".turn.selected").dataset.turn, "m3");
    assert.equal(
      d.getElementById("follow-latest").getAttribute("aria-pressed"),
      "true",
    );
    assert.notEqual(
      d.getElementById("connection-status").textContent,
      "历史回放",
    );
    assert.equal(d.querySelector("#session-node .trace-step").textContent, "1");
    await waitFor(
      () => d.querySelector("#loop-node .trace-step")?.textContent === "2",
    );
  });
}

test("new input cancels pending replay history so a late response cannot restore the old message", async (t) => {
  const payload = fixture();
  const { d, w, refresh } = await page(t, payload, false, null, false);
  const originalFetch = w.fetch;
  let resolveHistory;
  w.fetch = (url, options) =>
    url.includes("?message_id=")
      ? new Promise((resolve) => {
          resolveHistory = resolve;
        })
      : originalFetch(url, options);
  d.querySelector('[data-turn="m1"] .replay-button').click();
  assert.match(
    d.querySelector('[data-turn="m1"] .replay-button').textContent,
    /读取历史/,
  );
  await refresh(incomingTurn(payload, "m3", true));
  assert.equal(d.querySelector(".turn.selected").dataset.turn, "m3");
  const history = withInteractions(structuredClone(payload));
  history.message_id = "m1";
  resolveHistory({ ok: true, json: async () => history });
  await new Promise(setImmediate);
  assert.equal(d.querySelector(".replay-controls"), null);
  assert.equal(d.querySelector(".turn.selected").dataset.turn, "m3");
  assert.equal(d.querySelector("#session-node .trace-step").textContent, "1");
  await waitFor(
    () => d.querySelector("#loop-node .trace-step")?.textContent === "2",
  );
});
