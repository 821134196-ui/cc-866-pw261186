"use strict";

/* ---------------------------------------------------------------------------
 * tiny helpers — all user-facing strings go through textContent, never innerHTML
 * ------------------------------------------------------------------------- */
const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "style") node.setAttribute("style", value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else if (key === "dataset") {
      for (const [k, v] of Object.entries(value)) node.dataset[k] = v;
    } else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

const SEVERITY_LABEL = { info: "兼容", degraded: "降级", breaking: "不兼容" };
const VERDICT_LABEL = {
  direct: "可直接升级",
  dual_write: "需双写",
  wait_drain: "等积压处理完",
  blocked: "不可升级",
  degraded: "可升级但有损",
  compatible: "兼容",
};
const VERDICT_GLYPH = {
  direct: "✓",
  dual_write: "⇄",
  wait_drain: "⏳",
  blocked: "✕",
  degraded: "△",
  compatible: "✓",
};
const MODE_LABEL = { backlog: "兼容历史积压", new_only: "只收新消息" };

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { message: text }; }
  if (!res.ok) {
    const message = data && (data.message || data.error) || `HTTP ${res.status}`;
    const err = new Error(message);
    err.details = data && data.details;
    err.status = res.status;
    throw err;
  }
  return data;
}

let toastTimer = null;
function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("show"), 2400);
}

function badge(verdict, labelOverride) {
  return el("span", { class: `badge ${verdict}` }, [
    el("span", { class: "glyph", text: VERDICT_GLYPH[verdict] || "•", "aria-hidden": "true" }),
    labelOverride || VERDICT_LABEL[verdict] || verdict,
  ]);
}

function fieldChips(fields, limit = 3) {
  const wrap = el("div", { class: "cell-fields" });
  fields.slice(0, limit).forEach((f) => {
    wrap.appendChild(el("span", { class: "chip bad", text: f }));
  });
  if (fields.length > limit) {
    wrap.appendChild(el("span", { class: "chip", text: `+${fields.length - limit}` }));
  }
  return wrap;
}

/* ---------------------------------------------------------------------------
 * state
 * ------------------------------------------------------------------------- */
const state = {
  events: [],
  current: null,
  data: null,
  tab: "matrix",
  simulate: { consumer: "", versions: null, dualWrite: false },
  releaseCandidate: null,
  impactVersion: null,
};

/* ---------------------------------------------------------------------------
 * sidebar
 * ------------------------------------------------------------------------- */
function renderSidebar() {
  const list = $("#event-list");
  list.replaceChildren();
  state.events.forEach((evt) => {
    const button = el("button", {
      class: "rail-item",
      "aria-current": state.current === evt.name ? "true" : "false",
      onclick: () => selectEvent(evt.name),
    }, [
      el("div", { class: "name", text: evt.name }),
      el("div", { class: "meta", text: `${evt.version_count} 个版本 · ${evt.consumer_count} 个消费者` }),
    ]);
    list.appendChild(button);
  });
}

async function loadEvents() {
  const data = await api("/api/event-types");
  state.events = data.event_types;
  renderSidebar();
}

async function selectEvent(name) {
  state.current = name;
  state.tab = "matrix";
  state.simulate = { consumer: "", versions: null, dualWrite: false };
  state.releaseCandidate = null;
  state.impactVersion = null;
  renderSidebar();
  await loadEvent();
}

async function loadEvent() {
  const main = $("#main");
  main.replaceChildren(el("div", { class: "empty", text: "正在加载…" }));
  try {
    state.data = await api(`/api/event-types/${encodeURIComponent(state.current)}`);
    renderEvent();
  } catch (err) {
    main.replaceChildren(el("div", { class: "callout warn", text: `加载失败：${err.message}` }));
  }
}

/* ---------------------------------------------------------------------------
 * event header + tabs
 * ------------------------------------------------------------------------- */
function summarySegbar(summary) {
  const total = summary.compatible + summary.degraded + summary.breaking || 1;
  const bar = el("div", { class: "segbar", role: "img", "aria-label": `兼容 ${summary.compatible}，降级 ${summary.degraded}，不兼容 ${summary.breaking}` });
  const add = (count, color, label) => {
    if (!count) return;
    bar.appendChild(el("span", {
      style: `width:${(count / total) * 100}%;background:${color}`,
      title: `${label} ${count}`,
    }));
  };
  add(summary.breaking, "var(--critical)", "不兼容");
  add(summary.degraded, "var(--warning)", "降级");
  add(summary.compatible, "var(--good)", "兼容");
  return bar;
}

