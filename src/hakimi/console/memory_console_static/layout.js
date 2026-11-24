/* Deterministic, testable force layout for the memory console graph.
 * Loaded as a browser global and as a CommonJS module in offline tests. */
(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.HakimiGraphLayout = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));

  function hashString(value) {
    let hash = 2166136261;
    const text = String(value);
    for (let i = 0; i < text.length; i += 1) {
      hash ^= text.charCodeAt(i);
      hash = Math.imul(hash, 16777619);
    }
    return hash >>> 0;
  }

  function nodeRadius(node) {
    const degree = Number(node && node.degree ? node.degree : 0);
    return node && node.type === 'assistant' ? 19 : 9 + Math.min(12, degree * 1.25);
  }

  function effectiveSpacing(spacing, count) {
    const base = Math.max(60, Number(spacing) || 110);
    // More nodes must not mean "everyone on the wall": shrink the spring length
    // smoothly while keeping the user's spacing slider meaningful.
    return clamp(base * Math.sqrt(35 / Math.max(1, count)), 38, base);
  }

  function iterationCount(base, count) {
    const requested = Math.max(40, Number(base) || 220);
    return Math.round(clamp(requested * clamp(Math.sqrt(40 / Math.max(1, count)), 0.55, 1.2), 90, requested * 1.2));
  }

  function initialPositions(nodes, width, height, pad) {
    const positions = new Map();
    const count = Math.max(1, nodes.length);
    const cx = width / 2;
    const cy = height / 2;
    const usable = Math.min(width, height) / 2 - pad;
    const ranked = nodes.map((node, index) => ({ node, index, degree: Number(node.degree || 0) }))
      .sort((a, b) => (b.degree - a.degree) || hashString(a.node.id) - hashString(b.node.id));
    const hub = ranked.length ? ranked[0].node.id : null;
    nodes.forEach((node, index) => {
      if (node.id === hub) {
        positions.set(node.id, { x: cx, y: cy });
        return;
      }
      const angle = index * 2.399963229728653;
      const radius = usable * Math.sqrt((index + 1) / count);
      const jitter = (hashString(node.id + ':' + index) % 21) - 10;
      positions.set(node.id, {
        x: clamp(cx + Math.cos(angle) * Math.max(20, radius + jitter), pad, width - pad),
        y: clamp(cy + Math.sin(angle) * Math.max(20, radius + jitter), pad, height - pad)
      });
    });
    return positions;
  }

  function normalizePinned(pinned) {
    if (pinned === undefined || pinned === null || pinned === false) return new Set();
    if (pinned instanceof Set) return new Set(pinned);
    if (Array.isArray(pinned)) return new Set(pinned.map(String));
    return new Set([String(pinned)]);
  }

  function layout(nodes, edges, options) {
    const settings = options || {};
    const width = Number(settings.width) || 1000;
    const height = Number(settings.height) || 760;
    const pad = Number(settings.pad) || 58;
    const list = Array.isArray(nodes) ? nodes.filter((node) => node && node.id) : [];
    if (!list.length) return new Map();
    const byId = new Map(list.map((node) => [String(node.id), node]));
    const links = (Array.isArray(edges) ? edges : []).filter((edge) =>
      edge && byId.has(String(edge.source)) && byId.has(String(edge.target)) &&
      String(edge.source) !== String(edge.target));
    const spacing = effectiveSpacing(settings.spacing, list.length);
    const iterations = iterationCount(settings.iterations, list.length);
    const pinned = normalizePinned(settings.pinned);
    const centered = normalizePinned(settings.centerPinned);
    centered.forEach((id) => pinned.add(id));
    const positions = new Map();
    const initial = settings.positions instanceof Map ? settings.positions : null;
    const fallback = initialPositions(list, width, height, pad);
    list.forEach((node) => {
      const prior = initial && initial.get(node.id);
      positions.set(node.id, {
        x: prior && Number.isFinite(prior.x) ? Number(prior.x) : fallback.get(node.id).x,
        y: prior && Number.isFinite(prior.y) ? Number(prior.y) : fallback.get(node.id).y
      });
    });
    const cx = width / 2;
    const cy = height / 2;
    const degrees = new Map(list.map((node) => [String(node.id), 0]));
    links.forEach((edge) => {
      degrees.set(String(edge.source), (degrees.get(String(edge.source)) || 0) + 1);
      degrees.set(String(edge.target), (degrees.get(String(edge.target)) || 0) + 1);
    });
    const maxDegree = Math.max(1, ...degrees.values());
    list.forEach((node) => {
      if (centered.has(node.id)) positions.set(node.id, { x: cx, y: cy });
    });
    const damping = 0.72;
    const maxStep = Math.max(8, spacing * 0.32);

    for (let iteration = 0; iteration < iterations; iteration += 1) {
      const forces = new Map(list.map((node) => [node.id, { x: 0, y: 0 }]));
      for (let i = 0; i < list.length; i += 1) {
        const left = list[i];
        const lp = positions.get(left.id);
        for (let j = i + 1; j < list.length; j += 1) {
          const right = list[j];
          const rp = positions.get(right.id);
          let dx = rp.x - lp.x;
          let dy = rp.y - lp.y;
          let distance = Math.hypot(dx, dy);
          if (distance < 0.5) {
            const seed = hashString(left.id + '|' + right.id);
            dx = 0.6 + (seed % 13) / 20;
            dy = 0.4 + (seed % 7) / 20;
            distance = Math.hypot(dx, dy);
          }
          const repulsion = (spacing * spacing * 0.92) / Math.max(24, distance);
          const fx = (dx / distance) * repulsion;
          const fy = (dy / distance) * repulsion;
          const lf = forces.get(left.id);
          const rf = forces.get(right.id);
          lf.x -= fx; lf.y -= fy; rf.x += fx; rf.y += fy;
          const minimum = nodeRadius(left) + nodeRadius(right) + 8;
          if (distance < minimum) {
            const push = (minimum - distance) * 0.42;
            lf.x -= (dx / distance) * push; lf.y -= (dy / distance) * push;
            rf.x += (dx / distance) * push; rf.y += (dy / distance) * push;
          }
        }
      }
      links.forEach((edge) => {
        const left = positions.get(String(edge.source));
        const right = positions.get(String(edge.target));
        const dx = right.x - left.x;
        const dy = right.y - left.y;
        const distance = Math.max(1, Math.hypot(dx, dy));
        const spring = (distance - spacing * 1.08) * 0.013;
        const fx = (dx / distance) * spring;
        const fy = (dy / distance) * spring;
        const lf = forces.get(String(edge.source));
        const rf = forces.get(String(edge.target));
        lf.x += fx; lf.y += fy; rf.x -= fx; rf.y -= fy;
      });
      list.forEach((node) => {
        const position = positions.get(node.id);
        if (pinned.has(node.id)) {
          return;
        }
        const force = forces.get(node.id);
        const degree = degrees.get(node.id) || 0;
        const gravity = 0.0065 + 0.0025 * (degree / maxDegree);
        const radius = nodeRadius(node);
        const inner = pad + radius;
        force.x += (cx - position.x) * gravity;
        force.y += (cy - position.y) * gravity;
        // Soft walls: push inward when close to the edge instead of clamping
        // every step (which made nodes pile up along the four boundaries).
        if (position.x < inner) force.x += (inner - position.x) * 0.18;
        if (position.x > width - inner) force.x -= (position.x - (width - inner)) * 0.18;
        if (position.y < inner) force.y += (inner - position.y) * 0.18;
        if (position.y > height - inner) force.y -= (position.y - (height - inner)) * 0.18;
        position.x += clamp(force.x, -maxStep, maxStep) * damping;
        position.y += clamp(force.y, -maxStep, maxStep) * damping;
      });
    }

    // Collision separation with pinned nodes immovable.  This runs after the
    // force phase so a high-degree hub cannot shove every satellite to a wall.
    for (let pass = 0; pass < 120; pass += 1) {
      let moved = false;
      for (let i = 0; i < list.length; i += 1) {
        const left = list[i];
        const lp = positions.get(left.id);
        for (let j = i + 1; j < list.length; j += 1) {
          const right = list[j];
          const rp = positions.get(right.id);
          let dx = rp.x - lp.x;
          let dy = rp.y - lp.y;
          let distance = Math.hypot(dx, dy);
          const minimum = nodeRadius(left) + nodeRadius(right) + 6;
          if (distance >= minimum) continue;
          if (distance < 0.1) {
            const seed = hashString(left.id + '|' + right.id);
            dx = 0.7 + (seed % 5) / 10;
            dy = 0.5 + (seed % 3) / 10;
            distance = Math.hypot(dx, dy);
          }
          const leftFixed = pinned.has(left.id);
          const rightFixed = pinned.has(right.id);
          if (leftFixed && rightFixed) continue;
          const push = (minimum - distance) + 0.5;
          if (leftFixed) {
            rp.x = clamp(rp.x + (dx / distance) * push, pad + nodeRadius(right), width - pad - nodeRadius(right));
            rp.y = clamp(rp.y + (dy / distance) * push, pad + nodeRadius(right), height - pad - nodeRadius(right));
          } else if (rightFixed) {
            lp.x = clamp(lp.x - (dx / distance) * push, pad + nodeRadius(left), width - pad - nodeRadius(left));
            lp.y = clamp(lp.y - (dy / distance) * push, pad + nodeRadius(left), height - pad - nodeRadius(left));
          } else {
            const half = push / 2;
            lp.x -= (dx / distance) * half; lp.y -= (dy / distance) * half;
            rp.x += (dx / distance) * half; rp.y += (dy / distance) * half;
          }
          moved = true;
        }
      }
      if (!moved) break;
    }

    // Fit non-pinned nodes into the safe interior; never translate a pinned
    // node.  With no pin, recenter as a whole so the graph occupies the middle.
    const free = list.filter((node) => !pinned.has(node.id));
    if (free.length) {
      if (!pinned.size) {
        const centroid = free.reduce((value, node) => {
          const position = positions.get(node.id);
          value.x += position.x / free.length;
          value.y += position.y / free.length;
          return value;
        }, { x: 0, y: 0 });
        free.forEach((node) => {
          const position = positions.get(node.id);
          position.x += cx - centroid.x;
          position.y += cy - centroid.y;
        });
      }
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      free.forEach((node) => {
        const position = positions.get(node.id);
        const radius = nodeRadius(node);
        minX = Math.min(minX, position.x - radius); minY = Math.min(minY, position.y - radius);
        maxX = Math.max(maxX, position.x + radius); maxY = Math.max(maxY, position.y + radius);
      });
      const innerLeft = pad + 8, innerTop = pad + 8;
      const innerRight = width - pad - 8, innerBottom = height - pad - 8;
      const spanX = Math.max(1, maxX - minX);
      const spanY = Math.max(1, maxY - minY);
      const fit = Math.min(1, (innerRight - innerLeft) / spanX, (innerBottom - innerTop) / spanY);
      if (fit < 1) {
        free.forEach((node) => {
          const position = positions.get(node.id);
          position.x = cx + (position.x - cx) * fit;
          position.y = cy + (position.y - cy) * fit;
        });
      }
      free.forEach((node) => {
        const position = positions.get(node.id);
        const radius = nodeRadius(node);
        position.x = clamp(position.x, pad + radius, width - pad - radius);
        position.y = clamp(position.y, pad + radius, height - pad - radius);
      });
    }

    // Fitting can scale two dense hubs together.  A short final separation pass
    // (after all scaling) guarantees no visible circles overlap.
    for (let pass = 0; pass < 80; pass += 1) {
      let moved = false;
      for (let i = 0; i < list.length; i += 1) {
        const left = list[i];
        const lp = positions.get(left.id);
        for (let j = i + 1; j < list.length; j += 1) {
          const right = list[j];
          const rp = positions.get(right.id);
          let dx = rp.x - lp.x;
          let dy = rp.y - lp.y;
          let distance = Math.hypot(dx, dy);
          const minimum = nodeRadius(left) + nodeRadius(right) + 6;
          if (distance >= minimum) continue;
          if (distance < 0.1) {
            const seed = hashString(left.id + '|' + right.id);
            dx = 0.7 + (seed % 5) / 10;
            dy = 0.5 + (seed % 3) / 10;
            distance = Math.hypot(dx, dy);
          }
          const leftFixed = pinned.has(left.id);
          const rightFixed = pinned.has(right.id);
          if (leftFixed && rightFixed) continue;
          const push = (minimum - distance) + 0.5;
          if (leftFixed) {
            rp.x = clamp(rp.x + (dx / distance) * push, pad + nodeRadius(right), width - pad - nodeRadius(right));
            rp.y = clamp(rp.y + (dy / distance) * push, pad + nodeRadius(right), height - pad - nodeRadius(right));
          } else if (rightFixed) {
            lp.x = clamp(lp.x - (dx / distance) * push, pad + nodeRadius(left), width - pad - nodeRadius(left));
            lp.y = clamp(lp.y - (dy / distance) * push, pad + nodeRadius(left), height - pad - nodeRadius(left));
          } else {
            const half = push / 2;
            lp.x -= (dx / distance) * half; lp.y -= (dy / distance) * half;
            rp.x += (dx / distance) * half; rp.y += (dy / distance) * half;
          }
          moved = true;
        }
      }
      if (!moved) break;
    }
    return positions;
  }

  return {
    WORLD: { width: 1000, height: 760, pad: 58 },
    clamp, hashString, nodeRadius, effectiveSpacing, iterationCount, initialPositions, layout
  };
});
