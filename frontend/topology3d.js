/* AEGIS dependency topology — spatial view, no dependencies.
 *
 * Why hand-rolled rather than three.js: the frontend has no build step and
 * the demo has to run on a laptop with no network. A CDN <script> would
 * trade the offline guarantee for a scene graph this needs almost none of —
 * ten nodes, twenty edges, one orbiting camera, flat-shaded quads. So the
 * projection is ~40 lines of maths on a 2D canvas: it costs no bytes we
 * don't already ship, it cannot fail to load, and it stays at 60fps on
 * integrated graphics.
 *
 * Spatial representation earns its place here specifically because
 * dependency depth is the thing the picture has to communicate: a rollback's
 * cost is how far the failure travels, and depth reads as depth.
 */

(function (global) {
  "use strict";

  var TAU = Math.PI * 2;

  function mount(canvas, options) {
    var opts = options || {};
    var ctx = canvas.getContext && canvas.getContext("2d");
    if (!ctx) return null;

    var reduced = global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches;

    var view = {
      nodes: [],          // {id, x, y, z, role, depth, sx, sy, scale}
      edges: [],          // {a, b}
      yaw: -0.5,
      pitch: -0.24,
      spin: reduced ? 0 : 0.055,
      dragging: false,
      last: null,
      selected: null,
      hover: null,
      visible: true,
      dirty: true,        // something changed that the painted frame does not show
      raf: 0,
      dpr: 1,
    };

    var COLORS = {
      base: "#2a3242",
      idle: "#6b7484",
      text: "#e6e9ef",
      changed: "#5b8cff",
      direct: "#ff5c5c",
      transitive: "#ffb454",
      tolerant: "#3ddc97",
    };

    /* ── layout ───────────────────────────────────────────────────────
     * Depth on the z axis (how many hops from the changed service), angle
     * spread within a depth ring. A ring layout keeps every node visible
     * from any camera angle, which a force layout does not.
     */
    function layout(nodes, edges, root) {
      var reverse = {};
      nodes.forEach(function (id) { reverse[id] = []; });
      edges.forEach(function (e) { if (reverse[e.b]) reverse[e.b].push(e.a); });

      var depth = {}, queue = [root], seen = {};
      depth[root] = 0; seen[root] = true;
      while (queue.length) {
        var cur = queue.shift();
        (reverse[cur] || []).forEach(function (dep) {
          if (seen[dep]) return;
          seen[dep] = true;
          depth[dep] = depth[cur] + 1;
          queue.push(dep);
        });
      }
      var maxDepth = 0;
      nodes.forEach(function (id) {
        if (depth[id] === undefined) depth[id] = 1;   // unreachable: park it mid-field
        if (depth[id] > maxDepth) maxDepth = depth[id];
      });

      var rings = {};
      nodes.forEach(function (id) { (rings[depth[id]] = rings[depth[id]] || []).push(id); });

      var placed = [];
      Object.keys(rings).forEach(function (key) {
        var ring = rings[key].slice().sort();
        var d = Number(key);
        ring.forEach(function (id, i) {
          var angle = (i / ring.length) * TAU + d * 0.6;
          var radius = d === 0 ? 0 : 0.55 + d * 0.36;
          placed.push({
            id: id,
            x: Math.cos(angle) * radius,
            y: Math.sin(angle) * radius * 0.62,
            z: -1.1 + (d / Math.max(1, maxDepth)) * 2.2,
            depth: d,
            role: "idle",
            sx: 0, sy: 0, scale: 1,
          });
        });
      });
      return placed;
    }

    function resize() {
      var rect = canvas.getBoundingClientRect();
      var dpr = Math.min(global.devicePixelRatio || 1, 2);
      var width = Math.max(1, Math.round(rect.width * dpr));
      var height = Math.max(1, Math.round(rect.height * dpr));
      // Assigning to canvas.width clears the backing store even when the
      // value is unchanged, so a resize handler that fires on every scroll
      // (some mobile browsers) would blank the view once per event.
      if (width === canvas.width && height === canvas.height && dpr === view.dpr) return;
      view.dpr = dpr;
      canvas.width = width;
      canvas.height = height;
      view.dirty = true;
      draw();
    }

    function project(node) {
      var cy = Math.cos(view.yaw), sy = Math.sin(view.yaw);
      var cp = Math.cos(view.pitch), sp = Math.sin(view.pitch);
      var x = node.x * cy - node.z * sy;
      var z = node.x * sy + node.z * cy;
      var y = node.y * cp - z * sp;
      var zz = node.y * sp + z * cp;
      var persp = 3.1 / (3.1 + zz);
      return { x: x * persp, y: y * persp, s: persp, z: zz };
    }

    function draw() {
      var w = canvas.width, h = canvas.height, dpr = view.dpr;
      ctx.clearRect(0, 0, w, h);
      var cx = w / 2, cyy = h / 2;
      var unit = Math.min(w, h) * 0.34;

      view.nodes.forEach(function (n) {
        var p = project(n);
        n.sx = cx + p.x * unit;
        n.sy = cyy + p.y * unit;
        n.scale = p.s;
        n.zz = p.z;
      });

      var byId = {};
      view.nodes.forEach(function (n) { byId[n.id] = n; });

      /* edges first, dimmed unless they carry the propagation */
      ctx.lineCap = "round";
      view.edges.forEach(function (e) {
        var a = byId[e.a], b = byId[e.b];
        if (!a || !b) return;
        var carries = a.role !== "idle" && a.role !== "tolerant" && b.role !== "idle";
        ctx.strokeStyle = carries ? (a.role === "direct" ? COLORS.direct : COLORS.transitive) : COLORS.base;
        ctx.globalAlpha = carries ? 0.75 : 0.4;
        ctx.lineWidth = (carries ? 2 : 1.1) * dpr;
        ctx.beginPath();
        ctx.moveTo(b.sx, b.sy);
        ctx.lineTo(a.sx, a.sy);
        ctx.stroke();
      });
      ctx.globalAlpha = 1;

      /* nodes back to front, so nearer plates occlude further ones */
      var depthOrder = view.nodes.slice().sort(function (a, b) { return b.zz - a.zz; });
      depthOrder.forEach(function (n) {
        var accent = COLORS[n.role] || COLORS.idle;
        var r = 7 * n.scale * dpr;
        var isSel = view.selected === n.id;
        var isHover = view.hover === n.id;
        n.r = r;

        if (n.role !== "idle") {
          ctx.beginPath();
          ctx.arc(n.sx, n.sy, r * 2.6, 0, TAU);
          ctx.fillStyle = accent;
          ctx.globalAlpha = 0.12;
          ctx.fill();
          ctx.globalAlpha = 1;
        }

        ctx.beginPath();
        ctx.arc(n.sx, n.sy, r, 0, TAU);
        ctx.fillStyle = n.role === "idle" ? "#12161e" : accent;
        ctx.fill();
        ctx.lineWidth = (isSel || isHover ? 2.4 : 1.2) * dpr;
        ctx.strokeStyle = isSel ? COLORS.text : accent;
        ctx.stroke();
      });

      /* Labels in a second pass, nearest first, skipping any that would land
       * on one already drawn. At some camera angles two nodes project close
       * enough together that their names overlap into an unreadable smear --
       * and the names are the whole point of the picture. Priority goes to
       * the selected node and to anything carrying the failure, so the ones
       * that get dropped are always the ones that matter least. */
      var placed = [];
      var labelOrder = depthOrder.slice().reverse();          // nearest first
      labelOrder.sort(function (a, b) {
        return labelPriority(b) - labelPriority(a);           // stable enough
      });
      labelOrder.forEach(function (n) {
        var isSel = view.selected === n.id;
        var size = 11.5 * Math.max(0.8, n.scale) * dpr;
        ctx.font = size + 'px ui-monospace, "SF Mono", Menlo, monospace';
        var width = ctx.measureText(n.id).width;
        // Padded: two labels that merely touch are as unreadable as two that
        // overlap, and at some angles they land a pixel apart.
        var padX = 5 * dpr, padY = 3 * dpr;
        var box = {
          x0: n.sx - width / 2 - padX, x1: n.sx + width / 2 + padX,
          y0: n.sy + n.r + 4 * dpr - padY, y1: n.sy + n.r + 6 * dpr + size + padY,
        };
        var collides = placed.some(function (other) {
          return box.x0 < other.x1 && box.x1 > other.x0 && box.y0 < other.y1 && box.y1 > other.y0;
        });
        if (collides && !isSel) return;
        placed.push(box);

        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillStyle = n.role === "idle" && !isSel ? COLORS.idle : COLORS.text;
        ctx.fillText(n.id, n.sx, n.sy + n.r + 6 * dpr);
      });

      view.dirty = false;
    }

    /* Which labels survive a collision: the selected node first, then
     * anything on the failure path, then nearer nodes. */
    function labelPriority(node) {
      if (view.selected === node.id) return 100;
      if (node.role === "direct") return 40;
      if (node.role === "changed") return 38;
      if (node.role === "transitive") return 30;
      if (node.role === "tolerant") return 20;
      return node.scale;                                   // nearer wins
    }

    function frame(now) {
      view.last_tick = now || 0;
      // A canvas nobody is looking at should not hold the main thread. The
      // loop stays alive so the view is correct the instant it scrolls back
      // into sight, but off-screen it neither advances the camera nor
      // repaints -- previously it stopped spinning and kept drawing sixty
      // identical frames a second, which is most of the cost.
      if (view.visible) {
        if (!view.dragging && view.spin) { view.yaw += view.spin * 0.016; view.dirty = true; }
        if (view.dirty) draw();
      }
      view.raf = global.requestAnimationFrame(frame);
    }

    /* ── interaction ─────────────────────────────────────────────────── */

    function pick(event) {
      var rect = canvas.getBoundingClientRect();
      var px = (event.clientX - rect.left) * view.dpr;
      var py = (event.clientY - rect.top) * view.dpr;
      var best = null, bestDist = 26 * view.dpr;
      view.nodes.forEach(function (n) {
        var d = Math.hypot(n.sx - px, n.sy - py);
        if (d < bestDist) { bestDist = d; best = n; }
      });
      return best;
    }

    function onDown(event) {
      view.dragging = true;
      view.last = { x: event.clientX, y: event.clientY, moved: 0 };
      canvas.setPointerCapture && canvas.setPointerCapture(event.pointerId);
    }

    function onMove(event) {
      if (!view.dragging) {
        var hit = pick(event);
        var next = hit ? hit.id : null;
        if (next !== view.hover) {
          view.hover = next;
          view.dirty = true;
          canvas.style.cursor = next ? "pointer" : "grab";
        }
        return;
      }
      var dx = event.clientX - view.last.x;
      var dy = event.clientY - view.last.y;
      view.last.moved += Math.abs(dx) + Math.abs(dy);
      view.yaw += dx * 0.008;
      view.pitch = Math.max(-1.1, Math.min(1.1, view.pitch + dy * 0.006));
      view.last.x = event.clientX;
      view.last.y = event.clientY;
      view.dirty = true;
    }

    function onUp(event) {
      var wasDrag = view.last && view.last.moved > 6;
      view.dragging = false;
      if (!wasDrag) {
        var hit = pick(event);
        if (hit && opts.onSelect) opts.onSelect(hit.id);
      }
    }

    function onLeave() {
      view.dragging = false;
      view.hover = null;
      view.dirty = true;
    }

    function onKey(event) {
      if (event.key === "ArrowLeft") { view.yaw -= 0.12; view.dirty = true; event.preventDefault(); }
      else if (event.key === "ArrowRight") { view.yaw += 0.12; view.dirty = true; event.preventDefault(); }
      else if (event.key === "ArrowUp") { view.pitch = Math.max(-1.1, view.pitch - 0.08); view.dirty = true; event.preventDefault(); }
      else if (event.key === "ArrowDown") { view.pitch = Math.min(1.1, view.pitch + 0.08); view.dirty = true; event.preventDefault(); }
    }

    canvas.style.cursor = "grab";
    canvas.tabIndex = 0;
    canvas.addEventListener("pointerdown", onDown);
    canvas.addEventListener("pointermove", onMove);
    canvas.addEventListener("pointerup", onUp);
    canvas.addEventListener("pointerleave", onLeave);
    canvas.addEventListener("keydown", onKey);
    global.addEventListener("resize", resize);

    var observer = null;
    if (global.IntersectionObserver) {
      observer = new global.IntersectionObserver(function (entries) {
        var next = entries[0].isIntersecting;
        if (next && !view.visible) view.dirty = true;   // repaint on return
        view.visible = next;
      }, { threshold: 0.05 });
      observer.observe(canvas);
    }

    // The canvas is sized by CSS inside a responsive grid, so it can change
    // size without the window doing so -- the readout column wrapping under
    // it, for instance. A window-resize listener alone leaves the backing
    // store stale and the picture stretched.
    var sizeObserver = null;
    if (global.ResizeObserver) {
      sizeObserver = new global.ResizeObserver(resize);
      sizeObserver.observe(canvas);
    }

    resize();
    draw();                                  // one synchronous frame, so the
                                             // view is never blank on arrival
    view.raf = global.requestAnimationFrame(frame);
    // requestAnimationFrame is suspended in background tabs and in some
    // embedded viewers. A 24Hz guard keeps the view painted there instead of
    // leaving a black rectangle, and costs nothing while rAF is healthy.
    view.guard = global.setInterval(function () {
      var now = global.performance ? global.performance.now() : Date.now();
      if (view.visible && now - (view.last_tick || 0) > 260) {
        view.last_tick = now;
        if (!view.dragging && view.spin) view.yaw += view.spin * 0.016;
        draw();
      }
    }, 42);

    return {
      setTopology: function (nodes, edges, root) {
        view.edges = edges.slice();
        view.nodes = layout(nodes, edges, root);
        view.selected = root;
        view.dirty = true;
      },
      setRoles: function (roles) {
        view.nodes.forEach(function (n) { n.role = roles[n.id] || "idle"; });
        view.dirty = true;
      },
      select: function (id) { view.selected = id; view.dirty = true; },
      resize: resize,
      destroy: function () {
        global.cancelAnimationFrame(view.raf);
        global.clearInterval(view.guard);
        global.removeEventListener("resize", resize);
        canvas.removeEventListener("pointerdown", onDown);
        canvas.removeEventListener("pointermove", onMove);
        canvas.removeEventListener("pointerup", onUp);
        canvas.removeEventListener("pointerleave", onLeave);
        canvas.removeEventListener("keydown", onKey);
        if (observer) observer.disconnect();
        if (sizeObserver) sizeObserver.disconnect();
      },
    };
  }

  global.AegisTopology3D = { mount: mount };
})(window);