function renderEvent() {
  const d = state.data;
  const main = $("#main");
  main.replaceChildren();

  const summary = d.matrix.summary;
  const head = el("div", { class: "event-head" }, [
    el("div", {}, [
      el("h1", { class: "event-title", text: d.event_type.description || d.event_type.name }),
      el("div", { class: "event-name", text: d.event_type.name }),
      el("div", { class: "event-desc", text: `${d.contracts.length} 个契约版本 · ${d.consumers.length} 个消费者 · ${d.messages.filter((m) => m.source === "recorded").length} 条已录历史消息` }),
    ]),
    el("button", { class: "btn", text: "重新计算", onclick: loadEvent }),
  ]);

  const stats = el("div", { class: "stats" }, [
    el("div", { class: "stat" }, [
      el("div", { class: "label", text: "生产者×消费者组合" }),
      el("div", { class: "value", text: String(summary.compatible + summary.degraded + summary.breaking) }),
      el("div", { class: "hint", text: "状态矩阵覆盖" }),
    ]),
    el("div", { class: "stat" }, [
      el("div", { class: "label", text: "完全兼容" }),
      el("div", { class: "value", text: String(summary.compatible), style: "color:#0a5c0a" }),
      el("div", { class: "hint", text: "可直接混跑" }),
    ]),
    el("div", { class: "stat" }, [
      el("div", { class: "label", text: "降级" }),
      el("div", { class: "value", text: String(summary.degraded), style: "color:#6b4a00" }),
      el("div", { class: "hint", text: "可运行但语义有损" }),
    ]),
    el("div", { class: "stat" }, [
      el("div", { class: "label", text: "不兼容" }),
      el("div", { class: "value", text: String(summary.breaking), style: "color:#8c1f1f" }),
      el("div", { class: "hint", text: "无法解码" }),
    ]),
  ]);
  stats.appendChild(el("div", { class: "stat", style: "flex:1;min-width:180px" }, [
    el("div", { class: "label", text: "分布" }),
    summarySegbar(summary),
    el("div", { class: "hint", text: "按配色：红=不兼容，黄=降级，绿=兼容", style: "margin-top:7px" }),
  ]));

  const tabs = el("div", { class: "tabs", role: "tablist" });
  const tabDefs = [
    ["matrix", "兼容矩阵"],
    ["simulate", "发布前模拟"],
    ["release", "升级判定"],
    ["changes", "变更结论"],
  ];
  tabDefs.forEach(([key, label]) => {
    tabs.appendChild(el("button", {
      class: "tab",
      role: "tab",
      "aria-selected": state.tab === key ? "true" : "false",
      onclick: () => { state.tab = key; renderEvent(); },
      text: label,
    }));
  });

  const panel = el("div", { class: "panel", role: "tabpanel" });
  if (state.tab === "matrix") panel.appendChild(renderMatrix());
  else if (state.tab === "simulate") panel.appendChild(renderSimulate());
  else if (state.tab === "release") panel.appendChild(renderRelease());
  else panel.appendChild(renderChanges());

  main.append(head, stats, tabs, panel);
}

/* ---------------------------------------------------------------------------
 * matrix tab
 * ------------------------------------------------------------------------- */
const SEVERITY_ORDER = { breaking: 0, degraded: 1, info: 2 };

