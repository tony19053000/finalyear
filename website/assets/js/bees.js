/* hive. — the hero bees (home page only)
   Five Buzz robot bees live in the hero and nowhere else. They sit on ONE transparent fixed WebGL overlay
   (pointer-events: none, DPR capped at 1.5, under the sticky header), so every link under them still
   takes the click.

   Each bee has a role:
   - two "letter" bees glide slowly along the headline ("Many laptops. One hive."), half a lap apart, on a
     gentle weave that passes over one word and under the next, line by line and back, never stopping on a word.
   - two "comb" bees stay over the honeycomb on the right (the "fly zone": right of the hero text on wide
     screens, the band under the buttons on narrow ones), hover with a gentle buzz and, one at a time,
     visit a comb cell: settle on its top, the cell glows (HIVE.comb.flash), the bee looks happy.
   - one "wanderer" roams the whole hero, resting only where it hides no text and no button.
   Download attraction: when the mouse comes within ~140 px of a Download button in the hero (or the header
   Download while the hero shows), the nearest three bees (two on phones) glide over and hover on perches
   round that button, ~20 px clear of its box so they frame it and never cover it; the others keep their
   roles. When the cursor leaves they drift back. A tap on Download (touch) gives a short burst of the same.
   Layering: the hero text (headline, sub-line, buttons) sits above the bee overlay (home.css), so a bee
   weaving along the headline passes behind the letters; the bees still fly in front of the comb.

   Hero only, driven by the story timeline (HIVE.story.T, from home.js): full in the hero (T < 0.45),
   flying out to the right and fading as the first story beat comes in (gone by T = 0.8). Scrolling back
   up brings them back. While they are gone the loop stops, so later beats pay nothing for them.

   Degrades: GLB fails -> flat Buzz sprites on the same overlay; no WebGL -> five still Buzz images
   (fading with the same timeline); reduced motion -> five still bees, no flying, no chase, no clock. */
