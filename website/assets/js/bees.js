/* hive. — the bees (home page only)
   Eight Buzz bees, all equals, live on ONE transparent fixed WebGL overlay above the whole page
   (pointer-events: none, DPR capped at 1.5, paused while the tab is hidden).

   Each bee runs its own small state machine and picks its next task from what is on screen:
     carry   take a honey job from one comb cell to another (the comb flashes where it lands)
     cell    inspect a comb cell: hover beside it, tilt down, curious, then thinking
     weave   thread through a headline line, in front of some letters and behind others
     visit   perch on a letter, a heading, a big number or a bar, or inspect a small UI
     wander  a short glide to a free spot
     follow  trail the visitor while they scroll; on the way some peel off to the next section
     cursor  now and then a nearby bee notices the pointer, follows it a little, then goes back to work
     flee    chased too often, it gets angry and darts away
   Movement is steering (arrive + separation), with facing turns, pitch, banking and a gentle bob.

   Depth among letters: the overlay sits above the text, so a bee is in front of everything by default.
   To pass BEHIND a letter, that letter is redrawn above the overlay: .bees-front holds one absolutely
   positioned copy per headline glyph, placed on the original glyph's own box (Range rects, so kerning and
   letter-spacing match). Depth is decided per WORD: a threading bee passes wholly behind one word and wholly
   in front of the next (or flies above / below a word); while it overlaps a word it is behind, that word's
   copies are shown, otherwise they stay hidden. It never switches depth inside a word.

   Degrades: GLB fails -> flat Buzz sprites; no WebGL -> no bees (the stage's still SVG Buzz stays);
   reduced motion -> a few still bees perched around the page, no chasing, no clock. */