function renderMatrix() {
  const d = state.data;
  const wrap = el("div", { class: "card flush" });
  const head = el("div", { class: "card-head", style: "padding:16px 20px 0" }, [
    el("div", {}, [
      el("div", { class: "card-title", text: "生产者版本 × 消费者版本" }),
      el("div", { class: "card-sub", text: "每格是一次真实配对：该版本消息投递给该消费者时逐字段解码的结果。点击查看失败字段。" }),
    ]),
  ]);
  wrap.appendChild(head);

  if (!d.contracts.length) {
    wrap.appendChild(el("div", { class: "empty", text: "尚无契约版本" }));
    return wrap;
  }
  if (!d.consumers.length) {
    wrap.appendChild(el("div", { class: "empty", text: "尚无消费者注册" }));
    return wrap;
  }

  const table = el("table", { class: "matrix" });
  const thead = el("thead");
  const headerRow = el("tr");
  headerRow.appendChild(el("th", { class: "rowhead", text: "生产者版本" }));
  d.matrix.consumers.forEach((consumer) => {
    headerRow.appendChild(el("th", {}, [
      el("span", { class: "mono", text: consumer.name }),
      el("span", { class: "mode", text: `@v${consumer.version} · ${MODE_LABEL[consumer.mode] || consumer.mode}` }),
    ]));
  });
  thead.appendChild(headerRow);
  table.appendChild(thead);

  const tbody = el("tbody");
  d.matrix.rows.forEach((row) => {
    const tr = el("tr");
    const head = el("th", { class: "rowhead" }, [
      el("span", { text: `v${row.producer_version}` }),
      el("span", { class: "mode", text: row.status }),
    ]);
    tr.appendChild(head);
    row.cells.forEach((cell) => {
      const td = el("td");
      td.style.padding = "0";
      const inner = el("div", {
        class: "cell",
        tabindex: "0",
        role: "button",
        dataset: { sev: cell.severity },
        "aria-label": `${row.producer_version} 到 ${cell.consumer_name}：${SEVERITY_LABEL[cell.severity]}`,
        onclick: () => openPair(cell, row),
        onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openPair(cell, row); } },
      }, [
        el("div", { class: "cell-top" }, [
          el("span", { class: `dot ${cell.severity}`, "aria-hidden": "true" }),
          el("span", { class: "cell-label", text: SEVERITY_LABEL[cell.severity] }),
        ]),
        cell.breaking_fields && cell.breaking_fields.length
          ? fieldChips(cell.breaking_fields)
          : el("div", { class: "hint-small", text: cell.messages ? `${cell.messages.passed}/${cell.messages.total} 条消息通过` : "" }),
      ]);
      td.appendChild(inner);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);

  const scroll = el("div", { class: "matrix-wrap", style: "padding:12px 20px 18px" }, [table]);
  wrap.appendChild(scroll);
  wrap.appendChild(el("div", { class: "legend", style: "padding:0 20px 18px" }, [
    el("span", { class: "key" }, [el("span", { class: "dot info" }), "兼容：全部可解码"]),
    el("span", { class: "key" }, [el("span", { class: "dot degraded" }), "降级：可解码但语义有损"]),
    el("span", { class: "key" }, [el("span", { class: "dot breaking" }), "不兼容：至少一个字段无法解码"]),
    el("span", { class: "hint-small", text: "失败字段标签格式：生产者字段 → 消费者字段" }),
  ]));
  return wrap;
}

function openPair(cell, row) {
  if (cell.error) {
    toast(cell.error);
    return;
  }
  const body = el("div", { class: "drawer-body" });
  body.appendChild(el("div", { class: "callout" }, [
    el("strong", { text: `生产者 v${row.producer_version} → ${cell.consumer_name}@v${cell.consumer_version}` }),
    el("div", {
      text: `模式：${MODE_LABEL[cell.consumer_mode] || cell.consumer_mode}。结论：${SEVERITY_LABEL[cell.severity]}。`,
    }),
  ]));

  if (cell.breaking_fields.length) {
    body.appendChild(el("h2", { class: "card-title", text: "失败字段" }));
    const chips = el("div", { style: "display:flex;gap:5px;flex-wrap:wrap;margin:8px 0 16px" });
    cell.breaking_fields.forEach((f) => chips.appendChild(el("span", { class: "chip bad", text: f })));
    body.appendChild(chips);
  }

  body.appendChild(el("h2", { class: "card-title", text: "字段级差异" }));
  const findings = (cell.schema && cell.schema.findings || []).filter((f) => f.severity !== "info");
  if (!findings.length) {
    body.appendChild(el("div", { class: "empty", text: "无降级或不兼容的字段差异" }));
  } else {
    const table = el("table", { class: "grid" });
    table.appendChild(el("thead", {}, [el("tr", {}, [
      el("th", { text: "严重度" }), el("th", { text: "字段" }), el("th", { text: "原因" }), el("th", { text: "处置" }),
    ])]));
    const tb = el("tbody");
    findings.forEach((f) => {
      const fieldText = f.producer_field && f.producer_field !== f.field
        ? `${f.producer_field} → ${f.field}` : f.field;
      tb.appendChild(el("tr", {}, [
        el("td", {}, [el("span", { class: `chip ${f.severity === "breaking" ? "bad" : "warn"}`, text: SEVERITY_LABEL[f.severity] })]),
        el("td", { class: "mono", text: fieldText }),
        el("td", { text: f.message }),
        el("td", { class: "hint-small", text: f.remediation || "—" }),
      ]));
    });
    table.appendChild(tb);
    body.appendChild(el("div", { class: "card flush", style: "margin-top:10px" }, [table]));
  }

  const msgs = cell.messages;
  if (msgs) {
    body.appendChild(el("h2", { class: "card-title", text: "消息模拟", style: "margin-top:18px" }));
    body.appendChild(el("div", { class: "card-sub", text: `共 ${msgs.total} 条消息，失败 ${msgs.failed} 条。` }));
    if (msgs.issues && msgs.issues.length) {
      const table = el("table", { class: "grid", style: "margin-top:8px" });
      table.appendChild(el("thead", {}, [el("tr", {}, [
        el("th", { text: "字段" }), el("th", { text: "失败次数" }), el("th", { text: "涉及版本" }), el("th", { text: "示例消息" }), el("th", { text: "原因" }),
      ])]));
      const tb = el("tbody");
      msgs.issues.slice(0, 8).forEach((issue) => {
        const fieldText = issue.producer_field && issue.producer_field !== issue.field
          ? `${issue.producer_field} → ${issue.field}` : issue.field;
        tb.appendChild(el("tr", {}, [
          el("td", { class: "mono", text: fieldText }),
          el("td", { class: "num", text: String(issue.count) }),
          el("td", { class: "mono", text: (issue.versions || []).map((v) => `v${v}`).join(", ") || "—" }),
          el("td", { class: "mono", text: issue.sample || "—" }),
          el("td", { text: issue.message }),
        ]));
      });
      table.appendChild(tb);
      body.appendChild(table);
    }
  }

  showDrawer(`v${row.producer_version} → ${cell.consumer_name}@v${cell.consumer_version}`, body);
}

function showDrawer(title, bodyNode) {
  const close = () => backdrop.remove();
  const backdrop = el("div", { class: "drawer-backdrop", onclick: (e) => { if (e.target === backdrop) close(); } }, [
    el("div", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-label": title }, [
      el("div", { class: "drawer-head" }, [
        el("div", {}, [el("div", { class: "card-title", text: title })]),
        el("button", { class: "btn small", text: "关闭", onclick: close }),
      ]),
      bodyNode,
    ]),
  ]);
  document.body.appendChild(backdrop);
  const onKey = (e) => { if (e.key === "Escape") { close(); document.removeEventListener("keydown", onKey); } };
  document.addEventListener("keydown", onKey);
}

