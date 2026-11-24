"""Offline, self-contained relationship graph viewer for Hakimi memory.

Examples:
    python memory_graph_view.py --session group:123 --scope all_groups --out memory_graph.html
    python memory_graph_view.py --session group:123 --person-id 10001 --out person.html
    python memory_graph_view.py --session group:123 --topic 线性代数 --rebuild

The output is one HTML file.  It opens no port, loads no CDN/script/font and sends
no data.  Data comes only from the normal scope-checked MemoryStore graph API.
"""
from __future__ import annotations
from hakimi import paths

import argparse
import html
import json
from pathlib import Path
import sys

BASE_DIR = paths.base()


def render_html(data, *, title="哈基米长期记忆 · 可核对关系网"):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    template = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{color-scheme:light;--ink:#26313d;--muted:#66717d;--line:#d8dee6;--panel:#ffffff;--bg:#f3f6f9;--accent:#3b6ea8}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);font-family:"Microsoft YaHei","PingFang SC","Noto Sans CJK SC",system-ui,sans-serif}
body{display:flex;flex-direction:column;min-height:100%}
header{padding:12px 18px 10px;background:#fff;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:center;flex-wrap:wrap}
h1{font-size:18px;margin:0;font-weight:650;letter-spacing:.02em}header .meta{color:var(--muted);font-size:12px}
#toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:9px 18px;background:#fbfcfe;border-bottom:1px solid var(--line);font-size:13px}
input,select,button{font:inherit;font-size:12.5px;padding:6px 8px;border:1px solid #c9d2dc;border-radius:6px;background:#fff;color:var(--ink)}
input[type=search]{width:260px}button{cursor:pointer}button:hover{border-color:var(--accent);color:var(--accent)}
label.toggle{display:flex;align-items:center;gap:4px;color:var(--muted)}
#main{display:flex;flex:1;min-height:0}#canvasWrap{flex:1;position:relative;min-width:0;background:#f7f9fc}
svg{display:block;width:100%;height:100%;touch-action:none;cursor:grab}svg.panning{cursor:grabbing}
#empty{position:absolute;inset:0;display:none;align-items:center;justify-content:center;color:var(--muted);font-size:14px;text-align:center;padding:30px}
#stats{position:absolute;left:14px;bottom:12px;background:rgba(255,255,255,.92);border:1px solid var(--line);border-radius:8px;padding:7px 10px;color:var(--muted);font-size:12px;line-height:1.6;max-width:560px}
#legend{position:absolute;right:14px;bottom:12px;background:rgba(255,255,255,.94);border:1px solid var(--line);border-radius:8px;padding:8px 11px;font-size:12px;color:var(--muted);line-height:1.7;max-width:330px}
#legend b{color:var(--ink)}.swatch{display:inline-block;width:9px;height:9px;border-radius:50%;margin:0 4px 0 8px;vertical-align:-1px}
#line-sample{display:inline-block;width:28px;border-top:3px solid #8a99aa;vertical-align:3px;margin:0 4px}
#line-sample.dash{border-top-style:dashed;border-top-width:2px}
aside{width:360px;max-width:42vw;background:var(--panel);border-left:1px solid var(--line);overflow:auto;padding:14px 16px 30px}
aside h2{font-size:15px;margin:2px 0 10px}aside h3{font-size:12px;text-transform:none;letter-spacing:.04em;color:var(--muted);margin:16px 0 6px}
#detail .empty{color:var(--muted);font-size:13px;line-height:1.7}.kv{display:grid;grid-template-columns:78px 1fr;gap:5px 8px;font-size:12.5px;line-height:1.5}.kv .k{color:var(--muted)}
.badge{display:inline-block;padding:1px 6px;border-radius:999px;background:#eef3f9;color:#3b5f86;font-size:11px;margin:0 3px 3px 0}
.evidence{border-top:1px dashed var(--line);padding:8px 0;font-size:12.5px;line-height:1.55}.evidence .ex{color:#3a4652;white-space:pre-wrap;word-break:break-word}.evidence .sub{color:var(--muted);font-size:11px;margin-top:2px}
.node{stroke:#fff;stroke-width:1.5;cursor:pointer}.node:hover{stroke:#22303c;stroke-width:2}.node.selected{stroke:#111;stroke-width:3}
.node-label{font-size:11px;fill:#26313d;paint-order:stroke;stroke:#fff;stroke-width:3px;stroke-linejoin:round;pointer-events:none}.node-label.hidden{display:none}
.edge{fill:none;stroke:#8a99aa;stroke-opacity:.32}.edge.co_present{stroke-dasharray:5 4;stroke-opacity:.22}.edge.direct{stroke:#b45f3a;stroke-opacity:.62}
.edge-hit{fill:none;stroke:transparent;stroke-width:14;cursor:pointer}.edge-group.hidden,.node-group.hidden{display:none}
.edge.selected{stroke:#111;stroke-opacity:1;stroke-width:4}
#detail .pill{font-size:11px;color:#3b5f86;background:#eef3f9;border-radius:999px;padding:2px 7px;margin:0 3px 4px 0;display:inline-block}
#detail .warn{font-size:12px;color:#8a5a20;background:#fff6e6;border:1px solid #f0d7a8;border-radius:6px;padding:7px 8px;line-height:1.5}
@media(max-width:800px){aside{width:100%;max-width:none;border-left:0;border-top:1px solid var(--line)}#main{flex-direction:column}#canvasWrap{min-height:58vh}}
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <span class="meta">范围：<b id="scopeText"></b> · 生成于 <span id="generated"></span></span>
</header>
<div id="toolbar">
  <input id="search" type="search" placeholder="搜索人物ID、话题、M编号…" aria-label="搜索节点">
  <select id="groupFilter" aria-label="按群/会话筛选"><option value="">全部群/会话</option></select>
  <select id="relationFilter" aria-label="按关系类型筛选"><option value="">全部关系</option></select>
  <label class="toggle"><input id="labelsAlways" type="checkbox">显示全部标签</label>
  <button id="resetView" type="button">重置视图</button>
  <button id="relayout" type="button">重新排布</button>
  <button id="clearFilter" type="button">清除筛选</button>
</div>
<div id="main">
  <div id="canvasWrap">
    <svg id="graph" role="img" aria-label="记忆关系图"></svg>
    <div id="empty">当前范围没有可展示的关系。不会为了画面虚构节点。</div>
    <div id="stats"></div>
    <div id="legend">
      <b>图例</b><br>
      <span class="swatch" style="background:#4e79a7"></span>人物<span class="swatch" style="background:#f28e2b"></span>话题
      <span class="swatch" style="background:#59a14f"></span>会话事件<span class="swatch" style="background:#e15759"></span>记忆
      <span class="swatch" style="background:#76b7b2"></span>来源<span class="swatch" style="background:#9c755f"></span>本机助手<br>
      <span id="line-sample"></span>实线粗细=log2(独立证据数) <span id="line-sample" class="dash"></span>虚线=同场出现（非社会关系）<br>
      节点大小仅表示本图中的连接度；不表示人的重要性或关系亲疏。
    </div>
  </div>
  <aside>
    <h2>依据与细节</h2>
    <div id="detail"><div class="empty">点击节点或连线查看真实证据。边权只来自去重后的事件/证据，不按消息数量刷高。</div></div>
  </aside>
</div>
<script>
const DATA = __DATA__;
const NS = "http://www.w3.org/2000/svg";
const COLORS = {person:"#4e79a7",topic:"#f28e2b",event:"#59a14f",memory:"#e15759",source:"#76b7b2",assistant:"#9c755f"};
const TYPE_LABEL = {person:"人物",topic:"话题",event:"会话事件",memory:"记忆",source:"来源",assistant:"本机助手"};
const REL_LABEL = Object.assign({}, DATA.legend.relations || {});
const svg = document.getElementById("graph");
const wrap = document.getElementById("canvasWrap");
const detail = document.getElementById("detail");
const stats = document.getElementById("stats");
const empty = document.getElementById("empty");
const groupFilter = document.getElementById("groupFilter");
const relationFilter = document.getElementById("relationFilter");
const search = document.getElementById("search");
const labelsAlways = document.getElementById("labelsAlways");

const state = {
  nodes: DATA.nodes.map((n, i) => ({...n, x: 0, y: 0, vx: 0, vy: 0, r: 8, index: i})),
  edges: DATA.edges.map((e, i) => ({...e, index: i})),
  transform: {x: 0, y: 0, k: 1},
  selected: null,
  selectedType: null,
  hover: null,
  drag: null,
  pan: null,
  filterGroup: "",
  filterRelation: "",
  query: "",
  ticks: 0,
  width: 900,
  height: 700,
};

function hashNumber(value) {
  let h = 2166136261 >>> 0;
  const text = String(value);
  for (let i = 0; i < text.length; i++) { h ^= text.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}
function nodeById(id) { return state.nodes.find(n => n.id === id); }
function edgeKey(edge) { return edge.source + "|" + edge.target + "|" + edge.relation; }
function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function measure() {
  const rect = wrap.getBoundingClientRect();
  state.width = Math.max(320, rect.width);
  state.height = Math.max(320, rect.height);
  svg.setAttribute("viewBox", `0 0 ${state.width} ${state.height}`);
}

function initPositions() {
  const n = state.nodes.length || 1;
  const radius = Math.min(state.width, state.height) * 0.33;
  state.nodes.forEach((node, i) => {
    const angle = (i / n) * Math.PI * 2;
    const jitter = (hashNumber(node.id) % 100) / 100 * 0.28;
    node.x = state.width / 2 + Math.cos(angle) * radius * (0.78 + jitter);
    node.y = state.height / 2 + Math.sin(angle) * radius * (0.78 + jitter);
    node.vx = 0; node.vy = 0;
  });
}

function degreeMap() {
  const map = new Map();
  state.edges.forEach(edge => {
    map.set(edge.source, (map.get(edge.source) || 0) + 1);
    map.set(edge.target, (map.get(edge.target) || 0) + 1);
  });
  state.nodes.forEach(node => { node.degree = map.get(node.id) || node.degree || 0; });
}

function tick() {
  const nodes = state.nodes;
  const n = nodes.length || 1;
  const cx = state.width / 2, cy = state.height / 2;
  const repulse = Math.max(1600, Math.min(7000, 60000 / Math.max(8, n)));
  for (let i = 0; i < nodes.length; i++) {
    const a = nodes[i];
    for (let j = i + 1; j < nodes.length; j++) {
      const b = nodes[j];
      let dx = b.x - a.x, dy = b.y - a.y;
      let d2 = dx * dx + dy * dy;
      if (d2 < 1) { dx = 0.7; dy = 0.7; d2 = 1; }
      if (d2 > 90000) continue;
      const d = Math.sqrt(d2);
      const separation = (a.r || 10) + (b.r || 10) + 22;
      const force = repulse / d2 + Math.max(0, separation - d) * 0.16;
      const fx = dx / d * force, fy = dy / d * force;
      a.vx -= fx; a.vy -= fy; b.vx += fx; b.vy += fy;
    }
  }
  state.edges.forEach(edge => {
    const a = nodeById(edge.source), b = nodeById(edge.target);
    if (!a || !b) return;
    const dx = b.x - a.x, dy = b.y - a.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 1;
    const target = 125;
    const force = (d - target) * 0.0038 / Math.sqrt(Math.max(1, Math.min(a.degree || 1, b.degree || 1)));
    const fx = dx / d * force, fy = dy / d * force;
    a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
  });
  nodes.forEach(node => {
    node.vx += (cx - node.x) * 0.0007;
    node.vy += (cy - node.y) * 0.0007;
    node.vx *= 0.84; node.vy *= 0.84;
    node.x += node.vx; node.y += node.vy;
    const pad = 44;
    node.x = Math.max(pad, Math.min(state.width - pad, node.x));
    node.y = Math.max(pad, Math.min(state.height - pad, node.y));
  });
  state.ticks++;
}

function radiusFor(node) { return 6.5 + Math.min(12, Math.sqrt(Math.max(1, node.degree || 1)) * 2.1); }
function edgeWidth(edge) { return 0.8 + Math.min(4.2, Math.log2(Math.max(1, edge.weight || 1) + 1)); }

function draw() {
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  degreeMap();
  state.nodes.forEach(node => { node.r = radiusFor(node); });
  const root = document.createElementNS(NS, "g");
  root.setAttribute("id", "viewport");
  svg.appendChild(root);
  const edgeLayer = document.createElementNS(NS, "g");
  const nodeLayer = document.createElementNS(NS, "g");
  root.appendChild(edgeLayer); root.appendChild(nodeLayer);
  const visibleEdges = state.edges.filter(edge => edgeVisible(edge));
  const visibleNodes = state.nodes.filter(node => nodeVisible(node, visibleEdges));
  const visibleIds = new Set(visibleNodes.map(node => node.id));

  visibleEdges.forEach(edge => {
    if (!visibleIds.has(edge.source) || !visibleIds.has(edge.target)) return;
    const a = nodeById(edge.source), b = nodeById(edge.target);
    if (!a || !b) return;
    const group = document.createElementNS(NS, "g");
    group.setAttribute("class", "edge-group");
    const path = document.createElementNS(NS, "path");
    path.setAttribute("class", "edge" + (edge.co_present ? " co_present" : "") + (edge.direct ? " direct" : ""));
    path.setAttribute("stroke-width", String(edgeWidth(edge)));
    path.setAttribute("d", `M ${a.x} ${a.y} L ${b.x} ${b.y}`);
    path.dataset.edge = String(edge.index);
    const hit = document.createElementNS(NS, "path");
    hit.setAttribute("class", "edge-hit");
    hit.setAttribute("d", path.getAttribute("d"));
    hit.dataset.edge = String(edge.index);
    hit.addEventListener("click", event => { event.stopPropagation(); selectEdge(edge); });
    group.appendChild(path); group.appendChild(hit);
    edgeLayer.appendChild(group);
  });

  const labelBoxes = [];
  visibleNodes.forEach(node => {
    const group = document.createElementNS(NS, "g");
    group.setAttribute("class", "node-group");
    group.dataset.node = node.id;
    const circle = document.createElementNS(NS, "circle");
    circle.setAttribute("class", "node" + (state.selected === node.id && state.selectedType === "node" ? " selected" : ""));
    circle.setAttribute("cx", node.x); circle.setAttribute("cy", node.y); circle.setAttribute("r", node.r);
    circle.setAttribute("fill", COLORS[node.type] || "#8a99aa");
    circle.dataset.node = node.id;
    circle.addEventListener("click", event => { event.stopPropagation(); selectNode(node); });
    circle.addEventListener("pointerdown", event => { event.stopPropagation(); startNodeDrag(event, node); });
    const label = document.createElementNS(NS, "text");
    label.setAttribute("class", "node-label");
    label.setAttribute("x", node.x); label.setAttribute("y", node.y - node.r - 4);
    label.setAttribute("text-anchor", "middle");
    const fullLabel = node.label || node.id;
    const shortLabel = /^用户[0-9]{10,}$/.test(fullLabel) ? '用户·' + fullLabel.slice(-6) : fullLabel;
    label.textContent = shortLabel.length > 18 ? shortLabel.slice(0, 17) + '…' : shortLabel;
    const tooltip = document.createElementNS(NS, 'title'); tooltip.textContent = fullLabel; circle.appendChild(tooltip);
    group.appendChild(circle); group.appendChild(label);
    nodeLayer.appendChild(group);
    node._label = label;
    node._group = group;
    node._circle = circle;
    node._labelBox = {x: node.x - label.getComputedTextLength() / 2, y: node.y - node.r - 16,
                      w: label.getComputedTextLength(), h: 14};
  });
  // Greedy label placement keeps Chinese labels from stacking on dense nodes.
  const sorted = visibleNodes.slice().sort((a, b) => (b.degree || 0) - (a.degree || 0));
  const shown = new Set();
  sorted.forEach(node => {
    const box = node._labelBox;
    const overlap = labelBoxes.some(other => !(box.x + box.w < other.x || other.x + other.w < box.x ||
                                              box.y + box.h < other.y || other.y + other.h < box.y));
    if (labelsAlways.checked || !overlap) { labelBoxes.push(box); shown.add(node.id); }
  });
  visibleNodes.forEach(node => { if (!shown.has(node.id)) node._label.classList.add("hidden"); });

  if (state.selectedType === "node" && state.selected) highlightNode(state.selected);
  applyTransform();
  updateStats(visibleNodes.length, visibleEdges.length);
}

function nodeVisible(node, edges) {
  if (!state.filterGroup && !state.filterRelation && !state.query) return true;
  if (state.filterGroup) {
    if (node.scope) { if (node.scope !== state.filterGroup) return false; }
    else if (!edges.some(edge => edge.source === node.id || edge.target === node.id)) return false;
  }
  const touched = edges.some(edge => edge.source === node.id || edge.target === node.id);
  if ((state.filterRelation) && !touched) return false;
  if (state.query) {
    const needle = state.query.toLowerCase();
    return (node.label || "").toLowerCase().includes(needle) || (node.id || "").toLowerCase().includes(needle) ||
           (node.memory_id || "").toLowerCase().includes(needle) || (node.official_id || "").includes(needle);
  }
  return true;
}
function edgeVisible(edge) {
  if (state.filterGroup && edge.scope !== state.filterGroup) return false;
  if (state.filterRelation && edge.relation !== state.filterRelation) return false;
  if (!state.filterRelation && edge.relation === 'mentions' && state.edges.some(other =>
      other.relation === 'direct_interaction' && other.source === edge.source &&
      other.target === edge.target && other.scope === edge.scope)) return false;
  return true;
}

function applyTransform() {
  const viewport = document.getElementById("viewport");
  if (viewport) viewport.setAttribute("transform", `translate(${state.transform.x} ${state.transform.y}) scale(${state.transform.k})`);
}

function updateStats(nodes, edges) {
  const counts = DATA.counts || {};
  const omitted = (counts.omitted_nodes || 0) + (counts.omitted_edges || 0);
  const sample = DATA.sampled ? " · 已按上限采样，完整数量见下" : "";
  stats.textContent = `显示 ${nodes}/${state.nodes.length} 节点，${edges}/${state.edges.length} 边；范围内共 ${counts.total_nodes_in_scope ?? state.nodes.length} 节点、${counts.total_edges_in_scope ?? state.edges.length} 边` +
    ` · 省略 ${counts.omitted_nodes ?? 0} 节点 / ${counts.omitted_edges ?? 0} 边${sample} · 缩放 ${state.transform.k.toFixed(2)}×`;
  empty.style.display = state.nodes.length ? "none" : "flex";
}

function highlightNode(id) {
  const neighbors = new Set([id]);
  state.edges.forEach(edge => { if (edge.source === id) neighbors.add(edge.target); if (edge.target === id) neighbors.add(edge.source); });
  state.nodes.forEach(node => {
    if (!node._group) return;
    const active = neighbors.has(node.id);
    node._group.style.opacity = active ? "1" : ".17";
  });
}
function clearSelection() {
  state.selected = null; state.selectedType = null;
  state.nodes.forEach(node => {
    if (node._group) node._group.style.opacity = "1";
    if (node._circle) node._circle.classList.remove("selected");
  });
  detail.innerHTML = '<div class="empty">点击节点或连线查看真实证据。边权只来自去重后的事件/证据，不按消息数量刷高。</div>';
}
function selectNode(node) {
  state.selected = node.id; state.selectedType = "node";
  state.nodes.forEach(item => item._circle && item._circle.classList.toggle("selected", item.id === node.id));
  highlightNode(node.id);
  const out = [];
  out.push(`<h3>${esc(TYPE_LABEL[node.type] || node.type)}</h3>`);
  out.push(`<div class="kv"><div class="k">节点ID</div><div>${esc(node.id)}</div>` +
           `<div class="k">标签</div><div>${esc(node.label)}</div>` +
           `<div class="k">范围</div><div>${esc(node.scope || "全局/别名")}</div>` +
           `<div class="k">连接度</div><div>${esc(node.degree)}（本图）</div></div>`);
  if (node.type === "person") {
    out.push(`<div class="kv"><div class="k">官方ID</div><div>${esc(node.official_id)}</div></div>`);
    if (node.aliases && node.aliases.length) {
      out.push(`<h3>昵称别名（有来源，不合并身份）</h3>`);
      node.aliases.forEach(alias => out.push(`<div class="pill">${esc(alias.alias)} · ${esc(alias.session)} · M${esc(alias.source_record_id)}</div>`));
    }
    out.push('<h3>提示</h3><div class="warn">昵称只是带来源的别名；不同官方ID即使同名也不会合并。直接互动只来自可观察的@/回复。</div>');
  }
  if (node.type === "memory") {
    out.push(`<h3>记忆</h3><div class="kv"><div class="k">M编号</div><div>${esc(node.memory_id || "")}</div></div>`);
  }
  const incident = state.edges.filter(edge => edge.source === node.id || edge.target === node.id);
  out.push(`<h3>相邻关系（${incident.length}，最多显示30）</h3>`);
  incident.slice(0, 30).forEach(edge => {
    const otherId = edge.source === node.id ? edge.target : edge.source;
    const other = nodeById(otherId);
    const arrow = edge.source === node.id ? "→" : "←";
    out.push(`<div class="evidence"><div><b>${esc(edge.relation_label || REL_LABEL[edge.relation] || edge.relation)}</b> ${arrow} ${esc(other ? other.label : otherId)}</div>` +
             `<div class="sub">权重 ${esc(edge.weight)}（去重事件） · ${esc(edge.scope)} · ${edge.evidence_count || 0} 条证据</div></div>`);
  });
  detail.innerHTML = out.join("");
}
function selectEdge(edge) {
  state.selected = edgeKey(edge); state.selectedType = "edge";
  const a = nodeById(edge.source), b = nodeById(edge.target);
  const title = REL_LABEL[edge.relation] || edge.relation_label || edge.relation;
  const rows = [
    `<h3>${esc(title)}</h3>`,
    `<div class="kv"><div class="k">起点</div><div>${esc(a ? a.label : edge.source)}</div>` +
    `<div class="k">终点</div><div>${esc(b ? b.label : edge.target)}</div>` +
    `<div class="k">范围</div><div>${esc(edge.scope)}</div>` +
    `<div class="k">权重</div><div>${esc(edge.weight)}（独立事件/证据，co_present按天）</div>` +
    `<div class="k">首/末见</div><div>${new Date((edge.first_seen || 0) * 1000).toLocaleDateString()} / ${new Date((edge.last_seen || 0) * 1000).toLocaleDateString()}</div></div>`,
    `<h3>证据（${edge.evidence_count || 0}，最多显示${(edge.evidence || []).length}）</h3>`,
  ];
  (edge.evidence || []).forEach(item => {
    rows.push(`<div class="evidence"><div class="ex">${esc(item.excerpt || "（原话已清理，仅保留关系）")}</div>` +
              `<div class="sub">${esc(item.memory_id)} · ${esc(item.kind)} · ${esc(item.record_kind)} · 状态 ${esc(item.state)} · ${new Date((item.at || 0) * 1000).toLocaleString()}</div></div>`);
  });
  if (edge.evidence_omitted) rows.push(`<div class="sub">另有 ${esc(edge.evidence_omitted)} 条证据未展开；可缩小范围或使用 memory.graph 继续查看。</div>`);
  if (edge.co_present) rows.push('<div class="warn">这是同场出现，不是朋友、恋人或任何关系判断。</div>');
  if (edge.direct) rows.push('<div class="warn">直接互动证据来自明确的@或引用回复；不推断亲密度。</div>');
  detail.innerHTML = rows.join("");
  state.nodes.forEach(node => { if (node._group) node._group.style.opacity = "1"; });
  [a, b].forEach(node => { if (node && node._group) node._group.style.opacity = "1"; });
}

function startNodeDrag(event, node) {
  event.preventDefault();
  const point = svgPoint(event);
  state.drag = {node, dx: node.x - point.x, dy: node.y - point.y};
  svg.setPointerCapture(event.pointerId);
}
function svgPoint(event) {
  const rect = svg.getBoundingClientRect();
  const x = (event.clientX - rect.left - state.transform.x) / state.transform.k;
  const y = (event.clientY - rect.top - state.transform.y) / state.transform.k;
  return {x, y};
}

svg.addEventListener("pointerdown", event => {
  if (event.target.closest(".node") || event.target.closest(".edge-hit")) return;
  const point = {x: event.clientX, y: event.clientY};
  state.pan = {x: point.x, y: point.y, tx: state.transform.x, ty: state.transform.y};
  svg.classList.add("panning");
  svg.setPointerCapture(event.pointerId);
});
svg.addEventListener("pointermove", event => {
  if (state.drag) {
    const point = svgPoint(event);
    state.drag.node.x = Math.max(30, Math.min(state.width - 30, point.x + state.drag.dx));
    state.drag.node.y = Math.max(30, Math.min(state.height - 30, point.y + state.drag.dy));
    draw();
  } else if (state.pan) {
    state.transform.x = state.pan.tx + (event.clientX - state.pan.x);
    state.transform.y = state.pan.ty + (event.clientY - state.pan.y);
    applyTransform();
  }
});
svg.addEventListener("pointerup", () => { state.drag = null; state.pan = null; svg.classList.remove("panning"); });
svg.addEventListener("pointercancel", () => { state.drag = null; state.pan = null; svg.classList.remove("panning"); });
svg.addEventListener("wheel", event => {
  event.preventDefault();
  const rect = svg.getBoundingClientRect();
  const px = event.clientX - rect.left, py = event.clientY - rect.top;
  const old = state.transform.k;
  const next = Math.max(0.25, Math.min(4, old * (event.deltaY < 0 ? 1.12 : 0.89)));
  state.transform.x = px - (px - state.transform.x) * (next / old);
  state.transform.y = py - (py - state.transform.y) * (next / old);
  state.transform.k = next;
  applyTransform();
  updateStats(state.nodes.length, state.edges.length);
}, {passive: false});

function populateFilters() {
  const groups = [...new Set(state.edges.map(edge => edge.scope).filter(Boolean))].sort();
  groups.forEach(scope => {
    const option = document.createElement("option");
    option.value = scope; option.textContent = scope;
    groupFilter.appendChild(option);
  });
  const relations = [...new Set(state.edges.map(edge => edge.relation))].sort();
  relations.forEach(relation => {
    const option = document.createElement("option");
    option.value = relation; option.textContent = REL_LABEL[relation] || relation;
    relationFilter.appendChild(option);
  });
}

function filterChanged() {
  state.filterGroup = groupFilter.value;
  state.filterRelation = relationFilter.value;
  state.query = search.value.trim();
  state.selected = null; state.selectedType = null;
  draw();
}
groupFilter.addEventListener("change", filterChanged);
relationFilter.addEventListener("change", filterChanged);
search.addEventListener("input", filterChanged);
labelsAlways.addEventListener("change", draw);
document.getElementById("clearFilter").addEventListener("click", () => {
  search.value = ""; groupFilter.value = ""; relationFilter.value = ""; filterChanged();
});
document.getElementById("resetView").addEventListener("click", () => {
  state.transform = {x: 0, y: 0, k: 1}; applyTransform(); updateStats(state.nodes.length, state.edges.length);
});
document.getElementById("relayout").addEventListener("click", () => { initPositions(); runLayout(); });

function runLayout() {
  const targetTicks = Math.min(360, 120 + state.nodes.length * 4);
  let last = 0;
  function frame(now) {
    if (state.ticks < targetTicks) {
      tick();
      if (now - last > 30) { draw(); last = now; }
      requestAnimationFrame(frame);
    } else {
      draw();
    }
  }
  state.ticks = 0;
  requestAnimationFrame(frame);
}

function boot() {
  measure();
  document.getElementById("scopeText").textContent = DATA.scope === "all_groups" ? "跨群（不含其他私聊）" : "当前会话";
  document.getElementById("generated").textContent = new Date((DATA.generated_at || 0) * 1000).toLocaleString();
  populateFilters();
  if (!state.nodes.length) { empty.style.display = "flex"; draw(); return; }
  initPositions();
  runLayout();
}
window.addEventListener("resize", () => { measure(); draw(); });
boot();
</script>
</body>
</html>"""
    return template.replace("__TITLE__", html.escape(title)).replace("__DATA__", payload)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export a self-contained offline memory relationship graph")
    parser.add_argument("--base", default=str(BASE_DIR), help="bot source directory (contains state/memory.sqlite3)")
    parser.add_argument("--session", default="group:1",
                        help="scope session, e.g. group:123 or private:10001 (default group:1 for all_groups)")
    parser.add_argument("--scope", choices=("current", "all_groups"), default="all_groups")
    parser.add_argument("--person-id", default="")
    parser.add_argument("--topic", default="")
    parser.add_argument("--memory-id", default="")
    parser.add_argument("--depth", type=int, choices=(1, 2), default=1)
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--out", default="memory_graph.html")
    parser.add_argument("--rebuild", action="store_true", help="rebuild graph cursor from every existing record first")
    args = parser.parse_args(argv)
    import re
    if not re.fullmatch(r"(?:group|private):[1-9][0-9]{0,19}", args.session):
        parser.error("--session must be group:<digits> or private:<digits>")
    from hakimi.memory.memory_store import store
    index = store(str(Path(args.base).resolve()))
    if args.rebuild:
        total = 0
        while True:
            count = index.rebuild_graph(limit=500, full=(total == 0))
            total += count
            if count < 500:
                break
        print(f"graph rebuilt: {total} records")
    focus_type = "person" if args.person_id else "topic" if args.topic else "memory" if args.memory_id else None
    focus_id = args.person_id or args.memory_id or None
    data = index.graph(
        args.session, (), cross_groups=args.scope == "all_groups", focus_type=focus_type,
        focus_id=focus_id, topic=args.topic, depth=args.depth, limit=args.limit, evidence_limit=5,
    )
    output = Path(args.out)
    output.write_text(render_html(data), encoding="utf-8")
    print(f"wrote {output} ({len(data.get('nodes', []))} nodes, {len(data.get('edges', []))} edges)")
    if data.get("error"):
        print(f"note: {data['error']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
