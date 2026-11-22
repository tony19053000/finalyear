/* hive. — home page
   - smooth scroll (Lenis, optional), line reveals, magnetic buttons
   - one sticky three.js honeycomb tells the opening story, scrubbed by scroll (reversible):
       the hive -> laptops drift apart (dark) -> a thin honey network links them all -> stay local: the laptops
       pack into one big comb and only your cell works -> hand off: they spread back into the network and a job
       rides the links -> split: they pack again, a job breaks into pieces that fly out to twelve cells and come
       home as one answer -> the comb glows as one locked hive -> it sinks away
   - benchmark bars, count-up stats, live mini-UIs for the four parts
   The bees (Buzz) live in assets/js/bees.js on their own overlay; the comb shares a small API (HIVE.comb).
   (bees.js is not loaded for now; HIVE.comb stays, unused, so the bees can come back unchanged.)
   Everything degrades: no WebGL -> static SVG; reduced motion -> still frames per scroll position. */
(function () {
  'use strict';
  var doc = document, win = window;
  var HIVE = win.HIVE || {};
  var reduced = !!HIVE.reduced || !!(win.matchMedia && win.matchMedia('(prefers-reduced-motion: reduce)').matches);
  var hasIO = 'IntersectionObserver' in win;

  function safe(fn) { try { fn(); } catch (e) { if (win.console) console.warn('[hive]', e); } }
  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
  function smooth(a, b, v) { var t = clamp((v - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); }
  /* the dark look: 1 from "Most laptops sit idle" (T = 1) through the three decisions (T = 6), 0 for the
     resting hive and as it locks (T = 7). Every laptop is dark then: no honey cells, no warm light, a cool
     dim edge light. On T = 1 there is nothing else; from T = 2 the thin honey network between the laptops
     is lit (see linksOf). Through the decisions only the laptops doing a job light up (their own honey
     glow, never the shared lights; the packed comb of the decisions is dark honey and gets back some
     warm edge light).
     The warm light comes back as the hive locks. */
  function idleOf(T) { return smooth(0.45, 0.85, T) * (1 - smooth(6.3, 6.75, T)); }
  /* the decisions' framing (T = 3..6): the field spreads wider across the page, reaching in under the text
     column (dimmed there, see --veil), so the network fills more of the screen */
  function wideOf(T) { return smooth(2.45, 2.95, T) * (1 - smooth(6.3, 6.8, T)); }
  /* how far the text column's dark veil eases off: only under the short step texts (T = 4..6) */
  function veilOf(T) { return 1 - 0.3 * smooth(3.35, 3.75, T) * (1 - smooth(6.3, 6.7, T)); }
  /* the decisions (T = 4..6) all run on one packed, big comb: from stay local (T = 4) the spread network
     closes in (the links shrink and fade as neighbours touch) and the comb is framed big, filling most of
     the screen. It stays packed through the hand-off (T = 5) and the split (T = 6) into the lock (T = 7),
     which only lights it up. */
  function packOf(T) { return smooth(3.3, 3.62, T); }
  /* the network: thin honey links between every laptop, only for the network beats (T = 2..3). Drawn
     outward as T = 2 arrives (over about half a beat), drawn back in as stay local packs the comb. */
  function linksOf(T) { return clamp((T - 1.5) / 0.5, 0, 1) * (1 - packOf(T)); }
  function whenVisible(el, onShow, onHide, threshold) {
    if (!hasIO) { onShow(); return null; }
    var io = new IntersectionObserver(function (en) {
      en[0].isIntersecting ? onShow() : (onHide && onHide());
    }, { threshold: threshold == null ? 0.15 : threshold });
    io.observe(el);
    return io;
  }

  /* ================= smooth scroll ================= */
  safe(function smoothScroll() {
    if (reduced || !win.Lenis) return;
    var lenis = new win.Lenis({ lerp: 0.11, smoothWheel: true, anchors: { offset: -72 } });
    doc.documentElement.classList.add('has-lenis');
    (function raf(t) { lenis.raf(t); requestAnimationFrame(raf); })(performance.now());
    doc.addEventListener('visibilitychange', function () { doc.hidden ? lenis.stop() : lenis.start(); });
    HIVE.lenis = lenis;
  });

  /* ================= line reveals ================= */
  safe(function lines() {
    var all = doc.querySelectorAll('.lines');
    var hero = doc.querySelector('.hero-title');
    function show(el) { el.classList.add('in'); }
    if (reduced || !hasIO) { all.forEach(show); return; }
    if (hero) setTimeout(function () { show(hero); }, 150);
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { if (en.isIntersecting) { show(en.target); io.unobserve(en.target); } });
    }, { threshold: 0.3 });
    all.forEach(function (el) { if (el !== hero) io.observe(el); });
  });

  /* ================= magnetic buttons ================= */
  safe(function magnetic() {
    if (reduced || !(win.matchMedia && win.matchMedia('(pointer: fine)').matches)) return;
    doc.querySelectorAll('.magnetic').forEach(function (el) {
      var raf = 0;
      el.addEventListener('mousemove', function (e) {
        var r = el.getBoundingClientRect();
        var x = clamp((e.clientX - (r.left + r.width / 2)) * 0.22, -10, 10);
        var y = clamp((e.clientY - (r.top + r.height / 2)) * 0.32, -8, 8);
        cancelAnimationFrame(raf);
        raf = requestAnimationFrame(function () { el.style.transform = 'translate(' + x.toFixed(1) + 'px,' + y.toFixed(1) + 'px)'; });
      });
      el.addEventListener('mouseleave', function () { cancelAnimationFrame(raf); el.style.transform = ''; });
    });
  });

  /* ================= the story timeline =================
     T is one number for the whole opening story, tied to scroll position:
     each chapter (.ch[data-t]) has T = its index when its centre crosses the
     middle of the viewport. After the last chapter T runs on to 8, reached
     exactly when the sticky stage lets go. Everything below reads T, so the
     story is fully reversible. */
  var story = { T: 0, subs: [] }; // subs: called with T after every scroll update
  safe(function timeline() {
    var storyEl = doc.querySelector('.story');
    var chs = Array.prototype.slice.call(doc.querySelectorAll('.story .ch[data-t]'));
    if (!storyEl || !chs.length) return;
    var anchors = [], storyEnd = 0, fadeEls = doc.querySelectorAll('.story .chapter, .story .chapter-in, .story .step');
    function layout() {
      var y = win.scrollY || win.pageYOffset;
      anchors = chs.map(function (el) {
        var r = el.getBoundingClientRect();
        return { t: +el.getAttribute('data-t'), c: r.top + y + r.height / 2, el: el };
      });
      var sr = storyEl.getBoundingClientRect();
      storyEnd = sr.bottom + y - win.innerHeight / 2; // the middle line when the stage releases
    }
    function compute() {
      var mid = (win.scrollY || win.pageYOffset) + win.innerHeight / 2;
      var T = 0, a = anchors;
      if (mid <= a[0].c) T = a[0].t;
      else if (mid >= a[a.length - 1].c) {
        var last = a[a.length - 1];
        T = last.t + clamp((mid - last.c) / Math.max(1, storyEnd - last.c), 0, 1);
      } else {
        for (var i = 0; i < a.length - 1; i++) {
          if (mid >= a[i].c && mid < a[i + 1].c) { T = a[i].t + (mid - a[i].c) / Math.max(1, a[i + 1].c - a[i].c) * (a[i + 1].t - a[i].t); break; }
        }
      }
      story.T = T;
      for (var k = 0; k < story.subs.length; k++) story.subs[k](T);
      // chapter text is fully present at its own moment and fades as it leaves
      if (!reduced) {
        fadeEls.forEach(function (el) {
          var t = +el.getAttribute('data-t');
          var o = 1 - smooth(0.42, 0.8, Math.abs(T - t));
          var s = Math.round(o * 100) / 100;
          if (el._o !== s) { el._o = s; el.style.opacity = Math.max(0.12, s); }
        });
      }
    }
    story.compute = compute;
    layout(); compute();
    // one scroll handler for the whole story, at most once per frame
    var queued = false;
    win.addEventListener('scroll', function () {
      if (queued) return;
      queued = true;
      requestAnimationFrame(function () { queued = false; compute(); });
    }, { passive: true });
    win.addEventListener('resize', function () { layout(); compute(); });
    win.addEventListener('load', function () { layout(); compute(); });
    if (doc.fonts && doc.fonts.ready) doc.fonts.ready.then(function () { layout(); compute(); });
  });

  /* ================= the honeycomb stage ================= */
  safe(function honeycomb() {
    var storyEl = doc.querySelector('.story');
    var stage = doc.querySelector('.stage');
    var canvas = doc.querySelector('.hive-gl');
    if (!storyEl || !stage || !canvas) return;
    function fail() { stage.classList.add('no-gl'); stage.classList.remove('gl-on'); }
    // the stage fades out before it lets go of the screen: no clip line. Driven by the story's scroll
    // update as well as the render loop, so it holds for the SVG fallback, reduced motion and fast jumps.
    function stageFade(T) { var o = 1 - smooth(7.55, 7.97, T); stage.style.setProperty('--fade', o.toFixed(3)); stage.style.visibility = o < 0.01 ? 'hidden' : ''; }
    story.subs.push(stageFade);
    stageFade(story.T);
    // warm accents behind / instead of the comb (stage glow, fallback honey cells) go out with the dark look (T = 1..6)
    function stageWarm(T) {
      stage.style.setProperty('--warm', (1 - idleOf(T)).toFixed(3));
      stage.style.setProperty('--links', smooth(0, 1, linksOf(T)).toFixed(3)); // the fallback's honey links
      stage.style.setProperty('--veil', veilOf(T).toFixed(3));
    }
    story.subs.push(stageWarm);
    stageWarm(story.T);
    var THREE = win.THREE;
    if (!THREE || !THREE.WebGLRenderer || !THREE.InstancedMesh) {
      fail();
      return;
    }
    var renderer;
    try {
      renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true, alpha: true, powerPreference: 'high-performance' });
    } catch (e) { fail(); return; }
    if (!renderer || !renderer.getContext()) { fail(); return; }
    renderer.setPixelRatio(Math.min(win.devicePixelRatio || 1, 1.75));
    renderer.setClearColor(0x000000, 0);
    if ('outputEncoding' in renderer && THREE.sRGBEncoding) renderer.outputEncoding = THREE.sRGBEncoding;

    var scene = new THREE.Scene();
    scene.fog = new THREE.Fog(0x0B0A08, 18, 32);
    var camera = new THREE.PerspectiveCamera(30, 1, 0.1, 100);
    camera.position.set(0, 0, 20);
    scene.add(new THREE.HemisphereLight(0xF2EDE4, 0x0B0A08, 0.42));
    var key = new THREE.DirectionalLight(0xFFE9C2, 1.05); key.position.set(-7, 9, 9); scene.add(key);
    var rim = new THREE.DirectionalLight(0xF5A623, 1.5); rim.position.set(9, -3, -7); scene.add(rim);
    var rimWarm = new THREE.Color(0xF5A623), rimIdle = new THREE.Color(0x3C4048); // dark look: a cool, dim edge light
    var fillL = new THREE.DirectionalLight(0x9AA6B8, 0.18); fillL.position.set(6, 2, 10); scene.add(fillL);

    var outer = new THREE.Group(), comb = new THREE.Group();
    outer.add(comb); scene.add(outer);
    var glow = new THREE.PointLight(0xFFC93C, 2.4, 9, 2); glow.position.set(0, 2.6, 0); comb.add(glow);

    /* ---- cells ---- */
    var R = 0.5, GAP = 0.075, N = 3, D = Math.sqrt(3) * R + GAP;
    var geo = new THREE.CylinderGeometry(R, R, 1, 6, 1);
    geo.translate(0, 0.5, 0);
    var mat = new THREE.MeshStandardMaterial({ color: 0xffffff, metalness: 0.5, roughness: 0.36, flatShading: true });
    mat.onBeforeCompile = function (sh) {
      sh.fragmentShader = sh.fragmentShader.replace('#include <emissivemap_fragment>',
        '#include <emissivemap_fragment>\n#ifdef USE_COLOR\n totalEmissiveRadiance += vColor.rgb * smoothstep(0.18, 0.55, vColor.r - vColor.b) * 0.6;\n#endif');
    };
    var seed = 7;
    function rnd() { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; }

    var cells = [], you = 0;
    for (var q = -N; q <= N; q++) for (var r = -N; r <= N; r++) {
      var s3 = -q - r;
      if (Math.abs(s3) > N) continue;
      var ring = Math.max(Math.abs(q), Math.abs(r), Math.abs(s3));
      var x = D * (q + r / 2), z = D * (Math.sqrt(3) / 2) * r;
      var dist = Math.sqrt(x * x + z * z) / (D * N);
      if (q === 0 && r === 0) you = cells.length;
      cells.push({ x: x, z: z, ring: ring, h: 0.45 + 1.35 * Math.pow(Math.max(0, 1 - dist), 1.5) + rnd() * 0.3,
        phase: rnd() * Math.PI * 2, honey: ring === N && rnd() < 0.12, shade: 0.75 + rnd() * 0.5,
        drift: -1, driftDur: 0, glow: 0, px: 0, py: 0, pz: 0, ph: 0, top: 0, exit: 0, busy: false, drop: rnd() });
    }
    var COUNT = cells.length;

    /* ---- where each laptop sits when the hive is apart: a loose, even field ---- */
    var RX = 6.2, RZ = 3.9;
    cells[you].sx = -0.6; cells[you].sz = 0.5; cells[you].sy = 0;
    cells.forEach(function (c, i) {
      if (i === you) return;
      for (var tries = 0; tries < 400; tries++) {
        var a = rnd() * Math.PI * 2, rr = Math.sqrt(rnd());
        var px = Math.cos(a) * RX * rr, pz = Math.sin(a) * RZ * rr, ok = true;
        for (var j = 0; j < i; j++) {
          var o = cells[j];
          if (o.sx === undefined) continue;
          var dx = o.sx - px, dz = o.sz - pz;
          if (dx * dx + dz * dz < 1.45 * 1.45) { ok = false; break; }
        }
        if (ok || tries === 399) { c.sx = px; c.sz = pz; c.sy = (rnd() - 0.5) * 0.9; break; }
      }
    });

    /* ---- the network: one clean, connected mesh over every laptop ----
       A Gabriel graph on the spread-out field: a and b are linked when no other laptop sits inside the
       circle whose diameter is a-b. That is a subgraph of the Delaunay triangulation, so no two links
       cross, and it contains the minimum spanning tree, so every laptop is reachable. Long, thin links
       along the outside are then dropped unless the tree needs them: three to five links per laptop. */
    function d2(i, j) { var dx = cells[i].sx - cells[j].sx, dz = cells[i].sz - cells[j].sz; return dx * dx + dz * dz; }
    var cand = [];
    cells.forEach(function (c, i) {
      for (var j = i + 1; j < cells.length; j++) {
        var dij = d2(i, j), ok = true;
        for (var m = 0; m < cells.length && ok; m++) if (m !== i && m !== j && d2(i, m) + d2(j, m) < dij) ok = false;
        if (ok) cand.push({ a: i, b: j, d: dij });
      }
    });
    cand.sort(function (x, y) { return x.d - y.d; });
    var root = cells.map(function (c, i) { return i; });
    function find(i) { while (root[i] !== i) { root[i] = root[root[i]]; i = root[i]; } return i; }
    cand.forEach(function (e) { var ra = find(e.a), rb = find(e.b); e.tree = ra !== rb; if (e.tree) root[ra] = rb; }); // Kruskal
    var medD = cand[Math.floor(cand.length / 2)].d, LONG = 1.4 * 1.4; // squared lengths
    var edges = [], seen = {};
    function addEdge(a, b) { var k = a < b ? a + '-' + b : b + '-' + a; if (seen[k] || a === b) return; seen[k] = 1; edges.push([a, b]); }
    cand.forEach(function (e) { if (e.tree || e.d <= medD * LONG) addEdge(e.a, e.b); });
    var adj = cells.map(function () { return []; });
    edges.forEach(function (e, k) { adj[e[0]].push({ n: e[1], k: k }); adj[e[1]].push({ n: e[0], k: k }); });
    var depth = cells.map(function () { return -1; }), parent = cells.map(function () { return null; });
    function bfs() {
      depth = cells.map(function () { return -1; }); parent = cells.map(function () { return null; });
      var queue = [you]; depth[you] = 0;
      while (queue.length) {
        var n = queue.shift();
        adj[n].forEach(function (e) { if (depth[e.n] < 0) { depth[e.n] = depth[n] + 1; parent[e.n] = { n: n, k: e.k }; queue.push(e.n); } });
      }
    }
    bfs();
    // safety net (the spanning tree already guarantees it): join any island to the nearest reached laptop
    cells.forEach(function (c, i) {
      if (depth[i] >= 0) return;
      var best = -1, bd = 1e9;
      cells.forEach(function (o, j) { if (depth[j] < 0) return; var d = d2(i, j); if (d < bd) { bd = d; best = j; } });
      var k = edges.length; edges.push([best, i]); adj[best].push({ n: i, k: k }); adj[i].push({ n: best, k: k }); bfs();
    });
    // every link starts at the end nearer your laptop, so the network draws outward from you
    edges.forEach(function (e) { if (depth[e[1]] < depth[e[0]]) { var t0 = e[0]; e[0] = e[1]; e[1] = t0; } });
    var meshInfo = { cells: COUNT, edges: edges.length, connected: depth.every(function (d) { return d >= 0; }),
      maxDepth: Math.max.apply(null, depth) };
    // when each link starts drawing (0 = first, 1 = last): by hops from your laptop, a little jitter inside a ring
    var edgeRank = edges.map(function (e) { return (depth[e[0]] + rnd() * 0.6) / Math.max(1, meshInfo.maxDepth); });
    /* hand-off target: one cell of the packed comb on the second ring, the one turned most towards the
       open right side of the page (and a little towards the camera) while the hand-off plays (T = 5) */
    var sendTo = -1, sendBest = -1e9, rotH = 0.35 + 5 * 0.11;
    for (var hi = 0; hi < COUNT; hi++) {
      var hc = cells[hi];
      if (hc.ring !== 2) continue;
      var hxr = hc.x * Math.cos(rotH) + hc.z * Math.sin(rotH), hzr = -hc.x * Math.sin(rotH) + hc.z * Math.cos(rotH);
      if (hxr + 0.5 * hzr > sendBest) { sendBest = hxr + 0.5 * hzr; sendTo = hi; }
    }
    /* split targets: twelve cells of the packed comb, one in each 30-degree slice around your cell
       (alternately the second and the outer ring, picked at random inside the slice), so the pieces fan
       out evenly across the whole comb. In order round the comb, so they leave in a sweep. */
    var splitTo = [];
    for (var sl0 = 0; sl0 < 12; sl0++) {
      var pool = [], want = sl0 % 2 ? N : 2;
      for (var ci0 = 0; ci0 < COUNT; ci0++) {
        var c0 = cells[ci0];
        if (ci0 === you || c0.ring < 2) continue;
        var ang = (Math.atan2(c0.z, c0.x) + Math.PI * 2) % (Math.PI * 2);
        if (Math.floor(ang / (Math.PI / 6) + 1e-6) % 12 === sl0) pool.push(ci0);
      }
      var pref = pool.filter(function (i) { return cells[i].ring === want; });
      var from = pref.length ? pref : pool;
      if (from.length) splitTo.push(from[Math.floor(rnd() * from.length)]);
    }
    meshInfo.split = splitTo.length;

    var mesh = new THREE.InstancedMesh(geo, mat, COUNT);
    if (mesh.instanceMatrix.setUsage) mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    comb.add(mesh);

    var E = edges.length;
    var g = new Float32Array(COUNT), hot = new Float32Array(E), fl = new Float32Array(COUNT), am = new Float32Array(COUNT); // per-frame glow, link heat, finish flash, amber share (reused)
    /* the links: one thin quad per link, turned to face the camera every frame and sized in screen pixels,
       so a link reads as a crisp ~1.3 px honey line with a soft glow at any zoom (GL lines are 1 device px).
       They are drawn after the cells with no depth test: the network sits on top of the field, every link
       whole from one laptop's top face to the next, never cut into stubs by a cell in front. */
    var LINK_HALF = 3.5; // px from the centre line to the edge of the glow
    var LINK_Y = 0.03; // links sit just above each cell's live top face
    var lineGeo = new THREE.BufferGeometry();
    var linePos = new Float32Array(E * 12), lineInt = new Float32Array(E * 4), lineAcross = new Float32Array(E * 4);
    var lineIdx = [];
    for (var li = 0; li < E; li++) {
      lineAcross.set([-1, 1, -1, 1], li * 4);
      lineIdx.push(li * 4, li * 4 + 1, li * 4 + 2, li * 4 + 2, li * 4 + 1, li * 4 + 3);
    }
    lineGeo.setAttribute('position', new THREE.BufferAttribute(linePos, 3));
    lineGeo.setAttribute('aInt', new THREE.BufferAttribute(lineInt, 1));
    lineGeo.setAttribute('aAcross', new THREE.BufferAttribute(lineAcross, 1));
    lineGeo.setIndex(lineIdx);
    if (lineGeo.attributes.position.setUsage) { lineGeo.attributes.position.setUsage(THREE.DynamicDrawUsage); lineGeo.attributes.aInt.setUsage(THREE.DynamicDrawUsage); }
    var lineMat = new THREE.ShaderMaterial({
      uniforms: { uCol: { value: new THREE.Color(0xFFC93C) }, uHot: { value: new THREE.Color(0xFFF1C4) }, uHalf: { value: LINK_HALF } },
      vertexShader: [
        'attribute float aInt; attribute float aAcross; varying float vInt; varying float vAcross;',
        'void main() {',
        '  vInt = aInt; vAcross = aAcross;',
        '  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);',
        '}'
      ].join('\n'),
      fragmentShader: [
        'uniform vec3 uCol; uniform vec3 uHot; uniform float uHalf; varying float vInt; varying float vAcross;',
        'void main() {',
        '  float d = abs(vAcross) * uHalf;', // px from the centre line
        '  float core = 1.0 - smoothstep(0.3, 1.0, d);',
        '  float halo = exp(-d * d * 0.32) * 0.38;',
        '  float a = clamp((core * 0.85 + halo) * min(vInt, 1.0), 0.0, 1.0);',
        '  gl_FragColor = vec4(mix(uCol, uHot, clamp(vInt - 1.0, 0.0, 1.0) * core), a);',
        '}'
      ].join('\n'),
      transparent: true, depthTest: false, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide
    });
    var lines = new THREE.Mesh(lineGeo, lineMat);
    lines.frustumCulled = false;
    lines.renderOrder = 2; // after the cells and the job chunks
    comb.add(lines);
    var camL = new THREE.Vector3(), invComb = new THREE.Matrix4(), pxK = 0.001; // camera in comb space; local units per px per unit distance

    var CH = 1 + splitTo.length; // the whole job + one piece per split target
    var chunkMat = new THREE.MeshStandardMaterial({ color: 0xFFC93C, emissive: 0xF5A623, emissiveIntensity: 0.75, metalness: 0.3, roughness: 0.35, flatShading: true });
    var chunks = new THREE.InstancedMesh(geo, chunkMat, CH);
    if (chunks.instanceMatrix.setUsage) chunks.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    chunks.frustumCulled = false;
    comb.add(chunks);

    var cDark = new THREE.Color(0x2E2920), cHoney = new THREE.Color(0xFFC93C), cDone = new THREE.Color(0xFFF1C4), cAmber = new THREE.Color(0xF5A623), tmpC = new THREE.Color(), tmpH = new THREE.Color();
    if (cDark.convertSRGBToLinear) { cDark.convertSRGBToLinear(); cHoney.convertSRGBToLinear(); cDone.convertSRGBToLinear(); cAmber.convertSRGBToLinear(); }
    var dummy = new THREE.Object3D();
    function ease(p) { return p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2; }
    function lerp(a, b, t) { return a + (b - a) * t; }

    var visW = 20, visH = 10, pointer = { x: 0, y: 0 }, Ts = 0, lastSpawn = 0, textShift = 0;
    var bobPhase = 0, pulsePhase = 0; // accumulated, so speeding up never jumps
    // framing (wide screens): x as a share of the visible width, scale, height, tilt;
    // wx / wide / wy: the decisions' wider framing (see wideOf)
    // kx / kbig / ky / kTilt: the decisions' packed comb, framed big (narrow screens: kNarrow)
    var POSE = { hx: 0.31, sx: 0.22, big: 2.6, apart: 1.35, hy: -0.7, sy: 0.15, tilt: 0.72, tiltApart: 0.98, wx: 0.06, wide: 1.25, wy: 0.08,
      kx: 0.07, kbig: 2.3, ky: -1.3, kTilt: 0.95, kNarrow: 1.2 };

    function putChunk(i, x, y, z, sc) {
      if (sc <= 0.001) { dummy.position.set(0, -60, 0); dummy.scale.set(0.001, 0.001, 0.001); }
      else { dummy.position.set(x, y, z); dummy.scale.set(0.4 * sc, 0.34 * sc, 0.4 * sc); }
      dummy.rotation.set(0, x * 0.4, 0);
      dummy.updateMatrix();
      chunks.setMatrixAt(i, dummy.matrix);
    }
    // a point flying in an arc from the top of cell a to the top of cell b (u 0..1): the split's pieces
    function arcTo(a, b, u, out) {
      out = out || AP;
      var A = cells[a], B = cells[b], t = ease(clamp(u, 0, 1));
      out[0] = lerp(A.px, B.px, t); out[2] = lerp(A.pz, B.pz, t);
      out[1] = lerp(A.top + 0.25, B.top + 0.04, t) + Math.sin(Math.PI * t) * 1.1;
      return out;
    }
    var AP = [0, 0, 0]; // scratch result, so the render loop does not allocate

    /* ---- what the bees (assets/js/bees.js) may know about the comb ----
       The bees live on their own overlay above the page; they read where each cell's top is on screen,
       which cells are free, and ask for a honey flash where they pick up or drop a job. */
    var edgeF = 0.44, apiV = new THREE.Vector3(), flashes = []; // flashes: { i, t0, k }
    function flashGlow(t) {
      for (var f = flashes.length - 1; f >= 0; f--) {
        var fl = flashes[f], a = t - fl.t0;
        if (a > 1.7 || a < 0) { flashes.splice(f, 1); continue; }
        g[fl.i] = Math.max(g[fl.i], fl.k * smooth(0, 0.12, a) * (1 - smooth(0.15, 1.6, a)));
      }
    }
    HIVE.comb = {
      count: COUNT,
      live: function () { return running && !dead && shown && story.T < 7.4; },
      T: function () { return Ts; },
      edge: function () { return edgeF; },
      rect: function () { return canvas.getBoundingClientRect(); },
      free: function (i) { var c = cells[i]; return !!c && !c.busy && c.drift < 0 && c.exit < 0.01; },
      busy: function (i, on) { if (cells[i]) cells[i].busy = !!on; },
      stats: function () { meshInfo.frameMs = Math.round(frameMs * 100) / 100; return meshInfo; }, // the network + scene cost per frame
      flash: function (i, k) { if (cells[i] && flashes.length < 24) flashes.push({ i: i, t0: clock, k: k == null ? 1 : k }); },
      screen: function (i, out, r) { // the top centre of cell i, in viewport px
        var c = cells[i];
        r = r || canvas.getBoundingClientRect();
        apiV.set(c.px, c.top, c.pz).applyMatrix4(comb.matrixWorld).project(camera);
        out.x = r.left + (apiV.x + 1) / 2 * r.width; out.y = r.top + (1 - apiV.y) / 2 * r.height;
        return out;
      }
    };

    function update(t, dt, snap) {
      var Tg = story.T;
      Ts = snap ? Tg : Ts + (Tg - Ts) * clamp(dt * 7, 0, 1);
      var T = Ts;
      stageFade(Tg);

      /* how far apart the laptops are (0 = one hive, 1 = spread out) */
      var pack = packOf(T);
      var S = smooth(0.15, 1.0, T) * (1 - 0.22 * smooth(1.4, 2.2, T)) * (1 - pack);
      var K = pack * (1 - smooth(6.4, 7.0, T)); // the split's big framing, handing over to the lock's
      var lock = smooth(6.55, 7.05, T) * (1 - smooth(7.2, 7.7, T));
      var exitP = smooth(7.1, 7.95, T);
      var links = linksOf(T), linkA = smooth(0, 1, links) * (1 - exitP); // links: how far drawn; linkA: how bright
      var idle = idleOf(T), warm = 1 - idle; // dark look: every laptop dark unless it is doing a job (see idleOf)
      var W = wideOf(T);
      /* energy climbs through the decisions (local: calm like the network beat, hand off: livelier,
         split: the most) and settles again as the hive locks. All of it is a function of T, so it scrubs both ways. */
      var calm = 1 - smooth(6.3, 6.9, T);
      var energy = (0.32 * smooth(4.4, 4.9, T) + 0.5 * smooth(5.4, 5.8, T)) * calm;
      if (!reduced) { bobPhase += dt * (0.6 + 2.2 * energy); pulsePhase += dt * (1.6 + 8 * energy); }

      /* pose of the whole group: a giant comb cropped by the right edge of the page,
         a still-large field of laptops while apart, giant again when it locks */
      var wide = camera.aspect > 1.25;
      var u = visH / 10.72; // keeps the framing the same at any viewport height
      var hx = wide ? visW * (POSE.hx + textShift) : 0, sx = wide ? visW * (POSE.sx + textShift) : 0, wx = wide ? visW * POSE.wx : 0;
      var gx = lerp(lerp(lerp(hx, sx, smooth(0, 1, T)), wx, W), hx, smooth(6.3, 7, T)) + (wide ? visW * 0.24 * exitP : 0); // drifts away from the text as it sinks
      var gs = u * lerp(wide ? POSE.big : 1.05, wide ? lerp(POSE.apart, POSE.wide, W) : 0.62, S) * (1 - 0.3 * exitP);
      var gy = u * (lerp(POSE.hy, wide ? lerp(POSE.sy, POSE.wy, W) : POSE.sy, S) - exitP * 2.2);
      var tilt = lerp(POSE.tilt, POSE.tiltApart, S);
      if (K > 0) { // the decisions: one packed comb, big, mid-page (reaching in under the text, dimmed by the veil)
        gx = lerp(gx, wide ? visW * POSE.kx : 0, K);
        gs = lerp(gs, u * (wide ? POSE.kbig : POSE.kNarrow), K);
        gy = lerp(gy, u * POSE.ky, K);
        tilt = lerp(tilt, POSE.kTilt, K);
      }
      outer.position.set(gx, gy, 0);
      outer.scale.set(gs, gs, gs);
      outer.rotation.x = tilt + pointer.y * 0.08;
      outer.rotation.y = pointer.x * 0.12;
      outer.rotation.z = reduced ? 0 : Math.sin(t * 0.21) * 0.04 * (1 - S);
      // a slow orbit tied to scroll, plus a breath of idle spin
      comb.rotation.y = 0.35 + T * 0.11 + (reduced ? 0 : t * 0.04);

      /* the job's journey, scrubbed by scroll */
      var k;
      g.fill(0); hot.fill(0); fl.fill(0); am.fill(0);
      for (k = 0; k < CH; k++) putChunk(k, 0, 0, 0, 0);

      // positions first (the chunks ride on them)
      for (var i = 0; i < COUNT; i++) {
        var c = cells[i];
        var sp = smooth(0, 1, clamp(S * 1.25 - c.drop * 0.25, 0, 1)); // laptops leave and return in a light stagger
        c.px = lerp(c.x, c.sx, sp); c.pz = lerp(c.z, c.sz, sp);
        c.py = lerp(0, c.sy, sp) + (reduced ? 0 : Math.sin(bobPhase + c.phase) * (0.12 + 0.45 * energy) * sp);
        c.ph = lerp(c.h, 0.42 + (c.h - 0.45) * 0.25, sp);
        // exit: cells drop away one by one
        var dropT = clamp((exitP - c.drop * 0.5) / 0.5, 0, 1);
        c.exit = ease(dropT);
      }

      var cy = cells[you];
      var u1 = clamp(T - 3.5, 0, 1), u2 = clamp(T - 4.5, 0, 1), u3 = clamp(T - 5.5, 0, 1);
      if (T > 3.5 && T < 4.5) { // stay local, on the packed comb: one job drops onto your laptop, runs there
        // (only that cell glows honey), finishes with a warm pulse
        var d1 = ease(clamp((u1 - 0.1) / 0.3, 0, 1));
        putChunk(0, cy.px, cy.top + lerp(3, 0.04, d1), cy.pz, smooth(0.06, 0.14, u1) * (1 - smooth(0.38, 0.46, u1))); // sinks into the top face
        var run = smooth(0.36, 0.44, u1) * (1 - smooth(0.66, 0.71, u1));
        var fin = smooth(0.66, 0.7, u1) * (1 - smooth(0.73, 0.86, u1)); // the finish: one warm honey pulse
        g[you] = Math.max(run * (0.8 + (reduced ? 0 : 0.12 * Math.sin(t * 3.2))), fin, 0.3 * smooth(0.72, 0.84, u1)); // then softly lit
        am[you] = 0.35 * run + 0.5 * fin; // deeper honey while it works and as it pulses: warm, never a white pillar
        fl[you] = 0.1 * fin;
      } else if (T >= 4.5 && T < 5.5) { // hand off, on the packed comb: your laptop is busy (it works in honey);
        // a job arrives, cannot go in, and arcs over to one other cell, which works on it; the result arcs back
        // and merges into your cell with a warm honey pulse
        var shim = reduced ? 0 : Math.sin(t * 3.2);
        var busyY = smooth(0, 0.1, u2) * (1 - smooth(0.82, 0.88, u2)); // your own work
        g[you] = Math.max(0.3, busyY * (0.72 + 0.1 * shim));
        am[you] = 0.4 * busyY;
        fl[you] = 0.05 * busyY * (reduced ? 1 : 0.5 + 0.5 * Math.sin(t * 4.1));
        var land = ease(clamp((u2 - 0.1) / 0.12, 0, 1)); // the job drops in and stops above your busy cell
        var bump = Math.sin(Math.PI * clamp((u2 - 0.2) / 0.06, 0, 1)) * 0.12; // a small bounce: it cannot go in
        if (u2 < 0.27) putChunk(0, cy.px, cy.top + lerp(2.6, 0.25, land) + bump, cy.pz, 0.65 * smooth(0.08, 0.14, u2));
        var go = clamp((u2 - 0.27) / 0.14, 0, 1);
        if (u2 >= 0.27 && go < 1) { var pA = arcTo(you, sendTo, go); putChunk(0, pA[0], pA[1], pA[2], 0.65 * (1 - smooth(0.85, 1, go))); }
        var work = smooth(0.39, 0.44, u2) * (1 - smooth(0.68, 0.72, u2)); // the other laptop works on it
        var flk = reduced ? 0.8 : 0.68 + 0.14 * Math.sin(t * 6.5 + cells[sendTo].phase * 7);
        g[sendTo] = work * flk;
        am[sendTo] = 0.85 * work;
        fl[sendTo] = 0.1 * work * (reduced ? 1 : 0.5 + 0.5 * Math.sin(t * 4.1 + cells[sendTo].phase * 5));
        var back = clamp((u2 - 0.7) / 0.14, 0, 1); // the result flies home
        if (u2 > 0.7 && back < 1) { var pB = arcTo(sendTo, you, back); putChunk(1, pB[0], pB[1], pB[2], 0.55 * smooth(0, 0.12, back) * (1 - smooth(0.9, 1, back))); }
        var merge = smooth(0.83, 0.86, u2) * (1 - smooth(0.88, 0.99, u2)); // the warm honey pulse as it merges
        g[you] = Math.max(g[you], merge);
        am[you] = Math.max(am[you], 0.5 * merge);
        fl[you] = Math.max(fl[you], 0.1 * merge);
      } else if (T >= 5.5 && T < 7.1) { // split, on the packed comb: one big job breaks into pieces that fly out
        // at once to twelve cells, which light up while they work; the pieces fly home and merge into one answer
        var v3 = T - 5.5; // runs on past 1, so your cell hands its glow over to the lock smoothly
        var big = smooth(0.14, 0.2, u3) * (1 - smooth(0.25, 0.29, u3));
        putChunk(0, cy.px, cy.top + 0.3 + 0.08 * smooth(0.2, 0.27, u3), cy.pz, 1.35 * big * (1 + 0.15 * smooth(0.22, 0.27, u3)));
        g[you] = 0.45 * smooth(0.16, 0.22, u3) * (1 - smooth(0.27, 0.33, u3));
        for (var n = 0; n < splitTo.length; n++) {
          var tgt = splitTo[n], d0 = n * 0.005, d1 = n * 0.005;
          var out = clamp((u3 - 0.26 - d0) / 0.12, 0, 1), home = clamp((u3 - 0.7 - d1) / 0.14, 0, 1);
          if (u3 > 0.26 + d0 && out < 1) { var p1 = arcTo(you, tgt, out); putChunk(1 + n, p1[0], p1[1], p1[2], 0.6 * (1 - smooth(0.85, 1, out))); }
          else if (u3 > 0.7 + d1 && home < 1) { var p2 = arcTo(tgt, you, home); putChunk(1 + n, p2[0], p2[1], p2[2], 0.5 * smooth(0, 0.12, home) * (1 - smooth(0.9, 1, home))); }
          var busy = smooth(0.36 + d0, 0.4 + d0, u3) * (1 - smooth(0.69 + d1, 0.73 + d1, u3)); // working on its piece
          var flick = reduced ? 0.8 : 0.68 + 0.14 * Math.sin(t * 6.5 + cells[tgt].phase * 7);
          g[tgt] = Math.max(g[tgt], busy * flick);
          am[tgt] = busy * 0.85; // a deeper honey while working, so it stays warm (not lemon) under the text veil
          fl[tgt] = Math.max(fl[tgt], 0.1 * busy * (reduced ? 1 : 0.5 + 0.5 * Math.sin(t * 4.1 + cells[tgt].phase * 5))); // warm highlights
        }
        var ans = smooth(0.84, 0.87, u3) * (1 - smooth(0.9, 1.0, v3)); // the one answer: a warm honey pulse at your cell
        fl[you] = 0.1 * ans;
        am[you] = 0.5 * ans;
        g[you] = Math.max(g[you], smooth(0.84, 0.88, u3) * (1 - 0.3 * smooth(0.92, 1.1, v3)) * (1 - smooth(1.35, 1.6, v3)));
      }
      // the rest of a packed comb (the decisions): dark honey, not black (it is one hive again, just not working)
      if (K > 0) for (k = 0; k < COUNT; k++) g[k] = Math.max(g[k], 0.16 * K);
      // resting hive: a slow wave of honey light rolls across it, always
      if (!reduced) { var rest = (1 - S) * (1 - exitP) * warm; for (k = 0; k < COUNT; k++) g[k] = Math.max(g[k], rest * 0.22 * Math.pow(Math.max(0, Math.sin(t * 0.45 - cells[k].x * 0.55 - cells[k].z * 0.25)), 8)); }
      // together: every cell lights as the hive locks
      if (lock > 0) for (k = 0; k < COUNT; k++) g[k] = Math.max(g[k], lock * (0.35 + 0.35 * (1 - cells[k].ring / N)));
      flashGlow(t); // where the bees pick up and drop jobs

      // idle hive (only while assembled at the top): a cell drifts out and back now and then
      if (!reduced && T < 0.3 && t - lastSpawn > 0.85) {
        lastSpawn = t;
        var act = 0; for (k = 0; k < COUNT; k++) if (cells[k].drift >= 0) act++;
        if (act < 4) { var pick = cells[Math.floor(rnd() * COUNT)]; if (pick.drift < 0 && !pick.busy) { pick.drift = t; pick.driftDur = 4.2 + rnd() * 2.2; } }
      }

      for (i = 0; i < COUNT; i++) {
        var cc = cells[i], lift = 0, mix = 0;
        if (cc.drift >= 0) {
          var p = (t - cc.drift) / cc.driftDur;
          if (p >= 1) cc.drift = -1;
          else { var e = ease(p < 0.5 ? p * 2 : (1 - p) * 2) * (1 - smooth(0.1, 0.5, T)); lift = e * (2.1 + cc.h * 0.4); mix = e; }
        }
        cc.glow += (g[i] - cc.glow) * (snap ? 1 : clamp(dt * 10, 0, 1));
        var gl = Math.max(mix * 0.8, cc.glow);
        // the few honey cells of the resting hive go dark while it is apart, so only the job glows
        tmpC.copy(cDark).multiplyScalar(cc.shade);
        if (cc.honey) tmpC.lerp(cHoney, (1 - smooth(0.1, 0.6, S)) * warm);
        tmpC.lerp(am[i] > 0 ? tmpH.copy(cHoney).lerp(cAmber, am[i]) : cHoney, gl * (1 - cc.exit)); // a job's glow shows even in the dark look: it is the only light there
        if (fl[i] > 0) tmpC.lerp(cDone, fl[i] * (1 - cc.exit));
        mesh.setColorAt(i, tmpC);
        var breathe = reduced ? 1 : 1 + 0.12 * Math.sin(t * 0.7 + cc.phase + cc.x * 0.4) * (1 - 0.6 * S);
        var es = 1 - cc.exit;
        dummy.position.set(cc.px, cc.py + lift + cc.glow * 0.16 - cc.exit * 2.4, cc.pz);
        dummy.rotation.set(cc.exit * 0.8, mix * 0.6 + S * cc.phase * 0.15, 0);
        var scl = (1 - mix * 0.18) * Math.max(0.001, es);
        dummy.scale.set(scl, Math.max(0.001, cc.ph * breathe * (1 - mix * 0.55) * (1 + 0.1 * fl[i]) * es), scl);
        dummy.updateMatrix();
        mesh.setMatrixAt(i, dummy.matrix);
        cc.top = dummy.position.y + dummy.scale.y; // the live top of the cell (the bees land here)
      }

      /* the links draw outward from your laptop and follow the laptops as they drift; the ones
         carrying work glow brighter. Each is a camera-facing quad, LINK_HALF px either side of the line. */
      outer.updateMatrixWorld(true);
      camL.copy(camera.position).applyMatrix4(invComb.copy(comb.matrixWorld).invert());
      for (k = 0; k < E; k++) {
        var ed = edges[k], A = cells[ed[0]], B = cells[ed[1]];
        var grow = clamp((links - edgeRank[k] * 0.55) / 0.45, 0, 1);
        var gw = ease(grow);
        var ax = A.px, ay = A.top + LINK_Y, az = A.pz;
        var bx = lerp(ax, B.px, gw), by = lerp(ay, B.top + LINK_Y, gw), bz = lerp(az, B.pz, gw);
        var pulse = reduced ? 0 : (0.1 + 0.25 * energy) * Math.pow(Math.max(0, Math.sin(pulsePhase - edgeRank[k] * 6)), 2 + 3 * energy);
        var inten = grow > 0.001 ? linkA * (0.4 + 0.15 * energy + pulse) * (1 - Math.max(A.exit, B.exit)) + hot[k] * (1.3 + 0.4 * energy) : 0;
        // side vector: across the link, facing the camera; its length is LINK_HALF px at each end
        var dx = bx - ax, dy = by - ay, dz = bz - az;
        var mx = (ax + bx) / 2 - camL.x, my = (ay + by) / 2 - camL.y, mz = (az + bz) / 2 - camL.z;
        var sx3 = dy * mz - dz * my, sy3 = dz * mx - dx * mz, sz3 = dx * my - dy * mx;
        var sl = Math.sqrt(sx3 * sx3 + sy3 * sy3 + sz3 * sz3);
        if (sl < 1e-6 || inten <= 0) { sx3 = sy3 = sz3 = 0; inten = 0; } else { sx3 /= sl; sy3 /= sl; sz3 /= sl; }
        var wa = LINK_HALF * pxK * Math.sqrt((ax - camL.x) * (ax - camL.x) + (ay - camL.y) * (ay - camL.y) + (az - camL.z) * (az - camL.z));
        var wb = LINK_HALF * pxK * Math.sqrt((bx - camL.x) * (bx - camL.x) + (by - camL.y) * (by - camL.y) + (bz - camL.z) * (bz - camL.z));
        var o = k * 12;
        linePos[o] = ax - sx3 * wa; linePos[o + 1] = ay - sy3 * wa; linePos[o + 2] = az - sz3 * wa;
        linePos[o + 3] = ax + sx3 * wa; linePos[o + 4] = ay + sy3 * wa; linePos[o + 5] = az + sz3 * wa;
        linePos[o + 6] = bx - sx3 * wb; linePos[o + 7] = by - sy3 * wb; linePos[o + 8] = bz - sz3 * wb;
        linePos[o + 9] = bx + sx3 * wb; linePos[o + 10] = by + sy3 * wb; linePos[o + 11] = bz + sz3 * wb;
        lineInt[k * 4] = lineInt[k * 4 + 1] = lineInt[k * 4 + 2] = lineInt[k * 4 + 3] = inten;
      }
      lineGeo.attributes.position.needsUpdate = true;
      lineGeo.attributes.aInt.needsUpdate = true;
      lines.visible = linkA > 0.002;

      mesh.instanceMatrix.needsUpdate = true;
      chunks.instanceMatrix.needsUpdate = true;
      chunkMat.emissiveIntensity = 0.75 + 0.45 * K; // the split's pieces stay bright over the big, darker comb
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
      glow.intensity = ((2.1 + (reduced ? 0 : Math.sin(t * 0.9) * 0.5)) * (1 - 0.5 * S) + lock * 1.8 + energy * 2.6) * warm;
      var idleL = idle * (1 - 0.55 * K); // the packed comb gets back some of the warm edge light
      rim.color.copy(rimWarm).lerp(rimIdle, idleL);
      rim.intensity = 1.5 * (1 - 0.85 * idleL);
      glow.position.set(lerp(0, cy.px, S), 2.6, lerp(0, cy.pz, S));
      glow.distance = 9 * Math.max(1, gs / 0.86); // light range in world units grows with the comb
    }

    function resize() {
      if (dead) return;
      var w = canvas.clientWidth || stage.clientWidth, h = canvas.clientHeight || stage.clientHeight;
      if (!w || !h) return;
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      visH = 2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * camera.position.z;
      visW = visH * camera.aspect;
      pxK = visH / camera.position.z / h; // world (= comb-local, the scale cancels) units per px, per unit of distance
      // where the story text column ends (share of the width): the comb keeps clear of it
      var edge = 0.44;
      doc.querySelectorAll('.story .chapter .tone, .story .chapter-in .tone, .story .step, .story .hero-sub').forEach(function (el) {
        var r = el.getBoundingClientRect(); if (r.width) edge = Math.max(edge, r.right / w);
      });
      textShift = clamp(edge - 0.44, 0, 0.1);
      edgeF = edge;
      camera.updateMatrixWorld(); camera.matrixWorldInverse.copy(camera.matrixWorld).invert(); // for the bees' projections before the first render
      stage.style.setProperty('--edge', (edge * 100).toFixed(1) + '%');
    }
    function onMove(e) {
      pointer.x = (e.clientX / win.innerWidth - 0.5) * 1.2;
      pointer.y = (e.clientY / win.innerHeight - 0.5) * 1.2;
    }
    if (!reduced) win.addEventListener('mousemove', onMove, { passive: true });

    var running = false, raf = 0, inView = true, clock = 6, last = 0, shown = false, dead = false, io = null, frameMs = 0;
    function draw() {
      if (dead) return;
      renderer.render(scene, camera);
      if (!shown) { shown = true; stage.classList.add('gl-on'); }
    }
    function frame(now) {
      if (!running) return;
      var dt = Math.min(0.05, (now - last) / 1000);
      last = now; clock += dt;
      var t0 = performance.now();
      update(clock, dt, false);
      draw();
      frameMs += (performance.now() - t0 - frameMs) * 0.05;
      raf = requestAnimationFrame(frame);
    }
    function start() {
      if (dead || running || reduced || !inView || doc.hidden) return;
      running = true; last = performance.now();
      raf = requestAnimationFrame(frame);
    }
    function stop() { running = false; cancelAnimationFrame(raf); }
    // reduced motion: no clock, just the frame for the current scroll position
    var pending = false;
    function still() {
      if (dead || pending) return;
      pending = true;
      requestAnimationFrame(function () { pending = false; update(8, 0.016, true); draw(); });
    }
    function onVis() { doc.hidden ? stop() : start(); }
    var rt = 0;
    function onResize() {
      clearTimeout(rt);
      rt = setTimeout(function () { if (dead) return; resize(); if (reduced) still(); else if (!running) draw(); }, 120);
    }
    canvas.addEventListener('webglcontextlost', function (e) {
      e.preventDefault();
      dead = true; stop(); clearTimeout(rt);
      if (io) io.disconnect();
      win.removeEventListener('resize', onResize);
      win.removeEventListener('mousemove', onMove);
      doc.removeEventListener('visibilitychange', onVis);
      fail();
    });

    resize();
    update(clock, 0.016, true);
    try { draw(); } catch (e) { fail(); return; }
    win.addEventListener('resize', onResize);
    if (doc.fonts && doc.fonts.ready) doc.fonts.ready.then(onResize);
    if (reduced) story.subs.push(still);
    io = whenVisible(storyEl, function () { inView = true; start(); }, function () { inView = false; stop(); }, 0);
    doc.addEventListener('visibilitychange', onVis);
    start();
  });

  /* ================= numbers: benchmark bars ================= */
  safe(function bench() {
    var bars = doc.getElementById('bars');
    if (!bars) return;
    var giant = doc.getElementById('giant-num'), label = doc.getElementById('giant-label');
    var tabs = doc.querySelectorAll('.tabs-line [role="tab"]');
    var DATA = {
      cpu: { giant: 3.21, label: 'faster on 4 laptops, one CPU job (best run)', rows: [
        { l: '1 laptop', s: 'baseline', v: 1.00 },
        { l: '2 laptops', s: 'Monte Carlo', v: 1.97 },
        { l: '4 laptops', s: 'best run', v: 3.21, hi: true }
      ] },
      gpu: { giant: 3.19, label: 'faster on 3 laptop GPUs, one AI text model', rows: [
        { l: '1 GPU', s: 'baseline', v: 1.00 },
        { l: '2 GPUs', s: 'movie-review AI, real data', v: 2.10 },
        { l: '3 GPUs', s: 'text model, 97% of ideal', v: 3.19, hi: true }
      ] }
    };
    var MAX = 3.4, current = 'cpu', shownOnce = false;
    function render(set, animate) {
      var d = DATA[set];
      bars.innerHTML = d.rows.map(function (r) {
        return '<div class="bar' + (r.hi ? ' hi' : '') + '"><div class="bar-l"><b>' + r.l + '</b><small>' + r.s + '</small></div>' +
          '<div class="bar-t"><i style="width:' + (animate ? 0 : (r.v / MAX * 100)) + '%" data-w="' + (r.v / MAX * 100) + '"></i></div>' +
          '<span class="bar-v mono">' + r.v.toFixed(2) + 'x</span></div>';
      }).join('');
      label.textContent = d.label;
      if (animate) {
        requestAnimationFrame(function () { requestAnimationFrame(function () {
          bars.querySelectorAll('i[data-w]').forEach(function (i, k) { i.style.transitionDelay = (k * 0.08) + 's'; i.style.width = i.getAttribute('data-w') + '%'; });
        }); });
        count(giant, d.giant, 2);
      } else giant.textContent = d.giant.toFixed(2);
    }
    function count(el, to, dec) {
      if (reduced) { el.textContent = to.toFixed(dec); return; }
      cancelAnimationFrame(el._raf); // a fast tab switch must not leave two counters racing
      var from = parseFloat(el.textContent) || 0, t0 = performance.now(), dur = 1100;
      (function f(now) {
        var p = clamp((now - t0) / dur, 0, 1), e = 1 - Math.pow(1 - p, 3);
        el.textContent = (from + (to - from) * e).toFixed(dec);
        el._raf = p < 1 ? requestAnimationFrame(f) : 0;
      })(t0);
    }
    function select(set, focus) {
      current = set;
      tabs.forEach(function (t) {
        var on = t.getAttribute('data-set') === set;
        t.setAttribute('aria-selected', String(on));
        t.tabIndex = on ? 0 : -1;
        if (on) { bars.setAttribute('aria-labelledby', t.id); if (focus) t.focus(); }
      });
      render(set, !reduced);
    }
    tabs.forEach(function (t, i) {
      t.addEventListener('click', function () { select(t.getAttribute('data-set')); });
      t.addEventListener('keydown', function (e) {
        if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
          e.preventDefault();
          var n = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
          select(n.getAttribute('data-set'), true);
        }
      });
    });
    render(current, false);
    if (reduced) return;
    giant.textContent = '0.00';
    bars.querySelectorAll('i[data-w]').forEach(function (i) { i.style.width = '0%'; });
    whenVisible(bars, function () { if (!shownOnce) { shownOnce = true; render(current, true); } }, null, 0.35);
  });

  /* ================= stats count-up ================= */
  safe(function countUp() {
    if (reduced || !hasIO) { // final values are already in the markup; make sure true zeros are visible
      doc.querySelectorAll('.num[data-to]').forEach(function (el) { if (!parseFloat(el.getAttribute('data-to'))) el.classList.add('zero', 'shown'); });
      return;
    }
    doc.querySelectorAll('.num[data-to]').forEach(function (el, idx) {
      var to = parseFloat(el.getAttribute('data-to')), dec = +(el.getAttribute('data-dec') || 0);
      var delay = idx * 110;
      if (!to) el.classList.add('zero');
      el.textContent = (0).toFixed(dec);
      var done = false;
      whenVisible(el, function () {
        if (done) return;
        done = true;
        if (!to) { setTimeout(function () { el.classList.add('shown'); }, delay); return; }
        var t0 = performance.now() + delay, dur = 1600;
        (function f(now) {
          var p = clamp((now - t0) / dur, 0, 1), e = p === 1 ? 1 : 1 - Math.pow(2, -10 * p);
          el.textContent = (to * e).toFixed(dec);
          if (p < 1) requestAnimationFrame(f);
        })(performance.now());
      }, null, 0.5);
    });
  });

  /* ================= parts: controller join code ================= */
  safe(function invite() {
    var codeEl = doc.getElementById('invite-code');
    if (!codeEl) return;
    var bar = doc.getElementById('invite-bar'), time = doc.getElementById('invite-time');
    var LIFE = 900, left = LIFE, timer = 0, ticks = 0;
    function fresh() {
      var abc = 'abcdefghjkmnpqrstuvwxyz23456789', s = 'hv-';
      for (var i = 0; i < 10; i++) s += abc[Math.floor(Math.random() * abc.length)];
      codeEl.textContent = s;
      codeEl.classList.remove('flip'); void codeEl.offsetWidth; codeEl.classList.add('flip');
      left = LIFE; tick();
    }
    function tick() {
      var m = Math.floor(left / 60), s = left % 60;
      time.textContent = (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
      bar.style.transform = 'scaleX(' + (left / LIFE) + ')';
    }
    fresh();
    if (reduced) return;
    whenVisible(codeEl, function () {
      if (timer) return;
      timer = setInterval(function () {
        if (doc.hidden) return;
        if (left > 0) { left--; tick(); }
        if (++ticks % 12 === 0) fresh();
      }, 1000);
    }, function () { clearInterval(timer); timer = 0; });
  });

  /* ================= parts: dashboard ================= */
  safe(function dash() {
    var bars = doc.querySelectorAll('.mini-dash [data-cpu]');
    if (!bars.length) return;
    function set(jitter) {
      bars.forEach(function (b) {
        var base = +b.getAttribute('data-cpu');
        b.style.width = clamp(base + (jitter ? (Math.random() - 0.5) * 18 : 0), 4, 50) + '%';
      });
    }
    set(false);
    if (reduced) return;
    var timer = 0;
    whenVisible(bars[0], function () { if (!timer) timer = setInterval(function () { if (!doc.hidden) set(true); }, 1500); },
      function () { clearInterval(timer); timer = 0; });
  });

  /* ================= parts: command line ================= */
  safe(function cli() {
    var pre = doc.getElementById('cli-term');
    if (!pre || reduced) return;
    var code = pre.querySelector('code');
    var steps = [
      { cmd: 'compute run ./hello -f', out: 'hello from the hive' },
      { cmd: 'compute explain <JOB_ID>' }
    ];
    function esc(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
    var running = false, timer = 0, si = 0, ci = 0, html = '';
    function render(extra) { code.innerHTML = html + extra + '<span class="cur"></span>'; }
    function step() {
      if (!running) return;
      if (si >= steps.length) { timer = setTimeout(function () { html = ''; si = 0; ci = 0; step(); }, 3800); return; }
      var s = steps[si];
      if (ci === 0) html += '<span class="p">$ </span>';
      if (ci < s.cmd.length) { ci++; render(esc(s.cmd.slice(0, ci))); timer = setTimeout(step, 30 + Math.random() * 45); return; }
      html += esc(s.cmd) + '\n';
      render('');
      timer = setTimeout(function () {
        if (s.out) { html += '<span class="o">' + esc(s.out) + '</span>\n'; render(''); }
        si++; ci = 0; timer = setTimeout(step, 900);
      }, 600);
    }
    whenVisible(pre, function () { if (!running) { running = true; html = ''; si = 0; ci = 0; step(); } },
      function () { running = false; clearTimeout(timer); });
  });
})();