/* ---------------------------------------------------------------------------
 * simulate tab
 * ------------------------------------------------------------------------- */
function renderSimulate() {
  const d = state.data;
  const card = el("div", { class: "card" });
  card.appendChild(el("div", { class: "card-head" }, [
    el("div", {}, [
      el("div", { class: "card-title", text: "发布前模拟一段历史消息" }),
      el("div", { class: "card-sub", text: "选定生产者版本区间与消费者，按契约生成 full / minimal 两种序列化习惯的消息并逐条解码。" }),
    ]),
  ]));

  const sim = state.simulate;
  if (!sim.consumer && d.consumers.length) sim.consumer = d.consumers[0].name;
  if (!sim.versions) sim.versions = d.contracts.map((c) => c.version);

  const versionToggles = el("div", { style: "display:flex;gap:6px;flex-wrap:wrap" });
  d.contracts.forEach((c) => {
    const pressed = sim.versions.includes(c.version);
    versionToggles.appendChild(el("button", {
      class: "btn small",
      "aria-pressed": pressed ? "true" : "false",
      style: pressed ? "border-color:var(--accent);background:#eef4fd" : "",
      onclick: () => {
        sim.versions = pressed
          ? sim.versions.filter((v) => v !== c.version)
          : [...sim.versions, c.version].sort((a, b) => a - b);
        if (!sim.versions.length) sim.versions = [c.version];
        renderEvent();
      },
      text: `v${c.version}`,
    }));
  });

  const controls = el("div", { class: "controls" }, [
    el("label", {}, ["消费者", el("select", {
      onchange: (e) => { sim.consumer = e.target.value; renderEvent(); },
    }, d.consumers.map((c) => el("option", {
      value: c.name,
      selected: c.name === sim.consumer,
      text: `${c.name} @v${c.version} · ${MODE_LABEL[c.mode] || c.mode}`,
    })))]),
    el("label", {}, ["生产者版本", versionToggles]),
    el("label", {}, [
      el("input", {
        type: "checkbox",
        checked: sim.dualWrite,
        onchange: (e) => { sim.dualWrite = e.target.checked; renderEvent(); },
      }),
      "生产者在过渡期双写旧字段名",
    ]),
  ]);
  card.appendChild(controls);

  const results = el("div", { id: "sim-results" });
  card.appendChild(results);
  card.appendChild(el("div", { class: "hint-small", style: "margin-top:10px", text: "模拟消息只覆盖契约声明的字段；已录制的真实消息会一并重放。" }));

  if (!d.consumers.length) {
    results.appendChild(el("div", { class: "empty", text: "尚无消费者注册" }));
    return card;
  }

  results.appendChild(el("div", { class: "empty", text: "计算中…" }));
  api(`/api/event-types/${encodeURIComponent(d.event_type.name)}/simulate`, {
    method: "POST",
    body: JSON.stringify({
      consumer_name: sim.consumer,
      producer_versions: sim.versions,
      dual_write: sim.dualWrite,
    }),
  }).then((res) => {
    results.replaceChildren();
    if (!res.pairs.length) {
      results.appendChild(el("div", { class: "empty", text: "没有可模拟的消息" }));
      return;
    }
    const consumer = res.consumer;
    const intro = el("div", { class: "callout" }, [
      el("strong", { text: `${consumer.name}@v${consumer.contract_version}` }),
      el("span", { text: ` · ${MODE_LABEL[consumer.mode] || consumer.mode} · ${res.message_count} 条消息` }),
    ]);
    results.appendChild(intro);
    if (sim.dualWrite) {
      results.appendChild(el("div", { class: "callout" }, [
        "已打开双写：模拟消息同时携带旧字段名与新字段名；已录制的历史消息不会被改写。",
      ]));
    }

    res.pairs.forEach((pair) => {
      const sev = pair.failed ? "breaking" : "info";
      const row = el("div", { class: "card flush", style: "margin-bottom:10px" });
      row.appendChild(el("div", { class: "card-head", style: "padding:12px 16px 0" }, [
        el("div", { class: "change-title" }, [
          el("span", { class: "mono", text: `v${pair.producer_version} → ${pair.consumer_name}@v${pair.consumer_version}` }),
          badge(sev === "info" ? "direct" : "blocked", sev === "info" ? "全部通过" : "存在失败"),
        ]),
        el("div", { class: "card-sub", text: `${pair.passed}/${pair.total} 条通过` }),
      ]));
      if (pair.fields.length) {
        const table = el("table", { class: "grid", style: "margin-top:8px" });
        table.appendChild(el("thead", {}, [el("tr", {}, [
          el("th", { text: "失败字段" }), el("th", { text: "次数" }), el("th", { text: "错误码" }), el("th", { text: "首条示例" }), el("th", { text: "说明" }),
        ])]));
        const tb = el("tbody");
        pair.fields.forEach((f) => {
          const fieldText = f.producer_field && f.producer_field !== f.field ? `${f.producer_field} → ${f.field}` : f.field;
          tb.appendChild(el("tr", {}, [
            el("td", { class: "mono", text: fieldText }),
            el("td", { class: "num", text: String(f.count) }),
            el("td", {}, f.codes.map((c) => el("span", { class: "chip bad", text: c, style: "margin-right:3px" }))),
            el("td", { class: "mono", text: f.sample || "—" }),
            el("td", { text: f.message }),
          ]));
        });
        table.appendChild(tb);
        row.appendChild(table);
      } else {
        row.appendChild(el("div", { class: "empty", text: "该版本全部消息可解码" }));
      }
      results.appendChild(row);
    });
  }).catch((err) => {
    results.replaceChildren(el("div", { class: "callout warn", text: `模拟失败：${err.message}` }));
  });

  return card;
}

