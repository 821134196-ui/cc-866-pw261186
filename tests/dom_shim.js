"use strict";
/* Minimal DOM shim — enough to execute app.js render paths in Node.
   Not a browser; it verifies structure and runtime errors only. */

class ClassList {
  constructor(node) { this.node = node; this.set = new Set(); }
  add(...c) { c.forEach((x) => this.set.add(x)); this._sync(); }
  remove(...c) { c.forEach((x) => this.set.delete(x)); this._sync(); }
  contains(c) { return this.set.has(c); }
  _sync() { this.node._className = [...this.set].join(" "); }
}

class Node {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parent = null;
    this.attributes = {};
    this.dataset = {};
    this.style = {};
    this.listeners = {};
    this._text = "";
    this._className = "";
    this.classList = new ClassList(this);
  }
  get className() { return this._className; }
  set className(v) {
    this._className = v || "";
    this.classList.set = new Set(String(v || "").split(/\s+/).filter(Boolean));
  }
  setAttribute(name, value) {
    if (name === "class") { this.className = value; return; }
    if (name === "style") { this.attributes.style = value; return; }
    this.attributes[name] = value;
  }
  getAttribute(name) { return this.attributes[name]; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent || "").join("");
    return this._text;
  }
  appendChild(child) {
    if (child && child.__isFragment) { child.children.forEach((c) => this.appendChild(c)); return child; }
    child.parent = this;
    this.children.push(child);
    return child;
  }
  replaceChildren(...nodes) {
    this.children = [];
    nodes.flat().forEach((n) => n && this.appendChild(n));
  }
  append(...nodes) { nodes.forEach((n) => this.appendChild(n)); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  }
  dispatch(type, event = {}) { (this.listeners[type] || []).forEach((fn) => fn(event)); }
  querySelector(sel) { return find(this, sel); }
  querySelectorAll(sel) { return findAll(this, sel); }
  focus() {}
  get firstChild() { return this.children[0] || null; }
  get innerHTML() { return ""; }
  set innerHTML(v) { throw new Error("innerHTML must not be used: " + v); }
}

function matches(node, sel) {
  if (!(node instanceof Node)) return false;
  sel = sel.trim();
  if (sel.startsWith("#")) return node.attributes.id === sel.slice(1);
  if (sel.startsWith(".")) return node.classList.contains(sel.slice(1));
  if (sel.startsWith("[")) {
    const m = sel.match(/^\[([^\]=]+)(?:="([^"]*)")?\]$/);
    if (!m) return false;
    return node.attributes[m[1]] !== undefined && (m[2] === undefined || node.attributes[m[1]] === m[2]);
  }
  return node.tagName === sel.toUpperCase();
}

function descendants(node, out = []) {
  node.children.forEach((c) => { if (c instanceof Node) { out.push(c); descendants(c, out); } });
  return out;
}
function find(root, sel) { return descendants(root).find((n) => matches(n, sel)) || null; }
function findAll(root, sel) { return descendants(root).filter((n) => matches(n, sel)); }

const document = {
  createElement: (tag) => new Node(tag),
  createTextNode: (text) => {
    const n = new Node("#text");
    n._text = String(text);
    return n;
  },
  createDocumentFragment: () => {
    const n = new Node("#fragment");
    n.__isFragment = true;
    return n;
  },
  body: new Node("body"),
  addEventListener() {},
  removeEventListener() {},
  querySelector(sel) { return find(this.body, sel); },
  querySelectorAll(sel) { return findAll(this.body, sel); },
};

// static page skeleton the real index.html provides
const main = document.createElement("main");
main.setAttribute("id", "main");
const eventList = document.createElement("div");
eventList.setAttribute("id", "event-list");
const toast = document.createElement("div");
toast.setAttribute("id", "toast");
toast.setAttribute("class", "toast");
document.body.append(main, eventList, toast);

// fetch mock wired to a real server URL via global __BASE__
global.fetch = async (path, options = {}) => {
  const url = (global.__BASE__ || "http://127.0.0.1:8765") + path;
  const res = await global.__realFetch__(url, options);
  const text = await res.text();
  return { ok: res.ok, status: res.status, text: async () => text };
};

module.exports = { document, Node, find, findAll, descendants };
