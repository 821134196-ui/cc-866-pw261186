"use strict";
/* Executes the real static/app.js against the DOM shim and a live server,
   asserting that every tab renders and that failures name pair + field. */

const path = require("path");
const fs = require("fs");
const vm = require("vm");

const BASE = process.env.BASE_URL || "http://127.0.0.1:8765";

async function main() {
  const realFetch = global.fetch;
  const { document, findAll } = require("./dom_shim");
  global.__BASE__ = BASE;
  global.__realFetch__ = realFetch;

  const ctx = {
    window: {},
    document,
    fetch: global.fetch,
    setTimeout,
    clearTimeout,
    console,
    encodeURIComponent,
    Number,
    String,
    Object,
    Array,
    JSON,
    Error,
    Math,
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  const source = fs.readFileSync(
    path.join(__dirname, "..", "event_contract_registry", "static", "app.js"),
    "utf8"
  );
  vm.runInContext(source, ctx, { filename: "app.js" });

  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  // boot() kicked off network work; give it time to pick the first event
  for (let i = 0; i < 60; i += 1) {
    await wait(100);
    const mainNode = document.querySelector("#main");
    if (mainNode && mainNode.textContent.includes("兼容矩阵")) break;
  }

  // switch to the richer event (4 versions × 5 consumers)
  const items = findAll(document.querySelector("#event-list"), ".rail-item");
  const target = items.find((item) => item.textContent.includes("order.placed"));
  assert(target, "sidebar lists order.placed");
  target.dispatch("click");
  for (let i = 0; i < 60; i += 1) {
    await wait(100);
    if (document.querySelector("#main").textContent.includes("order.placed")) break;
  }

  const mainNode = document.querySelector("#main");
  const text = mainNode.textContent;
  assert(text.includes("兼容矩阵"), "matrix tab renders");
  assert(text.includes("不兼容"), "severity labels present");

  const cells = findAll(mainNode, ".cell");
  assert(cells.length >= 8, `matrix has cells (${cells.length})`);
  const breaking = cells.filter((c) => c.dataset.sev === "breaking");
  assert(breaking.length > 0, "at least one breaking cell");
  const chips = breaking.flatMap((c) => findAll(c, ".chip"));
  assert(chips.length > 0, "breaking cells name fields");

  const tabs = findAll(mainNode, ".tab");
  assert(tabs.length === 4, `four tabs (${tabs.length})`);

  // switch to each tab by invoking its click listener
  const expected = {
    1: "发布前模拟一段历史消息",
    2: "发布前升级判定",
    3: "逐项变更结论",
  };
  for (const [index, needle] of Object.entries(expected)) {
    tabs[Number(index)].dispatch("click");
    await wait(900);
    const current = document.querySelector("#main");
    assert(current.textContent.includes(needle), `tab ${index} renders: ${needle}`);
  }

  // release tab: distinct verdict labels must appear
  tabs[2].dispatch("click");
  await wait(900);
  const releaseText = document.querySelector("#main").textContent;
  for (const label of ["可直接升级", "需双写", "等积压处理完", "不可升级"]) {
    assert(releaseText.includes(label), `verdict ${label} present`);
  }
  const badges = findAll(document.querySelector("#main"), ".badge");
  assert(badges.length >= 4, "verdict badges rendered");

  // changes tab: defaults to the latest pair; drive it to v1→v2 where the
  // rename lives, then confirm the pair and the dual-write conclusion.
  tabs[3].dispatch("click");
  await wait(900);
  const changesMain = document.querySelector("#main");
  assert(changesMain.textContent.includes("逐项变更结论"), "changes tab renders");
  const pairSelect = findAll(changesMain, "select")[0];
  assert(pairSelect, "version-pair select present");
  pairSelect.dispatch("change", { target: { value: "2" } });
  await wait(900);
  const changesText = document.querySelector("#main").textContent;
  assert(changesText.includes("重命名字段"), "rename change listed");
  assert(changesText.includes("billing-legacy"), "affected consumer named");
  assert(changesText.includes("需双写"), "rename judged as dual-write");

  console.log("UI_RENDER_OK", JSON.stringify({ cells: cells.length, breaking: breaking.length, chips: chips.length }));
}

function assert(cond, message) {
  if (!cond) {
    console.error("UI_RENDER_FAIL:", message);
    process.exit(1);
  }
}

main().catch((err) => {
  console.error("UI_RENDER_ERROR:", err && err.stack || err);
  process.exit(1);
});