/* ---------------------------------------------------------------------------
 * release tab
 * ------------------------------------------------------------------------- */
function renderRelease() {
  const d = state.data;
  const card = el("div", { class: "card" });
  card.appendChild(el("div", { class: "card-head" }, [
    el("div", {}, [
      el("div", { class: "card-title", text: "发布前升级判定" }),
      el("div", { class: "card-sub", text: "对每个消费者回答：当前部署能否直接接收新版本？需不需要双写？还是必须等旧消息处理完？" }),
    ]),
  ]));

  if (!d.contracts.length) {
    card.appendChild(el("div", { class: "empty", text: "尚无契约版本" }));
    return card;
  }
  if (state.releaseCandidate === null) {
    state.releaseCandidate = d.contracts[d.contracts.length - 1].version;
  }

  const select = el("select", {
    onchange: (e) => { state.releaseCandidate = Number(e.target.value); renderEvent(); },
  }, d.contracts.map((c) => el("option", {
    value: String(c.version),
    selected: c.version === state.releaseCandidate,
    text: `v${c.version} · ${c.status}`,
  })));

  card.appendChild(el("div", { class: "controls" }, [
    el("label", {}, ["候选版本", select]),
    el("span", { class: "hint-small", text: "候选版本作为生产者；其他版本作为历史积压。" }),
  ]));

  const results = el("div");
  results.appendChild(el("div", { class: "empty", text: "计算中…" }));
  card.appendChild(results);

  api(`/api/event-types/${encodeURIComponent(d.event_type.name)}/release-check`, {
    method: "POST",
    body: JSON.stringify({ candidate_version: state.releaseCandidate }),
  }).then((res) => {
    results.replaceChildren();
    const s = res.summary;
    results.appendChild(el("div", { class: "verdict-grid", style: "margin-bottom:16px" }, [
      tile(s.direct, "可直接升级", "direct"),
      tile(s.dual_write, "需双写", "dual_write"),
      tile(s.wait_drain, "等积压处理完", "wait_drain"),
      tile(s.blocked, "不可升级", "blocked"),
    ]));
    results.appendChild(el("div", { class: "hint-small", style: "margin-bottom:14px", text: `窗口：历史 v${res.window.backlog_producer_versions.join(", v") || "—"} 共 ${res.window.backlog_messages} 条；新版本 ${res.window.new_messages} 条。` }));

    res.consumers.forEach((entry) => {
      const row = el("div", { class: "card flush", style: "margin-bottom:10px" });
      const head = el("div", { class: "card-head", style: "padding:13px 16px 12px;margin-bottom:0" }, [
        el("div", {}, [
          el("div", { class: "change-title" }, [
            el("span", { class: "mono", text: `${entry.consumer_name}@v${entry.consumer_version}` }),
            badge(entry.verdict, VERDICT_LABEL[entry.verdict]),
            el("span", { class: "pill", text: MODE_LABEL[entry.mode] || entry.mode }),
          ]),
          el("div", { class: "change-detail", text: entry.reason }),
        ]),
        entry.breaking_fields.length
          ? el("div", { style: "display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end" },
              entry.breaking_fields.map((f) => el("span", { class: "chip bad", text: f })))
          : el("div", { class: "hint-small", text: entry.stage ? `失败阶段：${entry.stage}` : "无阻塞字段" }),
      ]);
      row.appendChild(head);

      const detail = el("div", { class: "detail" });
      detail.appendChild(el("dl", { class: "kv" }, [
        el("dt", { text: "当前部署读新消息" }),
        el("dd", { text: `${entry.frozen.failed}/${entry.frozen.total} 失败` }),
        el("dt", { text: "升级后回放积压" }),
        el("dd", {
          text: entry.backlog.ignored
            ? "不适用（该消费者只收新消息）"
            : `${entry.backlog.failed}/${entry.backlog.total} 失败`,
        }),
      ]));

      const shown = entry.stage === "upgraded_backlog" ? entry.backlog.errors : entry.frozen.errors;
      if (shown && shown.length) {
        const table = el("table", { class: "grid", style: "margin-top:10px" });
        table.appendChild(el("thead", {}, [el("tr", {}, [
          el("th", { text: "字段" }), el("th", { text: "错误码" }), el("th", { text: "涉及版本" }), el("th", { text: "说明" }), el("th", { text: "处置" }),
        ])]));
        const tb = el("tbody");
        shown.forEach((issue) => {
          const fieldText = issue.producer_field && issue.producer_field !== issue.field ? `${issue.producer_field} → ${issue.field}` : issue.field;
          tb.appendChild(el("tr", {}, [
            el("td", { class: "mono", text: fieldText }),
            el("td", {}, [el("span", { class: "chip bad", text: issue.code })]),
            el("td", { class: "mono", text: (issue.versions || []).map((v) => `v${v}`).join(", ") || "—" }),
            el("td", { text: issue.message }),
            el("td", { class: "hint-small", text: issue.remediation || "—" }),
          ]));
        });
        table.appendChild(tb);
        detail.appendChild(table);
      }

      if (entry.dual_write_preview) {
        detail.appendChild(el("div", { class: "callout", style: "margin-top:12px" }, [
          el("strong", { text: "双写预演：" }),
          entry.dual_write_preview.ok
            ? "打开双写后新消息全部可解码。"
            : `双写后仍有 ${entry.dual_write_preview.errors.length} 类字段失败，无法靠双写解决。`,
        ]));
        if (!entry.dual_write_preview.ok) {
          const chips = el("div", { style: "display:flex;gap:4px;flex-wrap:wrap;margin-top:6px" });
          entry.dual_write_preview.errors.forEach((e) => {
            const fieldText = e.producer_field && e.producer_field !== e.field ? `${e.producer_field} → ${e.field}` : e.field;
            chips.appendChild(el("span", { class: "chip bad", text: `${fieldText} · ${e.code}` }));
          });
          detail.appendChild(chips);
        }
      }
      row.appendChild(detail);
      results.appendChild(row);
    });
  }).catch((err) => {
    results.replaceChildren(el("div", { class: "callout warn", text: `判定失败：${err.message}` }));
  });

  return card;
}

