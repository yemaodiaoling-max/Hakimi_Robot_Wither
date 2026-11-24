'use strict';
const assert = require('assert');
const L = require('../src/hakimi/console/memory_console_static/layout.js');

const W = 1000, H = 760, PAD = 58;

function fixture(count, kind) {
  const nodes = [];
  const edges = [];
  for (let i = 0; i < count; i += 1) {
    nodes.push({ id: 'p' + i, type: 'person', degree: 0, label: '用户' + i });
  }
  if (kind === 'star') {
    nodes[0].type = 'assistant';
    for (let i = 1; i < count; i += 1) {
      nodes[0].degree += 1;
      nodes[i].degree += 1;
      edges.push({ source: 'p0', target: 'p' + i });
    }
  } else if (kind === 'clustered') {
    // A few dense centres plus a high-degree hub, without randomness.
    const centres = [0, 1, 2];
    for (let i = 3; i < count; i += 1) {
      const centre = centres[i % centres.length];
      edges.push({ source: 'p' + centre, target: 'p' + i });
      nodes[centre].degree += 1;
      nodes[i].degree += 1;
      if (i % 4 === 0) {
        edges.push({ source: 'p' + (i - 1), target: 'p' + i });
        nodes[i - 1].degree += 1;
        nodes[i].degree += 1;
      }
    }
  } else {
    for (let i = 1; i < count; i += 1) {
      edges.push({ source: 'p' + (i - 1), target: 'p' + i });
      nodes[i - 1].degree += 1;
      nodes[i].degree += 1;
    }
  }
  return { nodes, edges };
}

function audit(nodes, positions) {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  let boundary = 0;
  let collisions = 0;
  for (const [id, point] of positions) {
    assert(Number.isFinite(point.x) && Number.isFinite(point.y), 'finite position ' + id);
    assert(point.x >= PAD - 0.01 && point.x <= W - PAD + 0.01, 'x inside hard pad ' + id);
    assert(point.y >= PAD - 0.01 && point.y <= H - PAD + 0.01, 'y inside hard pad ' + id);
    if (point.x <= PAD + 10 || point.x >= W - PAD - 10 || point.y <= PAD + 10 || point.y >= H - PAD - 10) {
      boundary += 1;
    }
  }
  const entries = Array.from(positions.entries());
  for (let i = 0; i < entries.length; i += 1) {
    for (let j = i + 1; j < entries.length; j += 1) {
      const a = entries[i][1], b = entries[j][1];
      const left = byId.get(entries[i][0]), right = byId.get(entries[j][0]);
      const gap = Math.hypot(a.x - b.x, a.y - b.y) - L.nodeRadius(left) - L.nodeRadius(right);
      if (gap < -1.0) collisions += 1;
    }
  }
  return { boundary, collisions, entries };
}

for (const count of [20, 35, 80, 160]) {
  for (const kind of ['star', 'clustered', 'chain']) {
    const { nodes, edges } = fixture(count, kind);
    const first = L.layout(nodes, edges, { width: W, height: H, pad: PAD, spacing: 110, iterations: 220 });
    const second = L.layout(nodes, edges, { width: W, height: H, pad: PAD, spacing: 110, iterations: 220 });
    for (const [id, point] of first) {
      const again = second.get(id);
      assert(Math.abs(point.x - again.x) < 1e-9 && Math.abs(point.y - again.y) < 1e-9,
        'layout must be deterministic for ' + id);
    }
    const result = audit(nodes, first);
    assert(result.collisions === 0, kind + ' ' + count + ' has ' + result.collisions + ' collisions');
    assert(result.boundary / count <= 0.08,
      kind + ' ' + count + ' boundary fraction ' + (result.boundary / count));
    const xs = result.entries.map((entry) => entry[1].x);
    const ys = result.entries.map((entry) => entry[1].y);
    const spanX = Math.max(...xs) - Math.min(...xs);
    const spanY = Math.max(...ys) - Math.min(...ys);
    assert(spanX > (kind === 'star' ? 220 : 120), kind + ' ' + count + ' uses horizontal centre space');
    assert(spanY > (kind === 'star' ? 220 : 120), kind + ' ' + count + ' uses vertical centre space');
  }
}