(function () {
  'use strict';
  var doc = document, win = window, HIVE = win.HIVE || (win.HIVE = {});
  var THREE = win.THREE;
  if (!doc.body || !doc.body.classList.contains('home') || !THREE || !THREE.WebGLRenderer) return;
  var reduced = !!HIVE.reduced || !!(win.matchMedia && win.matchMedia('(prefers-reduced-motion: reduce)').matches);
  var fine = !!(win.matchMedia && win.matchMedia('(pointer: fine)').matches);

  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
  function lerp(a, b, t) { return a + (b - a) * t; }
  function rnd(a, b) { return a + Math.random() * (b - a); }
  function hyp(x, y) { return Math.sqrt(x * x + y * y); }
  function damp(k, dt) { return 1 - Math.exp(-k * dt); }
  function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }
  function warn(e) { if (win.console) console.warn('[hive bees]', e); }

  /* ---------------- overlay, renderer, camera (1 css px = 1 unit, y down = -y) ---------------- */
  var canvas = doc.createElement('canvas');
  canvas.className = 'bees-gl';
  canvas.setAttribute('aria-hidden', 'true');
  var front = doc.createElement('div');
  front.className = 'bees-front';
  front.setAttribute('aria-hidden', 'true');
  doc.body.appendChild(canvas);
  doc.body.appendChild(front);
  var renderer = null;
  try { renderer = new THREE.WebGLRenderer({ canvas: canvas, alpha: true, antialias: true }); } catch (e) { renderer = null; }
  if (!renderer || !renderer.getContext()) { canvas.parentNode.removeChild(canvas); front.parentNode.removeChild(front); return; }
  renderer.setPixelRatio(Math.min(win.devicePixelRatio || 1, 1.5));
  renderer.setClearColor(0x000000, 0);
  if (THREE.sRGBEncoding) renderer.outputEncoding = THREE.sRGBEncoding;

  var scene = new THREE.Scene();
  var cam = new THREE.OrthographicCamera(0, 1, 0, -1, 1, 4000);
  cam.position.set(0, 0, 2000);
  // the comb's own light rig (warm key from top-left, honey rim from behind), a touch brighter for the small bees
  scene.add(new THREE.HemisphereLight(0xF2EDE4, 0x0B0A08, 0.75));
  var key = new THREE.DirectionalLight(0xFFE9C2, 1.45); key.position.set(-7, 9, 9); scene.add(key);
  var rim = new THREE.DirectionalLight(0xF5A623, 1.3); rim.position.set(9, -3, -7); scene.add(rim);
  var fill = new THREE.DirectionalLight(0x9AA6B8, 0.35); fill.position.set(6, 2, 10); scene.add(fill);

  /* ---------------- sizes ---------------- */
  var W = 1, H = 1, SY = 0, LEN = 150, BH = 100, N = 5;
  var MLEN = 0.885, MHT = 0.62; // model length / height in model units (measured when the GLB loads)
  function sizes() {
    W = win.innerWidth || doc.documentElement.clientWidth || 1;
    H = win.innerHeight || doc.documentElement.clientHeight || 1;
    // about the size of the old hero bee (the job it carries is part of the model, so it scales with it)
    LEN = Math.min(clamp(H * 0.09, 60, 100), W * 0.12);
    BH = LEN * MHT / MLEN;
    N = W < 760 ? 3 : 8;
    renderer.setSize(W, H, false);
    cam.left = 0; cam.right = W; cam.top = 0; cam.bottom = -H;
    cam.updateProjectionMatrix();
  }
  sizes();

  /* ---------------- page layout: headline glyphs, places to visit, text to keep clear of ---------------- */
  var L = { heads: [], pois: [], avoid: [], res: {}, ox: 0, oy: 0, next: 0 };
  var HEAD_SEL = ['.hero-title', '.final-title'];
  var POI_SEL = [
    ['.chapter .tone b, .chapter-in .tone b', 'perch'],
    ['.step h3', 'perch'],
    ['.numbers h2 b, .facts h2 b, .parts h2 b, .next h2 b', 'perch'],
    ['#giant-num', 'perch'],
    ['.bar-t', 'bar'],
    ['.stat .num', 'perch'],
    ['.grid3 .tone b', 'perch'],
    ['.part .mini', 'inspect'],
    ['.roadmap h3', 'perch'],
    ['.footer-mark span', 'perch']
  ];
  // body text and controls a bee must never rest on
  var AVOID_SEL = '.hero-sub, .hero-cta, .chapter .tone, .chapter-in .tone, .step p, .step h3, .step-n, .numbers h2, .facts h2, .parts h2, .next h2, ' +
    '.giant, .giant-label, .tabs-line, .bar-l, .bar-v, .bench-note, .stat dd, .stat dt, .grid3 .tone, .part h3, .part p, .mini, ' +
    '.roadmap h3, .roadmap p, .final-title, .final-cta, .footer-top, .footer-base, .hero-title';
  var mctx = doc.createElement('canvas').getContext('2d');

  function dRect(r) { return { l: r.left, t: r.top + SY, r: r.right, b: r.bottom + SY, w: r.width, h: r.height }; }
  function firstText(el) {
    var tw = doc.createTreeWalker(el, NodeFilter.SHOW_TEXT, null), n;
    while ((n = tw.nextNode())) if (/\S/.test(n.data)) return n;
    return null;
  }
  // the first line of an element's text, as a document rect
  function firstLine(el) {
    var tn = firstText(el);
    if (!tn) return null;
    var rg = doc.createRange(); rg.selectNodeContents(tn);
    var rs = rg.getClientRects();
    for (var i = 0; i < rs.length; i++) if (rs[i].width > 2) return { r: dRect(rs[i]), node: tn };
    return null;
  }
  // where the ink of `ch` starts (document y) in a text box: canvas font metrics, with a plain fallback
  function inkTop(node, r, ch) {
    var cs = win.getComputedStyle(node.parentElement);
    mctx.font = cs.fontStyle + ' ' + cs.fontWeight + ' ' + cs.fontSize + ' ' + cs.fontFamily;
    var m = mctx.measureText(ch || 'H');
    if (m.fontBoundingBoxAscent == null || m.actualBoundingBoxAscent == null) return r.t + r.h * 0.21;
    var base = r.t + (r.h - (m.fontBoundingBoxAscent + m.fontBoundingBoxDescent)) / 2 + m.fontBoundingBoxAscent;
    return base - m.actualBoundingBoxAscent;
  }

  // headlines: one copy per glyph above the overlay (hidden until a bee passes behind that letter)
  function buildHead(h) {
    h.lines.forEach(function (ln) { ln.chars.forEach(function (c) { if (c.sp.parentNode) c.sp.parentNode.removeChild(c.sp); }); });
    h.lines = [];
    var lns = h.el.querySelectorAll('.ln');
    if (!lns.length) lns = [h.el];
    var rg = doc.createRange();
    Array.prototype.forEach.call(lns, function (ln) {
      var chars = [], words = [], wi = 0, gap = false, tw = doc.createTreeWalker(ln, NodeFilter.SHOW_TEXT, null), n;
      while ((n = tw.nextNode())) {
        var cs = win.getComputedStyle(n.parentElement);
        for (var k = 0; k < n.data.length; k++) {
          var ch = n.data.charAt(k);
          if (!/\S/.test(ch)) { gap = true; continue; }
          if (gap && chars.length) wi++;
          gap = false;
          rg.setStart(n, k); rg.setEnd(n, k + 1);
          var cr = rg.getBoundingClientRect();
          if (!cr.width || !cr.height) continue;
          var r = dRect(cr);
          var sp = doc.createElement('span');
          sp.textContent = ch;
          var s = sp.style;
          s.left = (r.l - L.ox) + 'px'; s.top = (r.t - L.oy) + 'px';
          s.height = r.h + 'px'; s.lineHeight = r.h + 'px';
          s.fontFamily = cs.fontFamily; s.fontSize = cs.fontSize; s.fontWeight = cs.fontWeight; s.fontStyle = cs.fontStyle;
          s.fontStretch = cs.fontStretch; s.color = cs.color;
          if (cs.fontVariationSettings) s.fontVariationSettings = cs.fontVariationSettings;
          if (cs.fontOpticalSizing) s.fontOpticalSizing = cs.fontOpticalSizing;
          front.appendChild(sp);
          var top = inkTop(n, r, ch);
          chars.push({ sp: sp, l: r.l, r: r.r, t: r.t, b: r.b, cx: (r.l + r.r) / 2, top: top,
            tall: top < r.t + r.h * 0.45 && /[A-Za-z0-9]/.test(ch), on: false });
          var w = words[wi] || (words[wi] = { chars: [], l: 1e9, r: -1e9, t: 1e9, b: -1e9, want: false });
          w.chars.push(chars[chars.length - 1]);
          w.l = Math.min(w.l, r.l); w.r = Math.max(w.r, r.r); w.t = Math.min(w.t, r.t); w.b = Math.max(w.b, r.b);
        }
      }
      if (!chars.length) return;
      var l = 1e9, rr = -1e9, t = 1e9, b = -1e9, top = 1e9;
      chars.forEach(function (c) { l = Math.min(l, c.l); rr = Math.max(rr, c.r); t = Math.min(t, c.t); b = Math.max(b, c.b); top = Math.min(top, c.top); });
      words = words.filter(Boolean);
      h.lines.push({ chars: chars, words: words, l: l, r: rr, t: t, b: b, h: b - t, top: top, cy: lerp(top, b, 0.42), key: h.key + ':' + h.lines.length });
    });
    h.sig = sigOf(h.el);
    h.ready = h.lines.length > 0;
  }
  function sigOf(el) { var r = el.getBoundingClientRect(); return Math.round(r.left) + ',' + Math.round(r.top + SY) + ',' + Math.round(r.width) + ',' + Math.round(r.height); }

  function layout(force) {
    SY = win.scrollY || win.pageYOffset || 0;
    var fr = front.getBoundingClientRect();
    L.ox = fr.left; L.oy = fr.top + SY;
    var t = lastT > 0 ? lastT : performance.now() / 1000;
    // headlines: measured once their line reveal has finished
    if (!L.heads.length) HEAD_SEL.forEach(function (sel, i) { var el = doc.querySelector(sel); if (el) L.heads.push({ el: el, key: 'h' + i, lines: [], ready: false, inAt: 0, sig: '' }); });
    L.heads.forEach(function (h) {
      var shown = reduced || h.el.classList.contains('in');
      if (!shown) return;
      if (!h.inAt) h.inAt = t;
      if (t - h.inAt < 1.4 && !reduced) return;
      // the line reveal must really be over (its transform gone), or the copies would sit off the glyphs
      var moving = Array.prototype.some.call(h.el.querySelectorAll('.ln > span'), function (s) { var tf = win.getComputedStyle(s).transform; return tf && tf !== 'none' && tf !== 'matrix(1, 0, 0, 1, 0, 0)'; });
      if (moving) { h.inAt = t - 1.0; return; }
      if (force || !h.ready || sigOf(h.el) !== h.sig) buildHead(h);
    });
    // places to visit
    var pois = [];
    POI_SEL.forEach(function (d) {
      doc.querySelectorAll(d[0]).forEach(function (el) {
        var p = poiSpot(el, d[1]);
        if (p) { p.key = 'p' + pois.length; pois.push(p); }
      });
    });
    L.pois = pois;
    // text to keep clear of
    var av = [];
    doc.querySelectorAll(AVOID_SEL).forEach(function (el) {
      var r = el.getBoundingClientRect();
      if (r.width && r.height) { var d = dRect(r); d.el = el; av.push(d); }
    });
    L.avoid = av;
    L.pois = L.pois.filter(function (p) { return clearAt(p.x0, p.y, p.el) || clearAt(p.x1, p.y, p.el); });
    L.next = t + 2.5;
  }
  function poiSpot(el, kind) {
    var r, fl;
    if (kind === 'bar') {
      var tr = el.getBoundingClientRect(), fillEl = el.querySelector('i');
      if (!tr.width) return null;
      r = dRect(tr);
      var fw = fillEl ? fillEl.getBoundingClientRect().width : r.w * 0.5;
      var x = r.l + Math.max(LEN * 0.3, fw);
      return { el: el, kind: 'perch', group: 'bars', x0: x, x1: x, y: r.t - BH * 0.34, lx: x + 100, ly: r.t };
    }
    if (kind === 'inspect') {
      var ir = el.getBoundingClientRect();
      if (!ir.width) return null;
      r = dRect(ir);
      var right = r.r + LEN * 0.7 < W - 8;
      var sx = right ? r.r + LEN * 0.32 : r.l + r.w * 0.72, sy = right ? r.t + r.h * 0.25 : r.t - BH * 0.55;
      return { el: el, kind: 'inspect', x0: sx, x1: sx, y: sy, lx: r.l + r.w * 0.5, ly: r.t + r.h * 0.5 };
    }
    fl = firstLine(el);
    if (!fl) return null;
    r = fl.r;
    var top = inkTop(fl.node, r, 'H');
    var x0 = r.l + Math.min(LEN * 0.35, r.w / 2), x1 = r.r - Math.min(LEN * 0.35, r.w / 2);
    return { el: el, kind: 'perch', x0: x0, x1: Math.max(x0, x1), y: top - BH * 0.34, lx: (r.l + r.r) / 2, ly: top };
  }
  // is a resting bee at (x, y) clear of body text and controls? `except` is the element it perches on
  function clearAt(x, y, except) {
    var hw = LEN * 0.4, ht = BH * 0.42, hb = BH * 0.3;
    for (var i = 0; i < L.avoid.length; i++) {
      var a = L.avoid[i];
      if (except && (a.el === except || a.el.contains(except) || except.contains(a.el))) continue;
      if (x + hw > a.l && x - hw < a.r && y + hb > a.t && y - ht < a.b) return false;
    }
    return true;
  }
  function onScreenY(y, m) { var s = y - SY; return s > 92 + (m || 0) && s < H - 50 - (m || 0); }

  /* ---------------- the look: the 3D Buzz (with expressions) or the flat sprite ---------------- */
  var YAW = 0.5; // ~29 degrees toward the camera, so the visor reads
  var AXX = new THREE.Vector3(1, 0, 0), AXY = new THREE.Vector3(0, 1, 0), AXZ = new THREE.Vector3(0, 0, 1);
  var qP = new THREE.Quaternion(), qY = new THREE.Quaternion(), qR = new THREE.Quaternion(), qT = new THREE.Quaternion();
  var NR = new THREE.Vector3(0.866, 0, 0.5), NL = new THREE.Vector3(0.866, 0, -0.5); // eye face normals (head space)
  // expression targets: face set (0 neutral, 1 happy, 2 think), eye scale / height / per side, brow slant,
  // mouth scale, head roll / nod, visor glow and rim colour (0 honey .. 1 angry red)
  var MOODS = {
    neutral:   { set: 0, e: 1, ey: 1, el: 1, er: 1, sl: 0, mx: 1, my: 1, mz: 1, roll: 0, nod: 0, vr: 0, vg: 0, vb: 0, rim: 0 },
    happy:     { set: 1, e: 1, ey: 1, el: 1, er: 1, sl: 0, mx: 1, my: 1, mz: 1, roll: 0, nod: -0.08, vr: 0.05, vg: 0.035, vb: 0.01, rim: 0 },
    think:     { set: 2, e: 1, ey: 1, el: 1, er: 1, sl: 0, mx: 1, my: 1, mz: 1, roll: 0.1, nod: 0.14, vr: 0, vg: 0, vb: 0, rim: 0 },
    curious:   { set: 0, e: 1, ey: 1.08, el: 0.88, er: 1.2, sl: 0, mx: 0.9, my: 1, mz: 0.9, roll: 0.26, nod: -0.04, vr: 0.02, vg: 0.015, vb: 0, rim: 0 },
    surprised: { set: 0, e: 1.28, ey: 1.42, el: 1, er: 1, sl: 0, mx: 1, my: 2.6, mz: 0.62, roll: 0, nod: -0.14, vr: 0.1, vg: 0.07, vb: 0.02, rim: 0 },
    angry:     { set: 0, e: 1.08, ey: 0.46, el: 1, er: 1, sl: 0.55, mx: 1, my: 0.8, mz: 1.3, roll: 0, nod: 0.12, vr: 0.27, vg: 0.025, vb: 0, rim: 1 }
  };
  var MKEYS = ['e', 'ey', 'el', 'er', 'sl', 'mx', 'my', 'mz', 'roll', 'nod', 'vr', 'vg', 'vb', 'rim'];

  var modelLook = (function () {
    var tpl = null, strength = {};
    function load(done, fail) {
      var gone = false;
      function bad(e) { if (!gone) { gone = true; if (e) warn(e); fail(); } }
      function go() {
        if (!THREE.GLTFLoader) { bad(); return; }
        new THREE.GLTFLoader().load('assets/models/buzz.glb', function (gltf) {
          if (gone) return;
          try {
            // r128 has no KHR_materials_emissive_strength: apply it by material name
            var mats = (gltf.parser && gltf.parser.json && gltf.parser.json.materials) || [];
            mats.forEach(function (m) { var e = m.extensions && m.extensions.KHR_materials_emissive_strength; if (e) strength[m.name] = e.emissiveStrength; });
            tpl = gltf.scene.getObjectByName('Buzz') || gltf.scene;
            tpl.traverse(function (o) {
              if (!o.isMesh) return;
              var m = o.material, n = m.name || '';
              if (strength[n]) m.emissiveIntensity = strength[n];
              // satin, like the comb's cells seen from a distance: no mirror shine, only the soft honey seams glow
              if (/^Bronze|^Joint/.test(n)) { m.metalness = 0.14; m.roughness = 0.66; }
              else if (n === 'Visor') { m.metalness = 0.12; m.roughness = 0.42; }
              else if (/^Job/.test(n)) { m.metalness = Math.min(m.metalness, 0.12); m.roughness = Math.max(m.roughness, 0.55); }
              else if (/^Honey/.test(n)) m.emissiveIntensity *= 0.85;
              m.needsUpdate = true;
            });
            var box = new THREE.Box3().setFromObject(tpl), sz = new THREE.Vector3();
            box.getSize(sz);
            if (sz.x > 0.1) { MLEN = sz.x; MHT = sz.y; }
          } catch (e) { bad(e); return; }
          gone = true; done();
        }, undefined, bad);
      }
      if (THREE.GLTFLoader) { go(); return; }
      var s = doc.createElement('script');
      s.src = 'https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/loaders/GLTFLoader.js';
      s.crossOrigin = 'anonymous'; s.referrerPolicy = 'no-referrer';
      s.onload = go; s.onerror = function () { bad(); };
      doc.head.appendChild(s);
      setTimeout(function () { bad(); }, 15000); // a stalled CDN falls back to the flat look
    }
    function make() {
      var root = tpl.clone(true), cache = new Map(), visor = [], rims = [];
      // geometry and most materials are shared; the visor and its rim are per bee, so each can glow its own mood
      root.traverse(function (o) {
        if (!o.isMesh) return;
        var n = o.material.name;
        if (n !== 'Visor' && n !== 'HoneyRim') return;
        var m = cache.get(o.material);
        if (!m) { m = o.material.clone(); cache.set(o.material, m); (n === 'Visor' ? visor : rims).push(m); }
        o.material = m;
      });
      var obj = new THREE.Group(); obj.add(root);
      function nd(n) { return root.getObjectByName(n); }
      function rest(o) { return o ? { o: o, s: o.scale.clone(), q: o.quaternion.clone() } : null; }
      var sets = [['EyeL', 'EyeR', 'Mouth'], ['EyeL_Happy', 'EyeR_Happy', 'Mouth_Happy'], ['EyeL_Think', 'EyeR_Think', 'Mouth_Think']]
        .map(function (s) { return s.map(function (n) { return rest(nd(n)); }); });
      var head = nd('Head'), headR = head ? head.rotation.clone() : null, job = nd('Job');
      var flames = ['FlameL', 'FlameR'].map(nd).filter(Boolean);
      var arms = ['ArmL', 'ArmR'].map(nd).filter(Boolean);
      var armRest = arms.map(function (a) { return a.quaternion.clone(); });
      var armCarry = arms.map(function (a) { var q = a.userData.carry_quat_gltf; return q ? new THREE.Quaternion(q[0], q[1], q[2], q[3]) : a.quaternion.clone(); });
      var rimBase = rims.length ? rims[0].emissive.clone() : new THREE.Color(), rimAngry = new THREE.Color(1, 0.2, 0.05);
      var cur = {}, shown = -1, carry = 0, lastT = -1;
      MKEYS.forEach(function (k) { cur[k] = MOODS.neutral[k]; });
      function setFace(f) {
        if (f === shown) return;
        shown = f;
        for (var i = 0; i < 3; i++) for (var k = 0; k < 3; k++) if (sets[i][k]) sets[i][k].o.visible = i === f;
      }
      return { obj: obj, set: function (st) {
        var dt = lastT < 0 ? 1 : clamp(st.t - lastT, 0, 0.1); lastT = st.t;
        var md = MOODS[st.mood] || MOODS.neutral, kk = st.snap ? 1 : damp(11, dt), i;
        for (i = 0; i < MKEYS.length; i++) cur[MKEYS[i]] += (md[MKEYS[i]] - cur[MKEYS[i]]) * kk;
        obj.position.set(st.x, -st.y, st.z * LEN * 0.5);
        obj.scale.setScalar(st.k * LEN / MLEN);
        // screen-facing turn: right (yaw -YAW) -> toward the camera -> left (yaw -PI + YAW); then the bank about
        // the bee's own length, then the pitch in the screen plane
        qY.setFromAxisAngle(AXY, -YAW - (1 - st.face) / 2 * (Math.PI - 2 * YAW));
        qR.setFromAxisAngle(AXX, st.roll);
        qP.setFromAxisAngle(AXZ, st.pitch);
        root.quaternion.copy(qP).multiply(qY).multiply(qR);
        setFace(md.set);
        // eyes: mood shape, blink, brow slant (about each eye's face normal)
        var fs = sets[md.set], bl = 1 - 0.9 * st.blink;
        for (i = 0; i < 2; i++) {
          var e = fs[i];
          if (!e) continue;
          var side = i === 0 ? cur.el : cur.er;
          e.o.scale.set(e.s.x * cur.e * side, e.s.y * cur.ey * side * bl, e.s.z * cur.e * side);
          if (md.set === 0 && Math.abs(cur.sl) > 0.002) { qT.setFromAxisAngle(i === 0 ? NL : NR, (i === 0 ? 1 : -1) * cur.sl); e.o.quaternion.copy(e.q).multiply(qT); }
          else e.o.quaternion.copy(e.q);
        }
        if (fs[2]) { // mouth: stretched to an "o" when surprised, turned over into a frown when angry
          fs[2].o.scale.set(fs[2].s.x * cur.mx, fs[2].s.y * cur.my, fs[2].s.z * cur.mz);
          if (md.set === 0 && cur.rim > 0.5) { qT.setFromAxisAngle(AXX, Math.PI); fs[2].o.quaternion.copy(fs[2].q).multiply(qT); }
          else fs[2].o.quaternion.copy(fs[2].q);
        }
        if (head) head.rotation.set(headR.x + cur.roll + st.headRoll, headR.y, headR.z + cur.nod + st.headNod);
        for (i = 0; i < visor.length; i++) visor[i].emissive.setRGB(cur.vr, cur.vg, cur.vb);
        for (i = 0; i < rims.length; i++) rims[i].emissive.copy(rimBase).lerp(rimAngry, cur.rim);
        // carrying: arms close round the job
        carry += ((st.carry ? 1 : 0) - carry) * (st.snap ? 1 : damp(9, dt));
        if (job) job.visible = carry > 0.5;
        for (i = 0; i < arms.length; i++) arms[i].quaternion.copy(armRest[i]).slerp(armCarry[i], carry);
        var fl = reduced ? 0.7 : (0.5 + 0.6 * st.jet) * (0.85 + 0.15 * Math.sin(st.t * 31 + st.x * 0.07) * Math.sin(st.t * 17 + st.y * 0.05));
        for (i = 0; i < flames.length; i++) flames[i].scale.set(fl, fl, fl);
      } };
    }
    return { load: load, make: make };
  })();

  var spriteLook = (function () { // fallback: the flat Buzz SVGs, rasterised once (plus mirrored copies)
    var tex = [];
    function load(done) {
      var names = ['c1', 'c1-carry', 'c1-happy', 'c1-think'], left = names.length, SZ = 512;
      names.forEach(function (nm, p) {
        var img = new Image();
        img.onload = img.onerror = function () {
          for (var m = 0; m < 2; m++) {
            var cv = doc.createElement('canvas'); cv.width = cv.height = SZ;
            var cx = cv.getContext('2d');
            if (m) { cx.translate(SZ, 0); cx.scale(-1, 1); }
            try { cx.drawImage(img, 0, 0, SZ, SZ); } catch (e) { /* blank */ }
            var tx = new THREE.CanvasTexture(cv);
            if (THREE.sRGBEncoding) tx.encoding = THREE.sRGBEncoding;
            tex[p * 2 + m] = tx;
          }
          if (--left === 0) { MLEN = 1; MHT = 0.7; done(); }
        };
        img.src = 'assets/bee-v2/' + nm + '.svg';
      });
    }
    function make() {
      var obj = new THREE.Group();
      var sm = new THREE.SpriteMaterial({ map: tex[0], transparent: true, depthWrite: false });
      var sp = new THREE.Sprite(sm); sp.center.set(0.47, 0.56); obj.add(sp);
      return { obj: obj, set: function (st) {
        var f = Math.max(0.14, Math.abs(st.face)), mir = st.face < 0 ? 1 : 0;
        var p = st.carry ? 1 : st.mood === 'happy' ? 2 : st.mood === 'think' ? 3 : 0, map = tex[p * 2 + mir];
        obj.position.set(st.x, -st.y, st.z * LEN * 0.5);
        if (sm.map !== map) sm.map = map;
        sm.rotation = st.pitch;
        sp.center.x = mir ? 0.53 : 0.47;
        var size = LEN * 1.15 * st.k;
        sp.scale.set(size * f, size, 1);
      } };
    }
    return { load: load, make: make };
  })();

  /* ---------------- the bees ---------------- */
  var bees = [], ST = { x: 0, y: 0, z: 0, k: 1, face: 1, pitch: 0, roll: 0, carry: false, jet: 0, t: 0, mood: 'neutral', blink: 0, headRoll: 0, headNod: 0, snap: false };
  var ptr = { x: -1e4, y: -1e4, vx: 0, vy: 0, t: -9, lx: 0, ly: 0, lt: 0 };
  var scr = { v: 0, last: 0, d: 0, at: -9, surp: -9 };
  var mgr = { recruit: 0, peel: 0, recycle: 0, notice: 0, noticeCd: 4 };
  var SLOTS = [{ x: 0.84, y: 0.32 }, { x: 0.12, y: 0.6 }, { x: 0.74, y: 0.74 }];
  var P = { x: 0, y: 0 }, P2 = { x: 0, y: 0 };

  function newBee(look, i) {
    return { look: look, i: i, x: W + LEN * (1 + i * 0.5), y: SY + H * (0.25 + 0.12 * i), z: 1, tz: 1, vx: -120, vy: 0, ax: 0, ay: 0,
      tx: 0, ty: 0, maxV: 300, maxA: 1500, gain: 5, slowR: 140, hold: false,
      face: -1, faceT: -1, pitch: 0, roll: 0, bob: 0, ry: 0, jet: 0.5, ph: Math.random() * 6.283,
      task: null, last: '', base: 'neutral', mood: 'neutral', moodT: '', moodUntil: 0,
      blinkAt: rnd(1, 4), blinkT: -9, look2: null, lx: 0, ly: 0, pitchBias: 0, headRoll: 0, carry: false,
      chase: 0, hitCd: 0, noticeCd: 0, peeled: -9, slot: -1 };
  }
  function speedScale() { return Math.sqrt(LEN / 150); } // smaller bees fly a little slower, not half as fast
  function setMood(b, m, dur, t) {
    if (b.moodT === 'angry' && t < b.moodUntil && m !== 'angry') return;
    b.moodT = m; b.moodUntil = t + dur;
  }
  function release(b) {
    var tk = b.task;
    if (!tk) return;
    if (tk.res) delete L.res[tk.res];
    var c = HIVE.comb;
    if (c && tk.cells) tk.cells.forEach(function (i) { c.busy(i, false); });
    b.carry = false; b.hold = false; b.pitchBias = 0; b.headRoll = 0; b.look2 = null; b.slot = -1;
    b.task = null;
  }
  function setTask(b, tk, t) {
    release(b);
    if (!tk) return;
    tk.t0 = t; tk.phase = tk.phase || 'go';
    b.task = tk; b.last = tk.kind;
    if (tk.res) L.res[tk.res] = b;
  }
  // is a spot free of other bees (their targets and their bodies)?
  function spotFree(x, y, self) {
    for (var i = 0; i < bees.length; i++) {
      var o = bees[i];
      if (o === self) continue;
      if (hyp(o.tx - x, o.ty - y) < LEN * 0.95) return false;
      if (o.hold && hyp(o.x - x, o.y - y) < LEN * 0.95) return false;
    }
    return true;
  }

  /* ---- comb helpers (the comb lives in home.js and shares a small API as HIVE.comb) ---- */
  var combRect = null;
  function combOn() { var c = HIVE.comb; return !!(c && c.live()); }
  function cellDoc(i, out) { HIVE.comb.screen(i, out, combRect); out.y += SY; return out; }
  function inCombZone(sx, sy) { return sx > HIVE.comb.edge() * W + LEN * 0.55 && sx < W - LEN * 0.45 && sy > H * 0.2 && sy < H * 0.86; }
  function freeCells() {
    var c = HIVE.comb, out = [];
    for (var i = 0; i < c.count; i++) {
      if (!c.free(i)) continue;
      c.screen(i, P, combRect);
      if (inCombZone(P.x, P.y)) out.push({ i: i, x: P.x, y: P.y });
    }
    return out;
  }

  /* ---- task builders: each returns a task or null ---- */
  function mkCarry(b) {
    if (!combOn() || HIVE.comb.T() > 3.3) return null;
    var n = 0; bees.forEach(function (o) { if (o.task && o.task.kind === 'carry') n++; });
    if (n >= 2) return null;
    var list = freeCells();
    if (list.length < 2) return null;
    var a = pick(list), bs = list.filter(function (c) { var d = hyp(c.x - a.x, c.y - a.y); return c !== a && d > LEN * 1.1 && d < LEN * 4.2; });
    if (!bs.length) return null;
    var z = pick(bs);
    HIVE.comb.busy(a.i, true); HIVE.comb.busy(z.i, true);
    return { kind: 'carry', a: a.i, b: z.i, cells: [a.i, z.i], phase: 'toA' };
  }
  function mkCell(b) {
    if (!combOn() || HIVE.comb.T() > 6.6) return null;
    if (bees.filter(function (o) { return o.task && o.task.kind === 'cell'; }).length >= 3) return null;
    var list = freeCells().filter(function (c) { return spotFree(c.x, c.y + SY - BH, b); });
    if (!list.length) return null;
    var c = pick(list);
    HIVE.comb.busy(c.i, true);
    var side = Math.random() < 0.5 ? -1 : 1;
    return { kind: 'cell', i: c.i, cells: [c.i], side: side, dur: rnd(2.2, 3.6) };
  }
  function linesInView() {
    var out = [];
    L.heads.forEach(function (h) { if (h.ready) h.lines.forEach(function (ln, li) { if (onScreenY(ln.t, 10) && onScreenY(ln.b, 10)) out.push({ h: h, ln: ln, li: li }); }); });
    return out;
  }
  function mkWeave(b) {
    var n = 0; bees.forEach(function (o) { if (o.task && o.task.kind === 'weave') n++; });
    if (n >= 2) return null;
    var ls = linesInView().filter(function (o) { return !L.res['w' + o.ln.key]; });
    if (!ls.length) return null;
    var o = pick(ls), dir = b.x < (o.ln.l + o.ln.r) / 2 ? 1 : -1;
    var x0 = dir > 0 ? o.ln.l - LEN * 0.75 : o.ln.r + LEN * 0.75, x1 = dir > 0 ? o.ln.r + LEN * 0.75 : o.ln.l - LEN * 0.75;
    // per word: does the bee pass behind it or in front of it (alternating), and through the line, above it or below it
    var par = Math.random() < 0.5 ? 0 : 1, lanes = [], behind = [], through = 0;
    o.ln.words.forEach(function (w, i) {
      behind.push((i + par) % 2 === 0);
      var r = Math.random(), lane = r < 0.62 ? 0 : r < 0.84 ? -1 : (o.li === 0 && o.h.lines.length > 1 ? -1 : 1);
      lanes.push(lane); if (!lane) through++;
    });
    if (!through && lanes.length) lanes[Math.floor(Math.random() * lanes.length)] = 0; // it always threads at least one word
    return { kind: 'weave', ln: o.ln, x0: x0, x1: x1, u: 0, ph: rnd(0, 6.28), behind: behind, lanes: lanes, res: 'w' + o.ln.key, phase: 'in' };
  }
  function mkPerchLetter(b) {
    var cands = [];
    linesInView().forEach(function (o) {
      if (o.li !== 0) return; // only the top line: a bee sitting on a lower line would cover the line above
      for (var r in L.res) if (r.indexOf('c' + o.ln.key + ':') === 0) return; // one bee per line, never a row of them
      o.ln.chars.forEach(function (c, ci) {
        var y = c.top - BH * 0.34, key = 'c' + o.ln.key + ':' + ci;
        if (c.tall && !L.res[key] && spotFree(c.cx, y, b) && onScreenY(y - BH * 0.4, 0)) cands.push({ c: c, y: y, key: key });
      });
    });
    if (!cands.length) return null;
    var p = pick(cands);
    return { kind: 'visit', mode: 'perch', x: p.c.cx, y: p.y, lx: p.c.cx + 160 * (Math.random() < 0.5 ? -1 : 1), ly: p.y, res: p.key, dur: rnd(2.4, 5) };
  }
  function mkPoi(b, filter) {
    var cands = [];
    L.pois.forEach(function (p) {
      if (L.res[p.key] || !onScreenY(p.y - BH * 0.45, 0) || !onScreenY(p.y + BH * 0.3, 0)) return;
      if (filter && !filter(p)) return;
      if (p.group && bees.some(function (o) { return o.task && o.task.group === p.group; })) return; // one bee on the bars at a time
      var x = rnd(p.x0, p.x1);
      if (x < LEN * 0.4 || x > W - LEN * 0.4 || !spotFree(x, p.y, b)) return;
      var d = hyp(x - b.x, p.y - b.y);
      cands.push({ p: p, x: x, w: 1 / (1 + d / 500) });
    });
    if (!cands.length) return null;
    var sum = 0; cands.forEach(function (c) { sum += c.w; });
    var r = Math.random() * sum, c = cands[0];
    for (var i = 0; i < cands.length; i++) { r -= cands[i].w; if (r <= 0) { c = cands[i]; break; } }
    return { kind: 'visit', mode: c.p.kind, x: c.x, y: c.p.y, lx: c.p.kind === 'inspect' ? c.p.lx : c.x + 160 * (Math.random() < 0.5 ? -1 : 1), ly: c.p.ly, res: c.p.key, group: c.p.group, dur: c.p.kind === 'inspect' ? rnd(2, 3.4) : rnd(2.6, 5.2) };
  }
  function mkWander(b) {
    for (var k = 0; k < 14; k++) {
      var x = W * rnd(0.06, 0.94), y = SY + H * rnd(0.2, 0.82);
      if (clearAt(x, y, null) && spotFree(x, y, b)) return { kind: 'visit', mode: 'hover', x: x, y: y, lx: x + (Math.random() < 0.5 ? -1 : 1) * 200, ly: y, dur: rnd(1.4, 2.8) };
    }
    return { kind: 'visit', mode: 'hover', x: clamp(b.x, LEN, W - LEN), y: clamp(b.y, SY + H * 0.2, SY + H * 0.8), lx: b.x + 100, ly: b.y, dur: 1.2 };
  }
  function pickTask(b, t) {
    var opts = [['carry', 3.2, mkCarry], ['cell', 1.1, mkCell], ['weave', 1.7, mkWeave], ['letter', 1.3, mkPerchLetter], ['poi', 2.6, mkPoi], ['wander', 0.6, mkWander]];
    while (opts.length) {
      var sum = 0, i;
      for (i = 0; i < opts.length; i++) sum += opts[i][1] * (opts[i][0] === b.lastPick ? 0.4 : 1);
      var r = Math.random() * sum, o = opts[0], oi = 0;
      for (i = 0; i < opts.length; i++) { r -= opts[i][1] * (opts[i][0] === b.lastPick ? 0.4 : 1); if (r <= 0) { o = opts[i]; oi = i; break; } }
      var tk = o[2](b);
      if (tk) { b.lastPick = o[0]; setTask(b, tk, t); return; }
      opts.splice(oi, 1);
    }
    setTask(b, mkWander(b), t);
  }

  /* ---- running a task: sets the steering target, speed, depth, look and mood; false = finished ---- */
  function hoverDefaults(b) {
    var s = speedScale();
    b.maxV = 300 * s; b.maxA = 1500 * s; b.gain = 5; b.slowR = Math.max(120, LEN); b.tz = 1; b.hold = false; b.look2 = null; b.pitchBias = 0; b.headRoll = 0; b.base = 'neutral';
  }
  function runTask(b, t, dt) {
    var tk = b.task, p, d;
    hoverDefaults(b);
    switch (tk.kind) {
      case 'wait':
        b.tx = b.x; b.ty = b.y; b.maxV = 0;
        return t - tk.t0 < tk.dur;

      case 'carry': {
        if (!combOn()) return false;
        var A = cellDoc(tk.a, P), above = BH * 0.52 + 6;
        if (tk.phase === 'toA') {
          b.tx = A.x; b.ty = A.y - above;
          if (!inCombZone(A.x, A.y - SY)) return false;
          if (hyp(b.tx - b.x, b.ty - b.y) < 14) { tk.phase = 'pick'; tk.t0 = t; }
        } else if (tk.phase === 'pick' || tk.phase === 'drop') {
          var C = tk.phase === 'pick' ? A : cellDoc(tk.b, P2);
          p = clamp((t - tk.t0) / 0.7, 0, 1);
          b.tx = C.x; b.ty = C.y - above + 16 * Math.sin(Math.PI * p); b.hold = true; b.slowR = 40;
          b.pitchBias = -0.18 * Math.sin(Math.PI * p);
          if (tk.phase === 'pick' && p > 0.5 && !b.carry) { b.carry = true; HIVE.comb.flash(tk.a, 0.45); }
          if (tk.phase === 'drop' && p > 0.5 && b.carry) { b.carry = false; HIVE.comb.flash(tk.b, 1); setMood(b, 'happy', 1.8, t); }
          if (p >= 1) { if (tk.phase === 'drop') return false; tk.phase = 'toB'; tk.t0 = t; }
        } else { // toB, carrying
          var B = cellDoc(tk.b, P2);
          if (!inCombZone(B.x, B.y - SY)) return false;
          b.tx = B.x; b.ty = B.y - above; b.maxV *= 0.8;
          if (hyp(b.tx - b.x, b.ty - b.y) < 14) { tk.phase = 'drop'; tk.t0 = t; }
        }
        return true;
      }

      case 'cell': {
        if (!combOn()) return false;
        var Cc = cellDoc(tk.i, P);
        b.tx = Cc.x + tk.side * LEN * 0.32; b.ty = Cc.y - BH * 0.62;
        b.look2 = true; b.lx = Cc.x; b.ly = Cc.y;
        if (tk.phase === 'go') {
          if (hyp(b.tx - b.x, b.ty - b.y) < 16) { tk.phase = 'stay'; tk.t0 = t; HIVE.comb.flash(tk.i, 0.3); }
        } else {
          p = (t - tk.t0) / tk.dur;
          b.hold = true; b.slowR = 60;
          b.tx += Math.sin(t * 0.9 + b.ph) * 4; b.ty += Math.cos(t * 1.3 + b.ph) * 3;
          b.pitchBias = -0.32; b.base = p < 0.5 ? 'curious' : 'think';
          if (p >= 1) { setMood(b, Math.random() < 0.5 ? 'happy' : 'neutral', 1, t); return false; }
        }
        return true;
      }

      case 'weave': {
        var ln = tk.ln, span = Math.abs(tk.x1 - tk.x0);
        b.base = 'happy';
        if (tk.phase === 'in') {
          laneAt(tk, tk.x0 < tk.x1 ? -1e9 : 1e9); b.tx = tk.x0; b.ty = WV.y; b.tz = 1;
          if (!onScreenY(ln.t, 0)) return false;
          if (hyp(b.tx - b.x, b.ty - b.y) < 26) { tk.phase = 'thread'; tk.t0 = t; }
        } else if (tk.phase === 'thread') {
          tk.u += dt * 150 * speedScale() / span;
          var u = clamp(tk.u, 0, 1);
          b.tx = lerp(tk.x0, tk.x1, u);
          laneAt(tk, b.tx);
          b.ty = WV.y + Math.sin(u * Math.PI * 2.2 + tk.ph) * ln.h * 0.06;
          b.maxV = 380 * speedScale(); b.slowR = 50; b.gain = 7;
          // depth follows whole words: fully behind one word, fully in front of the next (only shading/scale here;
          // which glyphs cover the bee is decided per word in frontLetters)
          b.tz = WV.z;
          if (u >= 1) { tk.phase = 'out'; tk.t0 = t; }
        } else {
          b.tx = tk.x1 + (tk.x1 > tk.x0 ? 1 : -1) * 40; b.ty = ln.cy - 30; b.tz = 1;
          if (t - tk.t0 > 0.7) return false;
        }
        return true;
      }

      case 'visit': {
        b.tx = tk.x; b.ty = tk.y; b.look2 = null;
        if (tk.phase === 'go') {
          d = hyp(tk.x - b.x, tk.y - b.y);
          if (tk.mode === 'perch' && d > 70) b.ty = tk.y - 26; // land from just above
          b.slowR = Math.max(110, LEN * 0.8);
          if (d < 9 && hyp(b.vx, b.vy) < 70) {
            tk.phase = 'stay'; tk.t0 = t;
            tk.mood = tk.mode === 'inspect' ? 'curious' : tk.mode === 'hover' ? 'neutral' : pick(['neutral', 'happy', 'think', 'neutral']);
            tk.glance = t + rnd(0.8, 1.8);
          }
          if (t - tk.t0 > 7) return false; // could not get there
        } else if (tk.phase === 'stay') {
          p = (t - tk.t0) / tk.dur;
          b.hold = true; b.slowR = 40;
          if (tk.mode !== 'perch') { b.tx += Math.sin(t * 0.9 + b.ph) * 4; b.ty += Math.cos(t * 1.3 + b.ph) * 3; }
          if (tk.mode === 'inspect') { b.look2 = true; b.lx = tk.lx; b.ly = tk.ly; b.pitchBias = -0.28; b.base = p < 0.55 ? 'curious' : 'think'; }
          else {
            b.base = tk.mood;
            if (t > tk.glance) { tk.glance = t + rnd(1.1, 2.6); tk.side = -(tk.side || 1); }
            b.look2 = true; b.lx = tk.x + (tk.side || 1) * 200; b.ly = tk.y + rnd(-2, 2);
          }
          if (p >= 1) { tk.phase = 'leave'; tk.t0 = t; }
          // the page scrolled the spot up under the header: leave once the scroll settles
          var sy0 = tk.y - SY;
          if (sy0 > -BH && sy0 < 84 + BH * 0.3 && t - scr.at > 0.5) { tk.phase = 'leave'; tk.t0 = t; }
        } else {
          b.ty = tk.y - BH * 0.6;
          if (t - tk.t0 > 0.45) return false;
        }
        return true;
      }

      case 'follow': {
        var sl = SLOTS[b.slot >= 0 ? b.slot : 0];
        b.tx = W * sl.x + Math.sin(t * 0.7 + b.ph) * 24;
        b.ty = SY + H * sl.y + Math.cos(t * 0.9 + b.ph) * 16;
        b.maxV = Math.max(300 * speedScale(), Math.abs(scr.v) * 1.25 + 160);
        b.maxA = 3600 * speedScale(); b.gain = 6;
        return t - scr.at < 0.75;
      }

      case 'cursor': {
        var px = ptr.x, py = ptr.y + SY;
        b.look2 = true; b.lx = px; b.ly = py; b.base = 'curious';
        if (tk.phase === 'go') { // noticed: stop, turn, a little start
          b.tx = b.x; b.ty = b.y; b.hold = true; b.slowR = 30;
          if (t - tk.t0 < 0.3) b.base = 'surprised';
          if (t - tk.t0 > 0.55) { tk.phase = 'follow'; tk.t0 = t; }
        } else {
          var dx = b.x - px, dy = b.y - py, dd = hyp(dx, dy) || 1, keep = LEN * 0.95;
          b.tx = px + dx / dd * keep; b.ty = py + dy / dd * keep;
          b.maxV = 420 * speedScale(); b.gain = 4;
          if (t - tk.t0 > tk.dur || t - ptr.t > 2.5) { setMood(b, 'happy', 0.9, t); return false; }
        }
        return true;
      }

      case 'flee': {
        b.base = 'angry';
        if (tk.phase === 'go') { // fuming: a little shake, glaring at the pointer
          b.look2 = true; b.lx = ptr.x; b.ly = ptr.y + SY;
          b.tx = tk.x + Math.sin(t * 46) * 4; b.ty = tk.y + Math.cos(t * 39) * 2; b.hold = true; b.slowR = 20; b.gain = 12;
          if (t - tk.t0 > 0.8) {
            tk.phase = 'dart'; tk.t0 = t;
            tk.x = ptr.x < W / 2 ? W - LEN * 0.7 : LEN * 0.7; tk.y = SY + (ptr.y < H / 2 ? H * 0.78 : H * 0.28);
          }
        } else if (tk.phase === 'dart') {
          b.tx = tk.x; b.ty = tk.y; b.maxV = 1150 * speedScale(); b.maxA = 5200 * speedScale(); b.gain = 8; b.slowR = 160;
          if (hyp(tk.x - b.x, tk.y - b.y) < 30 || t - tk.t0 > 1.6) { tk.phase = 'sulk'; tk.t0 = t; }
        } else {
          b.tx = tk.x; b.ty = tk.y; b.hold = true; b.slowR = 60;
          if (t - tk.t0 > 1.5) { b.chase = 0; b.noticeCd = t + 25; return false; }
        }
        return true;
      }
    }
    return false;
  }

  /* ---- weave lanes: for x on a line, the height to fly at (through / above / below each word) and the depth ---- */
  var WV = { y: 0, z: 1 };
  function laneY(ln, lane) { return lane < 0 ? ln.top - BH * 0.42 : lane > 0 ? ln.b + BH * 0.3 : ln.cy; }
  function laneZ(tk, i) { return tk.lanes[i] ? 0.4 : tk.behind[i] ? -0.6 : 0.85; }
  function laneAt(tk, x) {
    var ws = tk.ln.words, n = ws.length, i;
    if (!n || x <= ws[0].l) { WV.y = n ? laneY(tk.ln, tk.lanes[0]) : tk.ln.cy; WV.z = n ? laneZ(tk, 0) : 1; return; }
    for (i = 0; i < n; i++) {
      var w = ws[i];
      if (x <= w.r) { WV.y = laneY(tk.ln, tk.lanes[i]); WV.z = laneZ(tk, i); return; }
      if (i + 1 < n && x < ws[i + 1].l) { // in the gap: ease from this word's lane to the next one's
        var k = smooth01((x - w.r) / Math.max(1, ws[i + 1].l - w.r));
        WV.y = lerp(laneY(tk.ln, tk.lanes[i]), laneY(tk.ln, tk.lanes[i + 1]), k);
        WV.z = lerp(laneZ(tk, i), laneZ(tk, i + 1), k);
        return;
      }
    }
    WV.y = laneY(tk.ln, tk.lanes[n - 1]); WV.z = laneZ(tk, n - 1);
  }
  function smooth01(v) { v = clamp(v, 0, 1); return v * v * (3 - 2 * v); }

  /* ---- steering: arrive + separation, acceleration-limited ---- */
  function steer(b, dt) {
    var dx = b.tx - b.x, dy = b.ty - b.y, d = hyp(dx, dy);
    var sp = b.maxV * Math.min(1, d / b.slowR);
    var wx = d > 0.01 ? dx / d * sp : 0, wy = d > 0.01 ? dy / d * sp : 0;
    var R = LEN * 0.95, sx = 0, sy = 0;
    for (var i = 0; i < bees.length; i++) {
      var o = bees[i];
      if (o === b) continue;
      var ox = b.x - o.x, oy = b.y - o.y, od = hyp(ox, oy);
      if (od < R && od > 0.001) { var f = (R - od) / R; sx += ox / od * f; sy += oy / od * f; }
    }
    var sepK = (b.hold ? 70 : 240) * speedScale();
    wx += sx * sepK; wy += sy * sepK;
    var ax = (wx - b.vx) * b.gain, ay = (wy - b.vy) * b.gain, am = hyp(ax, ay);
    if (am > b.maxA) { ax *= b.maxA / am; ay *= b.maxA / am; }
    b.vx += ax * dt; b.vy += ay * dt;
    b.x += b.vx * dt; b.y += b.vy * dt;
    var k = damp(7, dt);
    b.ax += (ax - b.ax) * k; b.ay += (ay - b.ay) * k;
  }

  /* ---- facing, pitch, bank, bob, depth, blink ---- */
  function orient(b, t, dt) {
    var v = hyp(b.vx, b.vy), s = speedScale();
    if (b.look2) { var lx = b.lx - b.x; if (Math.abs(lx) > 24) b.faceT = lx > 0 ? 1 : -1; }
    else if (Math.abs(b.vx) > 28 * s) b.faceT = b.vx > 0 ? 1 : -1;
    b.face += (b.faceT - b.face) * damp(6.5, dt);
    var pt = 0;
    if (v > 30 * s && !b.hold) pt = clamp(Math.atan2(-b.vy, Math.abs(b.vx) + 60 * s), -0.75, 0.75) * 0.6;
    else if (b.look2) pt = clamp(Math.atan2(-(b.ly - b.y), Math.abs(b.lx - b.x) + 80), -0.5, 0.5) * 0.45;
    pt += b.pitchBias;
    b.pitch += (pt - b.pitch) * damp(6, dt);
    // banking: roll about the body with the turn (lateral acceleration), so it shows its back or belly
    var lat = v > 1 ? (b.vx * b.ay - b.vy * b.ax) / v : 0;
    var rt = clamp(lat / (2600 * s), -0.42, 0.42) + (b.task && b.task.kind === 'flee' && b.task.phase === 'go' ? Math.sin(t * 52) * 0.08 : 0);
    b.roll += (rt - b.roll) * damp(5, dt);
    b.z += (b.tz - b.z) * damp(b.task && b.task.kind === 'weave' ? 5 : 3, dt);
    var perched = b.hold && b.task && b.task.kind === 'visit' && b.task.mode === 'perch';
    var amp = (perched ? 0.8 : 5 * (1 - 0.5 * clamp(v / (300 * s), 0, 1))) * s;
    b.bob += (amp - b.bob) * damp(4, dt);
    b.ry = b.y + Math.sin(t * 2.4 + b.ph) * b.bob;
    b.jet += (clamp(v / (320 * s) + hyp(b.ax, b.ay) / (2500 * s), perched ? 0.08 : 0.25, 1) - b.jet) * damp(6, dt);
    // blinks: random, sometimes double
    if (t > b.blinkAt) { b.blinkT = t; b.blinkAt = t + (Math.random() < 0.2 ? 0.28 : rnd(2.2, 6)); }
  }
  function blinkOf(b, t) { var a = (t - b.blinkT) / 0.15; return a < 0 || a > 1 ? 0 : 1 - Math.abs(a * 2 - 1); }

  /* ---- headline depth, whole words at a time: while a bee threads a line, every word it passes BEHIND has
     its glyph copies shown above the overlay (covering the bee); every other word stays under the bee.
     The bee never changes depth inside a word, only in the gaps between words. ---- */
  function frontLetters() {
    var h, ln, w, i, j, k, c;
    for (i = 0; i < L.heads.length; i++) { h = L.heads[i]; for (j = 0; j < h.lines.length; j++) { ln = h.lines[j]; for (k = 0; k < ln.words.length; k++) ln.words[k].want = false; } }
    for (var n = 0; n < bees.length; n++) {
      var b = bees[n], tk = b.task;
      if (!tk || tk.kind !== 'weave' || !tk.ln || !tk.ln.words) continue;
      var kz = 0.93 + 0.07 * b.z, hw = LEN * 0.46 * kz, ht = BH * 0.48 * kz, hb = BH * 0.42 * kz;
      ln = tk.ln;
      for (k = 0; k < ln.words.length; k++) {
        w = ln.words[k];
        if (tk.behind[k] && !tk.lanes[k] && b.x + hw > w.l && b.x - hw < w.r && b.ry + hb > w.t && b.ry - ht < w.b) w.want = true;
      }
    }
    for (i = 0; i < L.heads.length; i++) { h = L.heads[i]; for (j = 0; j < h.lines.length; j++) { ln = h.lines[j]; for (k = 0; k < ln.words.length; k++) {
      w = ln.words[k];
      if (w.want === w.on) continue;
      w.on = w.want;
      for (c = 0; c < w.chars.length; c++) w.chars[c].sp.style.visibility = w.on ? 'visible' : 'hidden';
    } } }
  }

  /* ---- the visitor: scroll (follow / peel off / fly in) and the pointer (notice / chase) ---- */
  function scrollTrack(t, dt) {
    scr.d = SY - scr.last;
    var raw = dt > 0 ? scr.d / dt : 0;
    scr.last = SY;
    if (Math.abs(raw) > 30) scr.at = t;
    scr.v += (raw - scr.v) * damp(9, dt);
  }
  function near(b) { return (b.y > SY - H * 0.15 && b.y < SY + H * 1.15) || (b.task && b.ty > SY - H * 0.1 && b.ty < SY + H * 1.1); }
  function visible(b) { var s = b.y - SY; return s > -BH && s < H + BH && b.x > -LEN && b.x < W + LEN; }
  function busyKind(b) { var k = b.task && b.task.kind; return k === 'carry' || k === 'cursor' || k === 'flee' || k === 'wait'; }

  function manage(t) {
    var scrolling = t - scr.at < 0.3, i, b, followers = 0;
    for (i = 0; i < bees.length; i++) if (bees[i].task && bees[i].task.kind === 'follow') followers++;

    // fast scroll: a start of surprise from the bees on screen
    if (Math.abs(scr.v) > 2300 && t - scr.surp > 1.4) {
      scr.surp = t;
      bees.forEach(function (o) { if (visible(o)) setMood(o, 'surprised', 0.9, t); });
    }
    // a few bees follow the visitor down (or up) the page
    if (scrolling && t > mgr.recruit) {
      mgr.recruit = t + 0.28;
      var want = Math.min(Math.abs(scr.v) > 900 ? 3 : 2, N - 1);
      if (followers < want) {
        var best = null, bd = 1e9;
        for (i = 0; i < bees.length; i++) {
          b = bees[i];
          if (busyKind(b) || (b.task && b.task.kind === 'follow') || t - b.peeled < 3.5) continue;
          if (b.hold && b.task && b.task.kind === 'visit' && b.ph > 3.6) continue; // some stay where they sit
          var dd = Math.abs(b.y - (SY + H / 2)) + Math.abs(b.x - W / 2) * 0.3;
          if (dd < bd) { bd = dd; best = b; }
        }
        if (best && bd < H * 1.2) {
          var used = {}; bees.forEach(function (o) { if (o.slot >= 0) used[o.slot] = 1; });
          var sl = 0; while (used[sl] && sl < SLOTS.length - 1) sl++;
          setTask(best, { kind: 'follow' }, t); best.slot = sl;
        }
      }
    }
    // on the way, one of them peels off to something in the section coming into view
    if (scrolling && followers > 0 && t > mgr.peel) {
      mgr.peel = t + 0.65;
      if (Math.random() < 0.45) {
        var dir = scr.v >= 0 ? 1 : -1, fs = bees.filter(function (o) { return o.task && o.task.kind === 'follow'; }), f = pick(fs);
        var tk = mkPoi(f, function (p) { var s = p.y - SY; return dir > 0 ? s > H * 0.5 : s < H * 0.5; });
        if (tk) { setTask(f, tk, t); f.peeled = t; }
      }
    }
    // always a few bees around: one far behind is quietly brought back, flying in from off-screen
    if (t > mgr.recycle) {
      var present = 0, seen = 0, far = null, fd = 0;
      for (i = 0; i < bees.length; i++) {
        b = bees[i];
        if (near(b)) present++;
        if (visible(b)) seen++;
        var away = b.y < SY - H * 0.5 ? SY - b.y : b.y > SY + H * 1.5 ? b.y - SY - H : 0;
        if (away > fd && !visible(b) && (!b.task || b.task.kind !== 'flee')) { fd = away; far = b; }
      }
      mgr.recycle = t + (seen < 3 ? 0.18 : 0.45); // hurry when the screen is nearly empty
      if (present < bees.length && far) respawn(far, t, scrolling);
    }
    // the pointer: now and then a nearby bee notices it
    if (fine && t > mgr.notice) {
      mgr.notice = t + 0.4;
      if (t > mgr.noticeCd && t - ptr.t < 1.5 && Math.random() < 0.14) {
        var nb = null, nd = Math.max(LEN * 2.4, 260);
        for (i = 0; i < bees.length; i++) {
          b = bees[i];
          if (busyKind(b) || t < b.noticeCd || (b.task && b.task.kind === 'follow' && scrolling)) continue;
          var pd = hyp(b.x - ptr.x, b.ry - SY - ptr.y);
          if (pd < nd) { nd = pd; nb = b; }
        }
        if (nb) {
          setTask(nb, { kind: 'cursor', dur: rnd(2, 4) }, t);
          mgr.noticeCd = t + rnd(7, 13); nb.noticeCd = t + 18;
        }
      }
    }
  }
  function respawn(b, t, scrolling) {
    release(b);
    var dir = scr.v >= 0 ? 1 : -1, s = speedScale();
    if (Math.random() < 0.6 || Math.abs(scr.v) < 100) {
      var left = Math.random() < 0.5;
      b.x = left ? -LEN : W + LEN; b.y = SY + H * rnd(0.28, 0.72); b.vx = (left ? 260 : -260) * s; b.vy = 0;
    } else {
      b.x = W * rnd(0.15, 0.85); b.y = dir > 0 ? SY + H + BH : SY - BH; b.vx = 0; b.vy = -dir * 260 * s;
    }
    b.z = b.tz = 1; b.face = b.faceT = b.vx < 0 ? -1 : 1; b.chase = 0;
    var followers = bees.filter(function (o) { return o.task && o.task.kind === 'follow'; }).length;
    if (scrolling && followers < 3) {
      var used = {}; bees.forEach(function (o) { if (o.slot >= 0) used[o.slot] = 1; });
      var sl = 0; while (used[sl] && sl < SLOTS.length - 1) sl++;
      setTask(b, { kind: 'follow' }, t); b.slot = sl;
    } else pickTask(b, t);
  }
  // a pointer rushing at a bee: it dodges; chased again and again, it gets angry and darts away
  function chase(t, dt) {
    var ps = hyp(ptr.vx, ptr.vy);
    for (var i = 0; i < bees.length; i++) {
      var b = bees[i];
      b.chase = Math.max(0, b.chase - dt * 0.2);
      if (!fine || t - ptr.t > 0.2 || ps < 450 || t < b.hitCd) continue;
      var dx = b.x - ptr.x, dy = b.ry - SY - ptr.y, d = hyp(dx, dy);
      if (d > LEN * 0.8 || ptr.vx * dx + ptr.vy * dy <= 0) continue;
      b.hitCd = t + 0.5; b.chase += 1;
      var s = speedScale();
      b.vx += dx / (d || 1) * 460 * s; b.vy += dy / (d || 1) * 460 * s;
      setMood(b, 'surprised', 0.7, t);
      if (b.chase >= 3 && (!b.task || b.task.kind !== 'flee')) {
        setTask(b, { kind: 'flee', x: b.x, y: b.y }, t);
        setMood(b, 'angry', 3.4, t);
      }
    }
  }

  /* ---------------- frame ---------------- */
  var raf = 0, lastT = -1, built = false, drawnEmpty = false;
  function frame(ts, manual) {
    if (!manual) raf = 0;
    if (HIVE.bees && HIVE.bees.paused && !manual) { SY = win.scrollY || 0; draw(lastT, false); schedule(); return; }
    var t = ts / 1000, dt = lastT < 0 ? 0.016 : clamp(t - lastT, 0, 0.05);
    lastT = t;
    SY = win.scrollY || win.pageYOffset || 0;
    try {
      if (t > L.next) layout(false);
      scrollTrack(t, dt);
      combRect = combOn() ? HIVE.comb.rect() : null;
      manage(t);
      chase(t, dt);
      for (var i = 0; i < bees.length; i++) {
        var b = bees[i];
        if (!b.task) pickTask(b, t);
        if (!runTask(b, t, dt)) { release(b); pickTask(b, t); runTask(b, t, dt); }
        // followers ride most of the scroll with the visitor (and steer the rest), so even a fast fling keeps them close
        if (b.task.kind === 'follow' && scr.d) b.y += clamp(scr.d, -H * 0.5, H * 0.5) * 0.85;
        steer(b, dt);
        orient(b, t, dt);
      }
      frontLetters();
      draw(t, false);
    } catch (e) { warn(e); }
    if (!manual) schedule();
  }
  function draw(t, snap) {
    var any = false;
    for (var i = 0; i < bees.length; i++) {
      var b = bees[i], st = ST;
      var vis = visible(b);
      b.look.obj.visible = vis;
      if (!vis) continue;
      any = true;
      st.x = b.x; st.y = b.ry - SY; st.z = b.z; st.k = (0.93 + 0.07 * b.z) * (HIVE.bees && HIVE.bees.zoom || 1);
      st.face = b.face; st.pitch = b.pitch * b.face; st.roll = b.roll * (b.face >= 0 ? 1 : -1);
      st.carry = b.carry; st.jet = b.jet; st.t = t; st.snap = snap;
      st.mood = t < b.moodUntil ? b.moodT : b.base;
      st.blink = reduced ? 0 : blinkOf(b, t);
      st.headRoll = b.headRoll; st.headNod = 0;
      b.look.set(st);
    }
    if (any || !drawnEmpty) { renderer.render(scene, cam); drawnEmpty = !any; }
  }
  function schedule() { if (!raf && !doc.hidden && !reduced) raf = requestAnimationFrame(frame); }

  /* ---------------- reduced motion: a few still bees, perched, moving only with the page ---------------- */
  function placeStill() {
    layout(true);
    var spots = [];
    L.heads.forEach(function (h) { if (h.ready && h.lines[0]) { var cs = h.lines[0].chars.filter(function (c) { return c.tall; }); if (cs.length) { var c = cs[Math.floor(cs.length * 0.62)]; spots.push({ x: c.cx, y: c.top - BH * 0.34, f: -1 }); } } });
    ['#giant-num', '.stat .num', '.roadmap h3', '.footer-mark span'].forEach(function (sel) {
      var p = L.pois.filter(function (q) { return q.el.matches(sel); })[0];
      if (p) spots.push({ x: (p.x0 + p.x1) / 2, y: p.y, f: 1 });
    });
    bees.forEach(function (b, i) {
      var s = spots[i];
      b.look.obj.visible = !!s;
      if (!s) { b.y = -1e5; return; }
      b.x = s.x; b.y = b.ry = s.y; b.z = 1; b.face = b.faceT = s.f; b.base = 'neutral';
    });
  }
  var still = 0;
  function drawStill() {
    if (still) return;
    still = requestAnimationFrame(function () { still = 0; SY = win.scrollY || win.pageYOffset || 0; draw(performance.now() / 1000, true); });
  }

  /* ---------------- boot ---------------- */
  function build(make) {
    sizes();
    var n = reduced ? Math.min(N, 5) : N;
    for (var i = 0; i < n; i++) {
      var look = make();
      scene.add(look.obj);
      var b = newBee(look, i);
      bees.push(b);
    }
    built = true;
    // for debugging in the console (advance(s) runs the simulation s seconds at 60 fps, e.g. in a hidden tab)
    HIVE.bees = { list: bees, layout: L, ptr: ptr, now: function () { return lastT; }, size: function () { return { LEN: LEN, BH: BH, MLEN: MLEN, MHT: MHT }; },
      advance: function (s) { var ts = lastT < 0 ? performance.now() : lastT * 1000; for (var k = 0; k < s * 60; k++) { ts += 1000 / 60; frame(ts, true); } } };
    layout(true);
    if (reduced) { placeStill(); drawStill(); win.addEventListener('scroll', drawStill, { passive: true }); return; }
    // the landing: the bees arrive from the right, one after another
    var t = performance.now() / 1000;
    bees.forEach(function (b, i) { b.x = W + LEN * (0.8 + i * 0.25); b.y = SY + H * (0.2 + (i % 5) * 0.14 + (i > 4 ? 0.07 : 0)); setTask(b, { kind: 'wait', dur: 0.3 + i * 0.26 }, t); });
    scr.last = SY;
    schedule();
  }
  function start() {
    var LOOK = { make: null };
    modelLook.load(function () { LOOK.make = modelLook.make; build(LOOK.make); },
      function () { if (LOOK.make) return; spriteLook.load(function () { LOOK.make = spriteLook.make; build(LOOK.make); }); });
  }

  win.addEventListener('pointermove', function (e) {
    if (e.pointerType && e.pointerType !== 'mouse') return;
    var t = performance.now() / 1000, dt = Math.max(0.008, t - ptr.lt);
    var vx = (e.clientX - ptr.lx) / dt, vy = (e.clientY - ptr.ly) / dt;
    if (t - ptr.lt > 0.25) { vx = 0; vy = 0; }
    ptr.vx += (vx - ptr.vx) * 0.5; ptr.vy += (vy - ptr.vy) * 0.5;
    ptr.lx = ptr.x = e.clientX; ptr.ly = ptr.y = e.clientY; ptr.lt = ptr.t = t;
  }, { passive: true });
  doc.addEventListener('mouseleave', function () { ptr.t = -9; });
  doc.addEventListener('visibilitychange', function () {
    if (doc.hidden) { cancelAnimationFrame(raf); raf = 0; }
    else { lastT = -1; scr.last = win.scrollY || 0; if (built) schedule(); }
  });
  var rt = 0;
  win.addEventListener('resize', function () {
    clearTimeout(rt);
    rt = setTimeout(function () {
      sizes(); // (setSize clears the canvas: draw again at once, so there is no blank frame)
      if (!built) return;
      layout(true);
      if (reduced) { placeStill(); drawStill(); } else { drawnEmpty = false; draw(lastT > 0 ? lastT : performance.now() / 1000, false); }
    }, 150);
  });
  win.addEventListener('load', function () { if (built) layout(true); });
  if (doc.fonts && doc.fonts.ready) doc.fonts.ready.then(function () { if (built) layout(true); });
  canvas.addEventListener('webglcontextlost', function (e) {
    e.preventDefault();
    cancelAnimationFrame(raf); raf = 0; reduced = true; // stops the loop for good
    canvas.style.display = 'none'; front.style.display = 'none';
  });
  try { start(); } catch (e) { warn(e); }
})();