function tile(count, label, verdict) {
  return el("div", { class: "verdict-tile" }, [
    el("div", { class: "num", text: String(count), style: `color:${verdictColor(verdict)}` }),
    el("div", { class: "cap", text: label }),
  ]);
}

function verdictColor(verdict) {
  return {
    direct: "#0a5c0a",
    dual_write: "#6b4a00",
    wait_drain: "#7a3418",
    blocked: "#8c1f1f",
  }[verdict] || "var(--primary-ink)";
}

/* ---------------------------------------------------------------------------
 * changes tab
 * ------------------------------------------------------------------------- */
function renderChanges() {
  const d = state.data;
  const card = el("div", { class: "card" });
  card.appendChild(el("div", { class: "card-head" }, [
    el("div", {}, [
      el("div", { class: "card-title", text: "逐项变更结论" }),
      el("div", { class: "card-sub", text: "重命名、填默认值、可选转必填等每一项变更，单独给出兼容结论与受影响的具体版本对。" }),
    ]),
  ]));

  if (d.contracts.length < 2) {
    card.appendChild(el("div", { class: "empty", text: "至少需要两个版本才能比较" }));
    return card;
  }
  if (state.impactVersion === null) {
    state.impactVersion = d.contracts[d.contracts.length - 1].version;
  }
  const select = el("select", {
    onchange: (e) => { state.impactVersion = Number(e.target.value); renderEvent(); },
  }, d.contracts.filter((c) => c.version > 1).map((c) => el("option", {
    value: String(c.version),
    selected: c.version === state.impactVersion,
    text: `v${c.version - 1} → v${c.version}`,
  })));
  card.appendChild(el("div", { class: "controls" }, [el("label", {}, ["版本对", select])]));

  const results = el("div");
  results.appendChild(el("div", { class: "empty", text: "计算中…" }));
  card.appendChild(results);

  api(`/api/event-types/${encodeURIComponent(d.event_type.name)}/impacts?to=${state.impactVersion}`)
    .then((res) => {
      results.replaceChildren();
      const s = res.summary;
      results.appendChild(el("div", { class: "stats", style: "margin:0 0 12px" }, [
        statChip("变更项", String(s.total), "var(--primary-ink)"),
        statChip("不兼容", String(s.breaking), "#8c1f1f"),
        statChip("降级", String(s.degraded), "#6b4a00"),
        statChip("兼容", String(s.compatible), "#0a5c0a"),
      ]));
      res.changes.forEach((change) => {
        results.appendChild(changeRow(change));
      });
    })
    .catch((err) => {
      results.replaceChildren(el("div", { class: "callout warn", text: `加载失败：${err.message}` }));
    });

  return card;
}