// A user-dragged pin keeps its exact coordinates; focus/hakimi can be centred.
const { nodes, edges } = fixture(35, 'star');
const positions = new Map();
nodes.forEach((node, index) => positions.set(node.id, {
  x: 120 + (index % 7) * 90,
  y: 120 + Math.floor(index / 7) * 100,
}));
const pinnedId = 'p9';
const before = { ...positions.get(pinnedId) };
const pinned = L.layout(nodes, edges, {
  width: W, height: H, pad: PAD, spacing: 110, iterations: 220,
  positions, pinned: [pinnedId],
});
const after = pinned.get(pinnedId);
assert(Math.abs(before.x - after.x) < 1e-9 && Math.abs(before.y - after.y) < 1e-9,
  'dragged pin must stay exactly where the user placed it');

const centred = L.layout(nodes, edges, {
  width: W, height: H, pad: PAD, spacing: 110, iterations: 220,
  positions: new Map(), centerPinned: ['p0'],
});
assert(Math.abs(centred.get('p0').x - W / 2) < 1e-9 && Math.abs(centred.get('p0').y - H / 2) < 1e-9,
  'centred focus node stays at the layout centre');

// User spacing remains meaningful and large graphs use smaller adaptive spacing.
assert(L.effectiveSpacing(110, 20) > L.effectiveSpacing(110, 160));
assert(L.iterationCount(220, 160) < L.iterationCount(220, 20));

// The real app.js layoutGraph must update the existing positions Map in place:
// an external holder auditing that Map must see the fitted result, not stale
// pre-layout coordinates (the old code replaced state.positions).
const fs = require('fs');
const vm = require('vm');
const appSource = fs.readFileSync(require('path').join(__dirname, '..', 'src', 'hakimi', 'console', 'memory_console_static', 'app.js'), 'utf8');
const appStart = appSource.indexOf('  function layoutGraph(');
const appEnd = appSource.indexOf('  function renderGraph()', appStart);
assert(appStart >= 0 && appEnd > appStart, 'app.js layoutGraph must exist');
for (const count of [35, 80, 120, 160]) {
  const appNodes = Array.from({ length: count }, (_, i) => ({
    id: i ? 'p' + i : 'assistant:hakimi', type: i ? 'person' : 'assistant', degree: i ? 1 : count - 1
  }));
  const appEdges = appNodes.slice(1).map((n) => ({ source: appNodes[0].id, target: n.id }));
  const appPositions = new Map(appNodes.map((node, i) => {
    const angle = i * 2.3999632297, radius = 25 * Math.sqrt(i);
    return [node.id, { x: 500 + radius * Math.cos(angle), y: 380 + radius * Math.sin(angle) }];
  }));
  const context = {
    state: { graph: { nodes: appNodes, edges: appEdges }, positions: appPositions, spacing: 110,
             userMoved: new Set(), focus: null },
    WORLD: { width: W, height: H, pad: PAD }, visibleEdges: () => appEdges,
    clamp: (value, low, high) => Math.max(low, Math.min(high, value)), Math, Map, Set
  };
  vm.createContext(context);
  context.window = context;
  vm.runInContext(fs.readFileSync(require('path').join(__dirname, '..', 'src', 'hakimi', 'console', 'memory_console_static', 'layout.js'), 'utf8'), context);
  vm.runInContext(appSource.slice(appStart, appEnd) + '\nlayoutGraph(260);', context);
  const appAudit = audit(appNodes, appPositions);
  assert.strictEqual(appAudit.collisions, 0, 'app layout ' + count + ' has ' + appAudit.collisions + ' collisions');
  assert(appAudit.boundary / count <= 0.08, 'app layout ' + count + ' boundary fraction too high');
}

console.log('graph layout tests ok');