(function () {
  'use strict';
  var doc = document, win = window, HIVE = win.HIVE || (win.HIVE = {});
  if (!doc.body || !doc.body.classList.contains('home')) return;
  var THREE = win.THREE;
  var reduced = !!HIVE.reduced || !!(win.matchMedia && win.matchMedia('(prefers-reduced-motion: reduce)').matches);
  var ROLES = ['letter', 'letter', 'comb', 'comb', 'wander'];
  var COUNT = ROLES.length;

  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
  function lerp(a, b, t) { return a + (b - a) * t; }
  function rnd(a, b) { return a + Math.random() * (b - a); }
  function hyp(x, y) { return Math.sqrt(x * x + y * y); }
  function damp(k, dt) { return 1 - Math.exp(-k * dt); }
  function smooth(a, b, v) { var t = clamp((v - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); }
  function warn(e) { if (win.console) console.warn('[hive bees]', e); }
  function scrollTop() { return win.scrollY || win.pageYOffset || 0; }

  /* ---------------- hero only: how present the bees are at story time T ---------------- */
  function storyT() {
    var s = HIVE.story;
    if (s && typeof s.T === 'number') return s.T;
    return scrollTop() / Math.max(1, win.innerHeight); // no timeline: one screen of scroll
  }
  function outOf(T) { return smooth(0.45, 0.8, T); }      // 0 in the hero, 1 once beat 1 is in: they fly off right
  function alphaOf(T) { return 1 - smooth(0.55, 0.8, T); } // and fade out on the way

  /* ---------------- sizes and the fly zone (viewport px) ---------------- */
  var W = 1, H = 1, LEN = 108, BH = 76;
  var MLEN = 0.885, MHT = 0.62; // model length / height in model units (measured when the GLB loads)
  var Z = { l: 0, r: 1, t: 0, b: 1, side: true };
  function measure() {
    W = win.innerWidth || doc.documentElement.clientWidth || 1;
    H = win.innerHeight || doc.documentElement.clientHeight || 1;
    // ~130 px long at 1920 x 1080, ~96 at 1280 x 800, ~65 at 375 wide
    LEN = W < 760 ? clamp(W * 0.175, 54, 78) : Math.min(clamp(H * 0.12, 76, 134), W * 0.144);
    BH = LEN * MHT / MLEN;
  }
  // the hero text (headline, line, buttons) is measured in page coordinates: the hero starts at the top of
  // the page, so at scroll 0 these are viewport coordinates, and as the page scrolls the text only moves up,
  // away from the zone
  function zone() {
    var sy = scrollTop(), right = 0, bottom = 0, top = 68;
    var hdr = doc.querySelector('.site-header');
    if (hdr) top = Math.max(0, hdr.getBoundingClientRect().bottom + Math.min(sy, 0));
    // the ink, not the boxes: the headline lines and the buttons are measured one by one (their blocks span the page)
    doc.querySelectorAll('.hero-title .ln > span, .hero-sub, .hero-cta .btn').forEach(function (el) {
      var r = el.getBoundingClientRect();
      if (!r.width) return;
      right = Math.max(right, r.right); bottom = Math.max(bottom, r.bottom + sy);
    });
    var l = right + LEN * 0.75, rr = W - LEN * 0.65;
    if (rr - l > LEN * 3.2) Z = { l: l, r: rr, t: top + BH * 1.0, b: H - BH * 0.9, side: true };
    else { // narrow: the band under the buttons, the comb's lower part
      var t = Math.max(top + BH, bottom + BH * 0.95), b = H - BH * 0.75;
      if (b - t < BH * 1.8) t = b - BH * 1.8;
      Z = { l: LEN * 0.65, r: W - LEN * 0.65, t: t, b: b, side: false };
    }
    TX.top = top;
    layout();
  }
  function zx(f) { return lerp(Z.l, Z.r, f); }
  function zy(f) { return lerp(Z.t, Z.b, f); }
  function inZone(x, y, m) { return x > Z.l + m && x < Z.r - m && y > Z.t + m && y < Z.b - m; }
  // home spots in the zone (shares of the zone, and the way the bee faces when still): the two comb bees,
  // then the wanderer's resting spot for the still versions
  var SPOTS_SIDE = [[0.2, 0.2, -1], [0.8, 0.62, -1], [0.42, 0.88, 1]];
  var SPOTS_BAND = [[0.12, 0.28, 1], [0.88, 0.74, -1], [0.5, 0.6, 1]];
  function spot(b) { var s = Z.side ? SPOTS_SIDE : SPOTS_BAND; return s[b.role === 'comb' ? b.ci : 2]; }

  /* ---------------- the headline's ink, in page px (re-measured as the hero settles) ----------------
     words: one box per word (DOM ranges); lines: the words grouped by their visual line (a heading line may
     wrap on phones); glyph boxes: the band the letters really cover inside each line (the line box also holds
     the font's leading); avoid: the sub-line and the buttons, where nothing ever rests. ring: resting spots just
     outside the headline (beside the lines' ends, over the top line's words) that fit on screen clear of the
     text, where the still versions put the two letter bees. */
  var TX = { top: 68, fs: 60, words: [], lines: [], avoid: [], ring: [], at: -9 };
  var PATH = { pts: [], len: 0, key: '' }; // the letter bees' weave (see weavePath)
  // the floor (page px) for the letter bees and the wanderer: their centre stays this high, so the whole bee
  // keeps 12 px above the sub-line (only the bees called to Download go lower)
  function floorY() { var s = TX.avoid[0]; return s ? s.t - 12 - BH * 0.62 : 1e9; }
  function rectOf(r, sy) { return { l: r.left, t: r.top + sy, r: r.right, b: r.bottom + sy }; }
  function layout() {
    var sy = scrollTop(), title = doc.querySelector('.hero-title'), words = [], lines = [], avoid = [];
    if (title) {
      TX.fs = parseFloat(win.getComputedStyle(title).fontSize) || TX.fs;
      var walk = doc.createTreeWalker(title, NodeFilter.SHOW_TEXT, null), node, rg = doc.createRange(), m, re = /\S+/g;
      while ((node = walk.nextNode())) {
        re.lastIndex = 0;
        while ((m = re.exec(node.nodeValue))) {
          rg.setStart(node, m.index); rg.setEnd(node, m.index + m[0].length);
          var r = rg.getBoundingClientRect();
          if (r.width > 1) words.push(rectOf(r, sy));
        }
      }
    }
    words.forEach(function (w) {
      var cy = (w.t + w.b) / 2, ln = null;
      for (var k = 0; k < lines.length; k++) if (Math.abs(lines[k].cy - cy) < TX.fs * 0.5) { ln = lines[k]; break; }
      if (!ln) { ln = { l: w.l, r: w.r, cy: cy, words: [] }; lines.push(ln); }
      ln.l = Math.min(ln.l, w.l); ln.r = Math.max(ln.r, w.r); ln.words.push(w);
      w.cy = cy;
    });
    lines.sort(function (a, b) { return a.cy - b.cy; });
    lines.forEach(function (ln) { ln.t = ln.cy - TX.fs * 0.42; ln.b = ln.cy + TX.fs * 0.4; }); // the glyph band
    doc.querySelectorAll('.hero-sub, .hero-cta .btn').forEach(function (el) {
      var r = el.getBoundingClientRect();
      if (r.width) avoid.push(rectOf(r, sy));
    });
    TX.words = words; TX.lines = lines; TX.avoid = avoid;
    ringOf();
    weavePath();
  }
  function hits(a, x, y, hw, hh) { // overlap area of a box (centre x, y, half sizes) with rect a
    var ox = Math.min(a.r, x + hw) - Math.max(a.l, x - hw), oy = Math.min(a.b, y + hh) - Math.max(a.t, y - hh);
    return ox > 0 && oy > 0 ? ox * oy : 0;
  }
  // may a bee rest here (page px)? on screen, under the header, off the sub-line and the buttons, and at
  // most a sliver of it over the letters
  function restOK(x, y, glyphShare) {
    var hw = LEN * 0.42, hh = BH * 0.42, k, ink = 0;
    if (x < LEN * 0.55 || x > W - LEN * 0.55 || y - BH * 0.55 < TX.top || y + BH * 0.5 > H) return false;
    for (k = 0; k < TX.avoid.length; k++) if (hits(TX.avoid[k], x, y, hw + 4, hh + 4) > 0) return false;
    for (k = 0; k < TX.lines.length; k++) ink += hits(TX.lines[k], x, y, hw, hh);
    return ink <= glyphShare * 4 * hw * hh;
  }
  function ringOf() {
    var L = TX.lines, A = [], gx = LEN * 0.62, i;
    if (L.length) {
      for (i = L.length - 1; i >= 0; i--) A.push({ x: L[i].l - gx, y: L[i].cy, face: 1 });
      L[0].words.forEach(function (w) { A.push({ x: (w.l + w.r) / 2, y: L[0].t - BH * 0.5, face: 0, w: w }); });
      for (i = 0; i < L.length; i++) A.push({ x: L[i].r + gx, y: L[i].cy, face: -1 });
    }
    var ring = A.filter(function (a) { return restOK(a.x, a.y, 0.12); });
    if (ring.length < 2 && L.length) { // tight screens: a few spots spread over the top line
      ring = [0.15, 0.5, 0.85].map(function (f) { return { x: clamp(lerp(L[0].l, L[0].r, f), LEN * 0.6, W - LEN * 0.6), y: L[0].t - BH * 0.5, face: 0 }; });
    }
    TX.ring = ring;
  }

  /* ---------------- the still versions: where each of the five rests (viewport px, at this scroll) ---------------- */
  function stillSpots() {
    var R = TX.ring, sy = scrollTop(), ci = 0;
    return ROLES.map(function (role, i) {
      if (role === 'letter' && R.length) {
        var a = R[i === 0 ? Math.floor(R.length * 0.4) : R.length - 1];
        return [a.x, a.y - sy, a.face || (i === 0 ? 1 : -1)];
      }
      if (role === 'wander' && !Z.side && R.length > 2) return [R[0].x, R[0].y - sy, 1]; // phones: by the headline, not under the buttons
      var s = (Z.side ? SPOTS_SIDE : SPOTS_BAND)[role === 'comb' ? ci++ : 2];
      return [zx(s[0]), zy(s[1]), s[2]];
    });
  }

  /* ---------------- no WebGL: five still Buzz images ---------------- */
  var imgs = null;
  function stillImages() {
    if (imgs) return;
    var box = doc.createElement('div');
    box.className = 'bees-still';
    box.setAttribute('aria-hidden', 'true');
    imgs = { box: box, list: [] };
    var names = ['c1-happy', 'c1', 'c1', 'c1-think', 'c1-happy'];
    for (var i = 0; i < COUNT; i++) {
      var im = doc.createElement('img');
      im.src = 'assets/bee-v2/' + names[i] + '.svg'; im.alt = ''; im.decoding = 'async';
      box.appendChild(im); imgs.list.push(im);
    }
    doc.body.appendChild(box);
    placeImages();
    if (HIVE.story && HIVE.story.subs) HIVE.story.subs.push(fadeImages);
    else win.addEventListener('scroll', function () { fadeImages(storyT()); }, { passive: true });
    fadeImages(storyT());
    var it = 0;
    win.addEventListener('resize', function () { clearTimeout(it); it = setTimeout(placeImages, 150); });
    win.addEventListener('load', placeImages);
    if (doc.fonts && doc.fonts.ready) doc.fonts.ready.then(placeImages);
    setTimeout(placeImages, 1400); // the headline's line reveal has settled
    HIVE.bees = { mode: function () { return 'images'; }, zone: function () { return Z; },
      visible: function () { return imgs.box.style.visibility === 'hidden' ? 0 : imgs.list.length; } };
  }
  function placeImages() {
    if (!imgs) return;
    measure(); zone();
    var sp = stillSpots(), size = LEN * 1.15;
    imgs.list.forEach(function (im, i) {
      var s = im.style;
      s.width = s.height = size.toFixed(1) + 'px';
      s.left = sp[i][0].toFixed(1) + 'px'; s.top = sp[i][1].toFixed(1) + 'px';
      s.transform = 'translate(-47%, -56%)' + (sp[i][2] > 0 ? '' : ' scaleX(-1)'); // the sprites face right; mirror to face left
    });
  }
  function fadeImages(T) {
    if (!imgs) return;
    var a = alphaOf(T);
    imgs.box.style.opacity = a.toFixed(3);
    imgs.box.style.visibility = a < 0.01 ? 'hidden' : '';
  }

  if (!THREE || !THREE.WebGLRenderer) { stillImages(); return; }

  /* ---------------- overlay, renderer, camera (1 css px = 1 unit, y down = -y) ---------------- */
  var canvas = doc.createElement('canvas');
  canvas.className = 'bees-gl';
  canvas.setAttribute('aria-hidden', 'true');
  doc.body.appendChild(canvas);
  var renderer = null;
  try { renderer = new THREE.WebGLRenderer({ canvas: canvas, alpha: true, antialias: true }); } catch (e) { renderer = null; }
  if (!renderer || !renderer.getContext()) { canvas.parentNode.removeChild(canvas); stillImages(); return; }
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
  function sizes() {
    measure();
    renderer.setSize(W, H, false);
    cam.left = 0; cam.right = W; cam.top = 0; cam.bottom = -H;
    cam.updateProjectionMatrix();
    zone();
  }
  sizes();

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
  var bees = [], ST = { x: 0, y: 0, z: 1, k: 1, face: 1, pitch: 0, roll: 0, carry: false, jet: 0, t: 0, mood: 'neutral', blink: 0, headRoll: 0, headNod: 0, snap: false };
  var visit = { next: 0 }; // when the next cell visit may start (one bee at a time)
  var CP = { x: 0, y: 0 };

  function newBee(look, i) {
    var role = ROLES[i];
    return { look: look, i: i, role: role, ci: ROLES.slice(0, i).filter(function (r) { return r === 'comb'; }).length,
      x: 0, y: 0, vx: 0, vy: 0, ax: 0, ay: 0, tx: 0, ty: 0, maxV: 150, maxA: 900, resp: 4,
      hx: 0, hy: 0, moveAt: 0, task: 'hover', cell: -1, phase: '', until: 0, flashed: false, hold: false,
      u: 0, sw: false, perch: -1, pdir: 1, below: false, away: true,
      face: -1, faceT: -1, pitch: 0, roll: 0, bob: 0, ry: 0, jet: 0.5, ph: Math.random() * 6.283, ph2: Math.random() * 6.283,
      mood: 'neutral', moodUntil: 0, moodT: 'neutral', blinkAt: rnd(1, 4), blinkT: -9, headRoll: 0, glanceAt: rnd(3, 7), shown: false };
  }
  function speedScale() { return Math.sqrt(LEN / 150); }
  function setMood(b, m, dur, t) { b.moodT = m; b.moodUntil = t + dur; }
  function pace(b, v, a, resp) { var s = speedScale(); b.maxV = v * s; b.maxA = a * s; b.resp = resp; }

  /* ---- comb bees: a home spot in the zone, moved now and then, clear of the other bees ---- */
  function home(b) { var s = spot(b); b.hx = zx(s[0]); b.hy = zy(s[1]); }
  function relocate(b, t) {
    var s = spot(b);
    for (var k = 0; k < 8; k++) {
      var x = zx(clamp(s[0] + rnd(-0.14, 0.14), 0.04, 0.96)), y = zy(clamp(s[1] + rnd(-0.18, 0.18), 0.04, 0.96)), ok = true;
      for (var j = 0; j < bees.length; j++) if (bees[j] !== b && hyp(bees[j].hx - x, bees[j].hy - y) < LEN * 1.5) { ok = false; break; }
      if (ok) { b.hx = x; b.hy = y; break; }
    }
    b.moveAt = t + rnd(7, 12);
  }

  /* ---- the comb (HIVE.comb from home.js): a cell inside the zone a bee can settle on ---- */
  function comb() { var c = HIVE.comb; return c && c.live && c.live() ? c : null; }
  function pickCell(b) {
    var c = comb();
    if (!c) return -1;
    var r = c.rect(), best = -1, bd = 1e9;
    for (var k = 0; k < 40; k++) {
      var i = Math.floor(Math.random() * c.count);
      if (!c.free(i)) continue;
      c.screen(i, CP, r);
      if (!inZone(CP.x, CP.y - BH * 0.4, LEN * 0.4)) continue;
      var d = hyp(CP.x - b.x, CP.y - b.y);
      if (d > LEN * 1.2 && d < bd) { bd = d; best = i; }
    }
    return best;
  }
  function endVisit(b, t) {
    var c = HIVE.comb;
    if (b.cell >= 0 && c && c.busy) c.busy(b.cell, false);
    b.task = 'hover'; b.cell = -1; b.phase = ''; b.hold = false;
    visit.next = t + rnd(5, 9);
    relocate(b, t);
  }
  function thinkComb(b, t, out) {
    var s = speedScale();
    if (b.task === 'visit') {
      var c = comb();
      if (!c || out > 0.02 || t > b.until) endVisit(b, t);
      else {
        c.screen(b.cell, CP, c.rect());
        b.tx = CP.x; b.ty = CP.y - BH * 0.42; // standing on the cell's top face
        var d = hyp(b.tx - b.x, b.ty - b.y);
        if (b.phase === 'go' && d < LEN * 0.2 && hyp(b.vx, b.vy) < 90 * s) { b.phase = 'land'; b.until = t + rnd(3.2, 4.5); b.hold = true; }
        if (b.phase === 'land' && !b.flashed) { b.flashed = true; c.flash(b.cell, 1); setMood(b, 'happy', 2.2, t); }
        if (b.phase === 'land' && t > b.until - 0.3) endVisit(b, t);
        pace(b, b.phase === 'go' ? 120 : 50, 420, 3);
        if (b.task === 'visit') return;
      }
    }
    // hovering: a slow figure around the home spot, the buzz
    if (t > b.moveAt) relocate(b, t);
    b.tx = b.hx + Math.sin(t * 0.32 + b.ph) * LEN * 0.4 + Math.sin(t * 0.75 + b.ph2) * LEN * 0.08;
    b.ty = b.hy + Math.sin(t * 0.5 + b.ph2) * BH * 0.3;
    b.maxV = 70 * s; b.maxA = 420 * s; b.resp = 3;
    // now and then a look around
    if (t > b.glanceAt) { setMood(b, Math.random() < 0.5 ? 'curious' : 'think', rnd(1.4, 2.6), t); b.glanceAt = t + rnd(6, 11); }
    // one visit at a time, only while the bees are fully in the hero
    if (out < 0.01 && t > visit.next && !bees.some(function (o) { return o.task === 'visit'; }) && Math.random() < 0.02) {
      var i = pickCell(b);
      if (i >= 0) { b.task = 'visit'; b.cell = i; b.phase = 'go'; b.flashed = false; b.until = t + 10; HIVE.comb.busy(i, true); }
      else visit.next = t + 2;
    }
  }

  /* ---- letter bees: a slow weave along the headline ----
     The path runs along each line in turn (the first left to right, the next right to left, and so on, then
     back to the start): it passes over one word and under the next, crossing the line in the gap between
     words, so the bee threads through the text like a needle (behind the letters: the hero text is layered
     above the bees). Nothing goes below floorY(), 12 px over the sub-line. It is a closed Catmull-Rom curve through those points, sampled with its
     arc length; each bee glides along it at a calm, constant pace and never stops on a word. */
  function weavePath() {
    var L = TX.lines, C = [], fs = TX.fs, gx = LEN * 0.55, A = fs * 0.41 + BH * 0.4, i, k;
    var xl = LEN * 0.5, xr = W - LEN * 0.5, ytop = TX.top + BH * 0.5, fl = floorY();
    // a turn beside a line's end; on a narrow screen there is no room beside it, so the turn goes over the
    // line (or through the gap to the next line) instead of on the letters
    function end(ln, x, over) {
      var cx = clamp(x, xl, xr);
      return cx === x ? [x, Math.min(ln.cy, fl)] : [cx, Math.min(fl, Math.max(ytop, ln.cy + (over ? -A : A)))];
    }
    for (i = 0; i < L.length; i++) {
      var ln = L[i], fwd = i % 2 === 0, ws = ln.words.slice().sort(function (a, b) { return a.l - b.l; });
      if (!fwd) ws.reverse();
      var last = i === L.length - 1;
      C.push(end(ln, fwd ? ln.l - gx : ln.r + gx, i === 0 || last));
      for (k = 0; k < ws.length; k++) {
        var under = (k + i) % 2 === 1 && !last;
        C.push([clamp((ws[k].l + ws[k].r) / 2, xl, xr), Math.min(fl, Math.max(ytop, ln.cy + (under ? A : -A)))]);
      }
      C.push(end(ln, fwd ? ln.r + gx : ln.l - gx, i === 0 || last));
    }
    var key = C.map(function (c) { return Math.round(c[0]) + ',' + Math.round(c[1]); }).join(' ');
    if (key === PATH.key) return;
    PATH.key = key; PATH.pts = []; PATH.len = 0;
    if (C.length < 3) return;
    var n = C.length, pts = [], d = 0, px = 0, py = 0;
    for (i = 0; i < n; i++) { // closed Catmull-Rom, 16 samples per span
      var p0 = C[(i - 1 + n) % n], p1 = C[i], p2 = C[(i + 1) % n], p3 = C[(i + 2) % n];
      for (k = 0; k < 16; k++) {
        var u = k / 16, u2 = u * u, u3 = u2 * u;
        var x = 0.5 * (2 * p1[0] + (-p0[0] + p2[0]) * u + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * u2 + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * u3);
        var y = 0.5 * (2 * p1[1] + (-p0[1] + p2[1]) * u + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * u2 + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * u3);
        y = Math.min(y, fl); // (the curve may bulge between points: never below the floor)
        if (pts.length) d += hyp(x - px, y - py);
        pts.push([x, y, d]); px = x; py = y;
      }
    }
    PATH.len = d + hyp(pts[0][0] - px, pts[0][1] - py);
    PATH.pts = pts;
  }
  function pathAt(f, out) { // the point at share f (0..1) of the path's length, page px
    var P = PATH.pts, s = ((f % 1) + 1) % 1 * PATH.len, lo = 0, hi = P.length - 1;
    while (lo < hi) { var m = (lo + hi + 1) >> 1; if (P[m][2] <= s) lo = m; else hi = m - 1; }
    var a = P[lo], b = P[(lo + 1) % P.length], seg = (lo + 1 < P.length ? b[2] : PATH.len) - a[2];
    var u = seg > 0 ? (s - a[2]) / seg : 0;
    out.x = lerp(a[0], b[0], u); out.y = lerp(a[1], b[1], u);
    return out;
  }
  function nearestOnPath(b, sy) {
    var P = PATH.pts, best = 0, bd = 1e9;
    for (var k = 0; k < P.length; k++) { var d = hyp(P[k][0] - b.x, P[k][1] - sy - b.y); if (d < bd) { bd = d; best = k; } }
    return P.length ? P[best][2] / PATH.len : 0;
  }
  function thinkLetter(b, t, sy, dt) {
    if (!PATH.pts.length) { thinkWander(b, t); return; }
    // glide on only while the bee keeps up with its point (after a swarm or the landing it catches up first)
    pathAt(b.u, CP);
    if (hyp(CP.x - b.x, CP.y - sy - b.y) < LEN * 0.9) b.u += 80 * speedScale() * dt / Math.max(1, PATH.len);
    b.tx = CP.x; b.ty = CP.y - sy;
    pace(b, 230, 520, 3);
    if (t > b.glanceAt) { setMood(b, Math.random() < 0.6 ? 'curious' : 'happy', rnd(1, 2), t); b.glanceAt = t + rnd(5, 9); }
  }

  /* ---- the wanderer: anywhere in the hero, resting only where it hides no text and no button ---- */
  // where the wanderer may be (page px): over the comb (right of the text) or round the headline, never
  // elsewhere in the text column (the sub-line, the buttons, below them)
  function wanderOK(x, y) {
    if (y > floorY()) return false;
    if (Z.side && x > Z.l) return true;
    var L = TX.lines;
    if (!L.length) return false;
    var l = Math.min.apply(null, L.map(function (n) { return n.l; })), r = Math.max.apply(null, L.map(function (n) { return n.r; }));
    return x > l - LEN * 0.6 && x < r + LEN * 0.6 && y > L[0].t - BH * 1.1;
  }
  function pickWander(b, t) {
    var sy = scrollTop(), best = null, bs = -1;
    for (var k = 0; k < 24; k++) {
      var x = rnd(LEN * 0.6, W - LEN * 0.6), y = rnd(TX.top + BH * 0.9, Math.min(H - BH * 0.8, floorY() - sy - BH * 0.25));
      if (!restOK(x, y + sy, 0) || !wanderOK(x, y + sy)) continue;
      var sc = Math.min(hyp(x - b.x, y - b.y), W * 0.28); // roam: prefer a fair way from where it is
      for (var j = 0; j < bees.length; j++) if (bees[j] !== b && hyp(bees[j].x - x, bees[j].y - y) < LEN * 1.4) sc -= W;
      if (sc > bs) { bs = sc; best = [x, y]; }
    }
    if (best) { b.hx = best[0]; b.hy = best[1]; }
    else if (TX.lines.length) { var L0 = TX.lines[0]; b.hx = clamp((L0.l + L0.r) / 2, LEN, W - LEN); b.hy = Math.max(TX.top + BH * 0.6, L0.t - BH * 0.5) - sy; } // over the headline
    b.moveAt = t + rnd(5, 9);
  }
  function thinkWander(b, t) {
    if (t > b.moveAt) pickWander(b, t);
    b.tx = b.hx + Math.sin(t * 0.5 + b.ph) * LEN * 0.2; b.ty = b.hy + Math.sin(t * 0.75 + b.ph2) * BH * 0.16;
    pace(b, 100, 360, 3);
    if (t > b.glanceAt) { setMood(b, Math.random() < 0.5 ? 'curious' : 'think', rnd(1, 2), t); b.glanceAt = t + rnd(5, 9); }
  }

  /* ---- the Download welcome: the nearest few bees come and hover round the button ---- */
  var ptr = { x: -1e4, y: -1e4, on: false };
  var burst = { until: -1, el: null };
  var sw = { on: false, el: null, r: null, t0: 0 };
  var NEAR = 140, LEAVE = 175;
  function dlEls() {
    var out = [];
    doc.querySelectorAll('.hero-cta a[href$="download.html"], .site-header .header-cta').forEach(function (el) {
      var r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0 && win.getComputedStyle(el).display !== 'none') out.push(el);
    });
    return out;
  }
  function dlNearest(x, y) { // the Download button nearest a point, and how far it is from the button's box
    var best = { el: null, d: 1e9 };
    dlEls().forEach(function (el) {
      var r = el.getBoundingClientRect();
      var dx = Math.max(r.left - x, 0, x - r.right), dy = Math.max(r.top - y, 0, y - r.bottom), d = hyp(dx, dy);
      if (d < best.d) best = { el: el, d: d };
    });
    return best;
  }
  // perches round the button, on its box grown by a clear margin plus half a bee, so a bee on a perch, or
  // moving along an edge to the next one, never touches the button. Perches that would put a bee over the
  // sub-line or the other button, or off screen, are not used.
  var PERCH = [[-1, 0], [-1, -1], [1, -1], [1, 0], [1, 1], [0.5, 1], [-0.5, 1], [-1, 1]]; // ring order; never straight over it
  var CORNERS = [1, 2, 4, 7];
  function perchAt(k, out) {
    var r = sw.r, bw = LEN * 0.56, bh = BH * 0.66, m = 24;
    out.x = (r.left + r.right) / 2 + PERCH[k][0] * (r.width / 2 + m + bw);
    out.y = (r.top + r.bottom) / 2 + PERCH[k][1] * (r.height / 2 + m + bh);
    out.ok = out.x > bw * 0.9 && out.x < W - bw * 0.9 && out.y > TX.top + bh && out.y < H - bh * 0.8;
    var sy = scrollTop(), hw = LEN * 0.45 + 4, hh = BH * 0.5 + 4;
    for (var k2 = 0; out.ok && k2 < TX.avoid.length; k2++) {
      var a = TX.avoid[k2];
      if (out.x + hw > a.l && out.x - hw < a.r && out.y + hh > a.t - sy && out.y - hh < a.b - sy) out.ok = false;
    }
    return out;
  }
  var PP = { x: 0, y: 0, ok: false };
  function perchFree(k, b) {
    if (!perchAt(k, PP).ok) return false;
    var px = PP.x, py = PP.y;
    for (var j = 0; j < bees.length; j++) {
      var o = bees[j];
      if (o === b || !o.sw || o.perch < 0) continue;
      if (o.perch === k) return false;
      perchAt(o.perch, CP);
      if (hyp(CP.x - px, CP.y - py) < LEN * 0.85) return false; // too close to another helper
    }
    perchAt(k, PP);
    return true;
  }
  function crossesButton(x0, y0, x1, y1) { // would a bee flying this straight line touch the button?
    var r = sw.r, hw = LEN * 0.45 + 8, hh = BH * 0.5 + 8;
    for (var s = 0; s <= 10; s++) {
      var x = lerp(x0, x1, s / 10), y = lerp(y0, y1, s / 10);
      if (x + hw > r.left && x - hw < r.right && y + hh > r.top && y - hh < r.bottom) return true;
    }
    return false;
  }
  function swarmCheck(t, out) {
    var want = false, el = null;
    if (out < 0.3) {
      if (t < burst.until && burst.el) { want = true; el = burst.el; }
      else if (ptr.on) { var nb = dlNearest(ptr.x, ptr.y); el = nb.el; want = !!el && nb.d <= (sw.on ? LEAVE : NEAR); }
    }
    if (want) { sw.el = el; sw.r = el.getBoundingClientRect(); }
    if (want && !sw.on) {
      // only the nearest two or three come over; the rest keep to their roles
      sw.on = true; sw.t0 = t;
      var cx = (sw.r.left + sw.r.right) / 2, cy = (sw.r.top + sw.r.bottom) / 2;
      var near = bees.slice().sort(function (a, b) { return hyp(a.x - cx, a.y - cy) - hyp(b.x - cx, b.y - cy); }).slice(0, W < 760 ? 2 : 3);
      near.forEach(function (b) { b.sw = true; b.perch = -1; });
      near.forEach(function (b) { // each to the free perch nearest it, so they come in from their own sides
        var best = -1, bd = 1e9;
        for (var k = 0; k < PERCH.length; k++) {
          if (!perchFree(k, b)) continue;
          var d = hyp(PP.x - b.x, PP.y - b.y);
          if (d < bd) { bd = d; best = k; }
        }
        if (best < 0) { b.sw = false; return; } // no room left round the button: it keeps to its role
        if (b.task === 'visit') endVisit(b, t);
        b.perch = best; b.pdir = Math.random() < 0.5 ? 1 : -1; b.until = t + rnd(2.5, 4.5);
        b.hold = false; b.phase = '';
        setMood(b, 'happy', 1.2, t);
      });
    } else if (!want && sw.on) {
      sw.on = false;
      var sy = scrollTop();
      bees.forEach(function (b) { // the helpers drift back to work
        if (!b.sw) return;
        b.sw = false; b.below = true;
        setMood(b, 'curious', 0.9, t);
        if (b.role === 'letter') b.u = nearestOnPath(b, sy);
        else if (b.role === 'comb') { b.task = 'hover'; relocate(b, t); }
        else pickWander(b, t);
      });
    }
  }
  // a calm, friendly hover round the button: the approach eases in (soft acceleration) and out (the arrive
  // radius in steer); now and then a helper drifts along the box's edge to the next free perch
  function thinkSwarm(b, t) {
    if (b.perch < 0 || !perchFree(b.perch, b)) {
      b.perch = -1;
      for (var k = 0; k < PERCH.length; k++) if (perchFree(k, b)) { b.perch = k; break; }
      if (b.perch < 0) { b.tx = b.x; b.ty = b.y; return; }
    }
    if (t > b.until) {
      var nx = (b.perch + b.pdir + PERCH.length) % PERCH.length;
      // drift only to a perch on the same side, so it never turns its back on the button
      if (Math.sign(PERCH[nx][0]) === Math.sign(PERCH[b.perch][0]) && perchFree(nx, b)) b.perch = nx; else b.pdir = -b.pdir;
      b.until = t + rnd(2.5, 4.5);
    }
    perchAt(b.perch, PP);
    var gx = PP.x, gy = PP.y;
    if (crossesButton(b.x, b.y, gx, gy)) { // coming in from the far side: round the nearest corner, not across the button
      var best = -1, bd = 1e9;
      for (var ci = 0; ci < CORNERS.length; ci++) {
        var c = CORNERS[ci];
        perchAt(c, CP);
        var d = hyp(CP.x - b.x, CP.y - b.y) + hyp(gx - CP.x, gy - CP.y);
        if (d < bd && !crossesButton(b.x, b.y, CP.x, CP.y)) { bd = d; best = c; }
      }
      if (best >= 0) { perchAt(best, CP); gx = CP.x; gy = CP.y; }
    }
    b.tx = gx + Math.sin(t * 0.9 + b.ph) * 5; b.ty = gy + Math.sin(t * 1.3 + b.ph2) * 4;
    pace(b, 560, 1050, 4.5);
    if (t > b.moodUntil) setMood(b, 'happy', 1.5, t);
  }

  /* ---- one bee's step: where it wants to be ---- */
  function think(b, t, out, sy, dt) {
    if (b.sw) { thinkSwarm(b, t); return; }
    if (b.role === 'letter') thinkLetter(b, t, sy, dt);
    else if (b.role === 'comb') thinkComb(b, t, out);
    else thinkWander(b, t);
  }

  /* ---- steering: arrive + separation, acceleration-limited ---- */
  function steer(b, dt, out) {
    // flying out: off the right edge, a little up
    var tx = lerp(b.tx, W + LEN * (2.5 + b.i * 0.4), out), ty = b.ty - out * H * 0.06;
    var dx = tx - b.x, dy = ty - b.y, d = hyp(dx, dy), slowR = LEN * (b.sw ? 2.6 : 1.2); // (helpers brake early: a glide, no overshoot)
    if (out > 0.5) b.away = true; // flown off: on the way back it may cruise faster, easing down as it nears its spot
    else if (b.away && (d < LEN * 2 || b.sw)) b.away = false;
    var maxV = out > 0.01 ? Math.max(b.maxV, 320 * speedScale()) : b.away ? Math.max(b.maxV, Math.min(420, d * 0.5) * speedScale()) : b.maxV;
    var sp = maxV * Math.min(1, d / slowR);
    var wx = d > 0.01 ? dx / d * sp : 0, wy = d > 0.01 ? dy / d * sp : 0;
    var R = LEN * (b.sw ? 0.45 : 1.05), sx = 0, sy = 0;
    for (var i = 0; i < bees.length; i++) {
      var o = bees[i];
      if (o === b) continue;
      var ox = b.x - o.x, oy = b.y - o.y, od = hyp(ox, oy);
      if (od < R && od > 0.001) { var f = (R - od) / R; sx += ox / od * f; sy += oy / od * f; }
    }
    var sepK = (b.sw ? 10 : b.hold ? 30 : 110) * speedScale();
    wx += sx * sepK; wy += sy * sepK;
    var maxA = Math.max(b.maxA, out > 0.01 ? 1000 * speedScale() : 0);
    var ax = (wx - b.vx) * b.resp, ay = (wy - b.vy) * b.resp, am = hyp(ax, ay);
    if (am > maxA) { ax *= maxA / am; ay *= maxA / am; }
    b.vx += ax * dt; b.vy += ay * dt;
    b.x += b.vx * dt; b.y += b.vy * dt;
    if (b.role !== 'comb' && !b.sw && out < 0.01) { // the floor over the sub-line: a hard limit, once the bee is above it
      var fy = floorY() - scrollTop();
      if (fy < TX.top) b.below = true; // (the page jumped and the story has not caught up yet: no floor this frame)
      else if (b.y <= fy) b.below = false;
      else if (!b.below) { b.y = fy; if (b.vy > 0) b.vy = 0; }
    }
    var k = damp(7, dt);
    b.ax += (ax - b.ax) * k; b.ay += (ay - b.ay) * k;
  }

  /* ---- facing, pitch, bank, bob, jets, blink ---- */
  function orient(b, t, dt) {
    var v = hyp(b.vx, b.vy), s = speedScale();
    var look = b.sw && sw.r && hyp(b.tx - b.x, b.ty - b.y) < LEN * 1.2; // round the Download button: eyes on it
    var lx = look ? (sw.r.left + sw.r.right) / 2 - b.x : 0, ly = look ? (sw.r.top + sw.r.bottom) / 2 - b.ry : 0;
    if (look) b.faceT = lx > 0 ? 1 : -1;
    else if (b.hold) b.faceT = b.x > (Z.l + Z.r) / 2 ? -1 : 1; // settled on a cell: turned to the comb's middle
    else if (Math.abs(b.vx) > 26 * s) b.faceT = b.vx > 0 ? 1 : -1;
    b.face += (b.faceT - b.face) * damp(look ? 6 : 4, dt);
    var pt = look ? clamp(Math.atan2(-ly, Math.abs(lx)), -0.8, 0.8) // nose tipped toward the button
      : v > 30 * s && !b.hold ? clamp(Math.atan2(-b.vy, Math.abs(b.vx) + 60 * s), -0.75, 0.75) * 0.55 : (b.hold ? 0.08 : 0);
    b.pitch += (pt - b.pitch) * damp(4, dt);
    var lat = v > 1 ? (b.vx * b.ay - b.vy * b.ax) / v : 0;
    b.roll += (clamp(lat / (1600 * s), -0.35, 0.35) - b.roll) * damp(3.5, dt);
    var amp = (b.hold ? 0.8 : 3.6 * (1 - 0.5 * clamp(v / (200 * s), 0, 1))) * s;
    b.bob += (amp - b.bob) * damp(4, dt);
    b.ry = b.y + Math.sin(t * 1.7 + b.ph) * b.bob + Math.sin(t * 19 + b.ph2) * 0.22 * s; // the slow bob and the buzz
    b.jet += (clamp(v / (260 * s) + hyp(b.ax, b.ay) / (1500 * s), b.hold ? 0.08 : 0.28, 1) - b.jet) * damp(6, dt);
    b.headRoll += ((b.hold ? 0 : Math.sin(t * 0.9 + b.ph) * 0.06) - b.headRoll) * damp(3, dt);
    if (t > b.blinkAt) { b.blinkT = t; b.blinkAt = t + (Math.random() < 0.2 ? 0.28 : rnd(2.2, 6)); }
  }
  function blinkOf(b, t) { var a = (t - b.blinkT) / 0.15; return a < 0 || a > 1 ? 0 : 1 - Math.abs(a * 2 - 1); }

  /* ---------------- frame ---------------- */
  var raf = 0, lastT = -1, built = false, dead = false, drawnEmpty = false, alpha = 1, mode = 'loading';
  function onScreen(b) { return b.x > -LEN && b.x < W + LEN && b.ry > -BH && b.ry < H + BH; }
  function fade(T) {
    alpha = alphaOf(T);
    canvas.style.opacity = alpha.toFixed(3);
    canvas.style.visibility = alpha < 0.01 ? 'hidden' : '';
  }
  function wanted() { return built && !dead && !doc.hidden && alpha >= 0.01; }
  function step(t, dt) {
    var out = outOf(storyT()), sy = scrollTop();
    if (t > TX.at || t < TX.at - 5) { layout(); TX.at = t + 0.6; } // the headline settles (line reveal, fonts): re-measure
    swarmCheck(t, out);
    for (var i = 0; i < bees.length; i++) { think(bees[i], t, out, sy, dt); steer(bees[i], dt, out); orient(bees[i], t, dt); }
  }
  function frame(ts) {
    raf = 0;
    if (!wanted()) { lastT = -1; return; } // gone (past the hero, or the tab is hidden): the loop stops
    var t = ts / 1000, dt = lastT < 0 ? 0.016 : clamp(t - lastT, 0, 0.05);
    lastT = t;
    try { step(t, dt); draw(t, false); } catch (e) { warn(e); }
    schedule();
  }
  function draw(t, snap) {
    var any = false;
    for (var i = 0; i < bees.length; i++) {
      var b = bees[i], st = ST, vis = onScreen(b);
      b.shown = vis;
      b.look.obj.visible = vis;
      if (!vis) continue;
      any = true;
      st.x = b.x; st.y = b.ry; st.z = 1; st.k = 1;
      st.face = b.face; st.pitch = b.pitch * b.face; st.roll = b.roll * (b.face >= 0 ? 1 : -1);
      st.carry = false; st.jet = b.jet; st.t = t; st.snap = snap;
      st.mood = t < b.moodUntil ? b.moodT : b.mood;
      st.blink = reduced ? 0 : blinkOf(b, t);
      st.headRoll = b.headRoll; st.headNod = 0;
      b.look.set(st);
    }
    if (any || !drawnEmpty) { renderer.render(scene, cam); drawnEmpty = !any; }
  }
  function schedule() { if (!raf && !reduced && wanted()) raf = requestAnimationFrame(frame); }

  /* ---------------- reduced motion: five still bees ---------------- */
  function placeStill() {
    zone();
    var sp = stillSpots();
    bees.forEach(function (b, i) {
      b.x = sp[i][0]; b.y = b.ry = sp[i][1]; b.face = b.faceT = sp[i][2]; b.pitch = b.roll = 0; b.jet = 0.3; b.headRoll = 0;
    });
  }
  var still = 0;
  function drawStill() {
    if (still || dead) return;
    still = requestAnimationFrame(function () { still = 0; if (alpha >= 0.01) draw(performance.now() / 1000, true); });
  }

  /* ---------------- boot ---------------- */
  function onStory(T) {
    var was = alpha >= 0.01;
    fade(T);
    if (!built) return;
    if (reduced) { if (alpha >= 0.01 && !was) drawStill(); return; }
    schedule();
  }
  // back to its role after a resize (or at the landing)
  function settle(b, t) {
    if (b.role === 'letter') b.u = b.i === 0 ? 0 : 0.5; // the two letter bees half a lap apart
    else if (b.role === 'comb') { if (b.task === 'visit') endVisit(b, t); home(b); b.moveAt = t + rnd(5, 9); }
    else { b.hx = b.x; b.hy = b.y; pickWander(b, t); }
  }
  function build(make) {
    sizes();
    for (var i = 0; i < COUNT; i++) {
      var look = make();
      scene.add(look.obj);
      bees.push(newBee(look, i));
    }
    built = true;
    var t = performance.now() / 1000;
    // for checks in the console (advance(s) runs the bees s seconds at 60 fps, e.g. in a hidden tab)
    HIVE.bees = { list: bees, zone: function () { return Z; }, mode: function () { return mode; },
      size: function () { return { LEN: LEN, BH: BH }; }, text: function () { return TX; },
      swarming: function () { return sw.on; },
      bbox: function (i) { // a bee's drawn box in viewport px (model or sprite)
        var bx = new THREE.Box3().setFromObject(bees[i].look.obj);
        return { l: bx.min.x, r: bx.max.x, t: -bx.max.y, b: -bx.min.y };
      }, helpers: function () { return bees.filter(function (b) { return b.sw; }).map(function (b) { return b.i; }); },
      visible: function () { return alpha < 0.01 ? 0 : bees.filter(function (b) { return b.shown; }).length; },
      advance: function (s) {
        var ts = lastT < 0 ? performance.now() : lastT * 1000;
        for (var k = 0; k < s * 60; k++) { ts += 1000 / 60; var tt = ts / 1000; step(tt, 1 / 60); lastT = tt; }
        draw(lastT, false);
      } };
    fade(storyT());
    if (reduced) { placeStill(); drawStill(); return; }
    // the landing: the bees arrive from the right, one after another, each to its role
    bees.forEach(function (b, k) {
      b.x = W + LEN * (0.8 + k * 0.6); b.vx = -160 * speedScale();
      b.y = b.role === 'comb' ? rnd(TX.top + BH, H - BH) : rnd(TX.top + BH * 0.7, Math.max(TX.top + BH * 0.7, floorY() - scrollTop()));
      settle(b, t);
    });
    visit.next = t + rnd(2.5, 4);
    schedule();
  }
  function start() {
    var made = false;
    modelLook.load(function () { if (made) return; made = true; mode = 'model'; build(modelLook.make); },
      function () { if (made) return; spriteLook.load(function () { if (made) return; made = true; mode = 'sprite'; build(spriteLook.make); }); });
  }

  if (HIVE.story && HIVE.story.subs) HIVE.story.subs.push(onStory);
  else win.addEventListener('scroll', function () { onStory(storyT()); }, { passive: true });
  fade(storyT());
  doc.addEventListener('visibilitychange', function () {
    if (doc.hidden) { cancelAnimationFrame(raf); raf = 0; } else { lastT = -1; schedule(); }
  });
  // the cursor, for the Download swarm (mouse and pen only; a touch is a burst on tap)
  if (!reduced) {
    win.addEventListener('pointermove', function (e) {
      if (e.pointerType === 'touch') return;
      ptr.x = e.clientX; ptr.y = e.clientY; ptr.on = true;
    }, { passive: true });
    doc.documentElement.addEventListener('mouseleave', function () { ptr.on = false; });
    win.addEventListener('blur', function () { ptr.on = false; });
    doc.addEventListener('pointerdown', function (e) {
      if (e.pointerType === 'mouse') return;
      var a = e.target && e.target.closest ? e.target.closest('a') : null;
      if (a && dlEls().indexOf(a) >= 0) { burst.el = a; burst.until = performance.now() / 1000 + 1.3; schedule(); }
    }, { passive: true });
  }
  var rt = 0;
  function onResize() {
    clearTimeout(rt);
    rt = setTimeout(function () {
      if (dead) return;
      sizes(); // (setSize clears the canvas: draw again at once, so there is no blank frame)
      if (!built) return;
      if (reduced) { placeStill(); drawStill(); return; }
      var t = performance.now() / 1000;
      bees.forEach(function (b) { settle(b, t); });
      drawnEmpty = false;
      if (alpha >= 0.01) draw(lastT > 0 ? lastT : t, false);
    }, 150);
  }
  win.addEventListener('resize', onResize);
  win.addEventListener('load', onResize);
  if (doc.fonts && doc.fonts.ready) doc.fonts.ready.then(onResize);
  if (reduced) setTimeout(onResize, 1400); // the still bees: once more after the headline's line reveal
  canvas.addEventListener('webglcontextlost', function (e) {
    e.preventDefault();
    dead = true; cancelAnimationFrame(raf); raf = 0;
    canvas.style.display = 'none';
    mode = 'images'; stillImages(); // the still images take over
  });
  try { start(); } catch (e) { warn(e); }
})();