function statChip(label, value, color) {
  return el("div", { style: "display:flex;gap:7px;align-items:baseline" }, [
    el("span", { class: "label", text: label, style: "color:var(--secondary-ink);font-size:12px" }),
    el("span", { style: `font-weight:620;font-size:18px;color:${color}`, text: value }),
  ]);
}

function changeRow(change) {
  const row = el("div", { class: "card flush", style: "margin-bottom:10px" });
  const sevDot = change.severity === "breaking" ? "breaking" : change.severity === "degraded" ? "degraded" : "info";
  const head = el("div", { class: "card-head", style: "padding:12px 16px 0" }, [
    el("div", { style: "min-width:0" }, [
      el("div", { class: "change-title" }, [
        el("span", { class: `dot ${sevDot}`, "aria-hidden": "true" }),
        el("span", { class: "pill", text: change.kind_label || change.kind }),
        el("span", { class: "mono", text: change.field }),
        el("span", { class: `chip ${change.severity === "breaking" ? "bad" : change.severity === "degraded" ? "warn" : "info"}`, text: SEVERITY_LABEL[change.severity] }),
      ]),
      el("div", { class: "change-detail", text: change.detail }),
    ]),
    el("div", { class: "change-side" }, [badge(change.verdict, VERDICT_LABEL[change.verdict])]),
  ]);
  row.appendChild(head);

  const interesting = change.affected_pairs.filter((p) => p.severity !== "info");
  if (!interesting.length) {
    row.appendChild(el("div", { class: "detail hint-small", text: "所有消费者均兼容此项变更。" }));
    return row;
  }
  const table = el("table", { class: "grid", style: "margin-top:10px" });
  table.appendChild(el("thead", {}, [el("tr", {}, [
    el("th", { text: "消费者" }), el("th", { text: "模式" }), el("th", { text: "结论" }),
    el("th", { text: "当前部署失败" }), el("th", { text: "积压回放失败" }), el("th", { text: "原因" }),
  ])]));
  const tb = el("tbody");
  interesting.forEach((pair) => {
    tb.appendChild(el("tr", {}, [
      el("td", { class: "mono", text: `${pair.consumer_name}@v${pair.consumer_version}` }),
      el("td", { class: "hint-small", text: MODE_LABEL[pair.mode] || pair.mode }),
      el("td", {}, [badge(pair.verdict, VERDICT_LABEL[pair.verdict])]),
      el("td", { class: "num", text: String(pair.frozen_failures) }),
      el("td", { class: "num", text: pair.mode === "backlog" ? String(pair.backlog_failures) : "—" }),
      el("td", { text: pair.message || "—" }),
    ]));
  });
  table.appendChild(tb);
  const detail = el("div", { class: "detail" }, [table]);
  if (change.verdict === "dual_write" && change.dual_write_resolves !== null) {
    detail.appendChild(el("div", { class: "callout", style: "margin-top:10px" }, [
      "双写预演：过渡期同时发送旧名与新名后，当前部署的新消息全部可解码。",
    ]));
  }
  row.appendChild(detail);
  return row;
}

/* ---------------------------------------------------------------------------
 * boot
 * ------------------------------------------------------------------------- */
async function boot() {
  try {
    await loadEvents();
    if (state.events.length) await selectEvent(state.events[0].name);
    else $("#main").replaceChildren(el("div", { class: "empty", text: "尚无事件类型" }));
  } catch (err) {
    $("#main").replaceChildren(el("div", { class: "callout warn", text: `初始化失败：${err.message}` }));
  }
}

boot();
