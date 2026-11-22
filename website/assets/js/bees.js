/* hive. — the hero bees (home page only)
   Five Buzz robot bees live in the hero and nowhere else. They sit on ONE transparent fixed WebGL overlay
   (pointer-events: none, DPR capped at 1.5, under the sticky header), so every link under them still
   takes the click.

   Each bee has a role:
   - two "letter" bees fly a tour round the headline ("Many laptops. One hive."): they perch just outside
     the glyphs (over the top line, beside each line's ends), hop from spot to spot and now and then loop
     once round a word. Anything that crosses the letters is a short, fast pass; they never park on a word.
   - two "comb" bees stay over the honeycomb on the right (the "fly zone": right of the hero text on wide
     screens, the band under the buttons on narrow ones), hover with a gentle buzz and, one at a time,
     visit a comb cell: settle on its top, the cell glows (HIVE.comb.flash), the bee looks happy.
   - one "wanderer" roams the whole hero, resting only where it hides no text and no button.
   Download attraction: when the mouse comes within ~140 px of a Download button in the hero (or the header
   Download while the hero shows), all five rush to the cursor and buzz round it in a tight ring until the
   cursor leaves, then fly back to their roles. A tap on Download (touch) gives a short swarm burst.

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
     the font's leading); avoid: the sub-line and the buttons, where nothing ever rests. ring: the letter bees'
     perches round the headline, in tour order (up the left side, over the top line word by word, down the
     right side), only the ones that fit on screen clear of the text. */
  var TX = { top: 68, fs: 60, words: [], lines: [], avoid: [], ring: [], at: -9 };
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
  function crossesInk(x0, y0, x1, y1, sy) { // does the straight line between two viewport points cross the letters?
    for (var s = 1; s < 8; s++) {
      var x = lerp(x0, x1, s / 8), y = lerp(y0, y1, s / 8) + sy;
      for (var k = 0; k < TX.lines.length; k++) { var l = TX.lines[k]; if (x > l.l && x < l.r && y > l.t && y < l.b) return true; }
    }
    return false;
  }

  /* ---------------- the still versions: where each of the five rests (viewport px, at this scroll) ---------------- */
  function stillSpots() {
    var R = TX.ring, sy = scrollTop(), ci = 0;
    return ROLES.map(function (role, i) {
      if (role === 'letter' && R.length) {
        var a = R[i === 0 ? Math.floor(R.length * 0.4) : R.length - 1];
        return [a.x, a.y - sy, a.face || (i === 0 ? 1 : -1)];
      }
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
      ai: 0, dir: 1, fast: false, loopW: null, loopT0: 0, loopDur: 1, loopDir: 1, loopCd: 0,
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
    b.moveAt = t + rnd(4.5, 8.5);
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
    visit.next = t + rnd(3.5, 7);
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
        if (b.phase === 'go' && d < LEN * 0.2) { b.phase = 'land'; b.until = t + rnd(2.4, 3.4); b.hold = true; }
        if (b.phase === 'land' && !b.flashed) { b.flashed = true; c.flash(b.cell, 1); setMood(b, 'happy', 2.2, t); }
        if (b.phase === 'land' && t > b.until - 0.3) endVisit(b, t);
        pace(b, b.phase === 'go' ? 220 : 90, 900, 4);
        if (b.task === 'visit') return;
      }
    }
    // hovering: a slow figure around the home spot, the buzz
    if (t > b.moveAt) relocate(b, t);
    b.tx = b.hx + Math.sin(t * 0.55 + b.ph) * LEN * 0.45 + Math.sin(t * 1.3 + b.ph2) * LEN * 0.12;
    b.ty = b.hy + Math.sin(t * 0.8 + b.ph2) * BH * 0.35;
    b.maxV = 150 * s; b.maxA = 900 * s; b.resp = 4;
    // now and then a look around
    if (t > b.glanceAt) { setMood(b, Math.random() < 0.5 ? 'curious' : 'think', rnd(1.2, 2.2), t); b.glanceAt = t + rnd(5, 10); }
    // one visit at a time, only while the bees are fully in the hero
    if (out < 0.01 && t > visit.next && !bees.some(function (o) { return o.task === 'visit'; }) && Math.random() < 0.02) {
      var i = pickCell(b);
      if (i >= 0) { b.task = 'visit'; b.cell = i; b.phase = 'go'; b.flashed = false; b.until = t + 6; HIVE.comb.busy(i, true); }
      else visit.next = t + 2;
    }
  }

  /* ---- letter bees: perch outside the glyphs, hop round the ring, loop a word now and then ---- */
  function otherLetter(b) { for (var k = 0; k < bees.length; k++) if (bees[k] !== b && bees[k].role === 'letter') return bees[k]; return null; }
  function nearestPerch(b, sy, skip) {
    var R = TX.ring, best = 0, bd = 1e9;
    for (var k = 0; k < R.length; k++) {
      if (k === skip && R.length > 1) continue;
      var d = hyp(R[k].x - b.x, R[k].y - sy - b.y);
      if (d < bd) { bd = d; best = k; }
    }
    return best;
  }
  function hop(b, sy) { // off to perch b.ai: a quick pass if the way crosses the letters
    var a = TX.ring[b.ai];
    b.phase = 'go'; b.fast = !!a && crossesInk(b.x, b.y, a.x, a.y - sy, sy);
  }
  function thinkLetter(b, t, sy) {
    var R = TX.ring;
    if (!R.length) { thinkWander(b, t); return; }
    if (b.ai >= R.length) b.ai %= R.length;
    if (b.phase === 'loop') { // once round a word, fast: over it, past its end, under it, back up its start
      var u = (t - b.loopT0) / b.loopDur, w = b.loopW;
      if (u < 1 && w) {
        var ang = -Math.PI / 2 + b.loopDir * u * Math.PI * 2;
        var rx = (w.r - w.l) / 2 + LEN * 0.55, ry = TX.fs * 0.41 + BH * 0.55;
        b.tx = clamp((w.l + w.r) / 2 + Math.cos(ang) * rx, LEN * 0.45, W - LEN * 0.45); b.ty = w.cy - sy + Math.sin(ang) * ry;
        pace(b, 1500, 7000, 10);
        return;
      }
      b.phase = 'perch'; b.until = t + rnd(0.4, 0.9); b.loopCd = t + rnd(5, 9);
    }
    var a = R[b.ai], ax = a.x, ay = a.y - sy;
    if (b.phase === 'go') {
      b.tx = ax; b.ty = ay;
      if (b.fast) pace(b, 820, 2600, 7); else pace(b, 460, 1700, 5);
      if (hyp(ax - b.x, ay - b.y) < LEN * 0.3) { b.phase = 'perch'; b.until = t + rnd(0.55, 1.5); }
      return;
    }
    // perched: a small buzz on the spot, never drifting onto the letters
    b.tx = ax + Math.sin(t * 1.7 + b.ph) * LEN * 0.1; b.ty = ay + Math.sin(t * 2.3 + b.ph2) * BH * 0.1;
    pace(b, 160, 900, 5);
    if (b.phase !== 'perch') { hop(b, sy); return; }
    if (t > b.glanceAt) { setMood(b, Math.random() < 0.6 ? 'curious' : 'happy', rnd(0.8, 1.6), t); b.glanceAt = t + rnd(4, 8); }
    if (t < b.until) return;
    if (a.w && t > b.loopCd && Math.random() < 0.3) {
      b.phase = 'loop'; b.loopW = a.w; b.loopT0 = t; b.loopDur = rnd(1.05, 1.35); b.loopDir = Math.random() < 0.5 ? 1 : -1;
      return;
    }
    if (Math.random() < 0.12) b.dir = -b.dir;
    var o = otherLetter(b), nx = (b.ai + b.dir + R.length) % R.length;
    if (o && R.length > 2 && o.ai === nx) nx = (nx + b.dir + R.length) % R.length; // never two on one perch
    b.ai = nx;
    hop(b, sy);
  }

  /* ---- the wanderer: anywhere in the hero, resting only where it hides no text and no button ---- */
  function pickWander(b, t) {
    var sy = scrollTop(), best = null, bs = -1;
    for (var k = 0; k < 24; k++) {
      var x = rnd(LEN * 0.6, W - LEN * 0.6), y = rnd(TX.top + BH * 0.9, H - BH * 0.8);
      if (!restOK(x, y + sy, 0)) continue;
      var sc = Math.min(hyp(x - b.x, y - b.y), W * 0.45); // roam: prefer far from where it is
      for (var j = 0; j < bees.length; j++) if (bees[j] !== b && hyp(bees[j].x - x, bees[j].y - y) < LEN * 1.4) sc -= W;
      if (sc > bs) { bs = sc; best = [x, y]; }
    }
    if (best) { b.hx = best[0]; b.hy = best[1]; }
    else { var s = spot(b); b.hx = zx(s[0]); b.hy = zy(s[1]); }
    b.moveAt = t + rnd(2.4, 4.8);
  }
  function thinkWander(b, t) {
    if (t > b.moveAt) pickWander(b, t);
    b.tx = b.hx + Math.sin(t * 0.9 + b.ph) * LEN * 0.22; b.ty = b.hy + Math.sin(t * 1.3 + b.ph2) * BH * 0.2;
    pace(b, 300, 1100, 4);
    if (t > b.glanceAt) { setMood(b, Math.random() < 0.5 ? 'curious' : 'think', rnd(1, 2), t); b.glanceAt = t + rnd(5, 9); }
  }

  /* ---- the Download swarm: a tight buzzing ring round the cursor ---- */
  var ptr = { x: -1e4, y: -1e4, on: false };
  var burst = { x: 0, y: 0, until: -1 };
  var sw = { on: false, x: 0, y: 0, t0: 0 };
  var NEAR = 140, LEAVE = 175;
  function dlEls() {
    var out = [];
    doc.querySelectorAll('.hero-cta a[href$="download.html"], .site-header .header-cta').forEach(function (el) {
      var r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0 && win.getComputedStyle(el).display !== 'none') out.push(el);
    });
    return out;
  }
  function dlDist(x, y) {
    var best = 1e9;
    dlEls().forEach(function (el) {
      var r = el.getBoundingClientRect();
      var dx = Math.max(r.left - x, 0, x - r.right), dy = Math.max(r.top - y, 0, y - r.bottom);
      best = Math.min(best, hyp(dx, dy));
    });
    return best;
  }
  function swarmCheck(t, out) {
    var want = false, x = ptr.x, y = ptr.y;
    if (out < 0.3) {
      if (t < burst.until) { want = true; x = burst.x; y = burst.y; }
      else if (ptr.on) want = dlDist(ptr.x, ptr.y) <= (sw.on ? LEAVE : NEAR);
    }
    if (want && !sw.on) {
      sw.on = true; sw.t0 = t;
      bees.forEach(function (b) {
        if (b.task === 'visit') endVisit(b, t);
        b.hold = false; b.phase = '';
        setMood(b, Math.random() < 0.5 ? 'surprised' : 'happy', 0.6, t);
      });
    } else if (!want && sw.on) {
      sw.on = false;
      var sy = scrollTop();
      bees.forEach(function (b) { // back to work
        setMood(b, 'curious', 0.9, t);
        if (b.role === 'letter') { var o = otherLetter(b); b.ai = nearestPerch(b, sy, o && o.i < b.i ? o.ai : -1); hop(b, sy); b.fast = true; }
        else if (b.role === 'comb') { b.task = 'hover'; relocate(b, t); }
        else pickWander(b, t);
      });
    }
    if (sw.on) { sw.x = x; sw.y = y; }
  }
  function thinkSwarm(b, t) {
    var n = bees.length, ang = (t - sw.t0) * 4.4 + b.i * Math.PI * 2 / n;
    var r = LEN * (0.64 + 0.07 * Math.sin(t * 5.3 + b.i * 1.7));
    b.tx = sw.x + Math.cos(ang) * r; b.ty = sw.y + Math.sin(ang) * r * 0.72;
    pace(b, 1700, 7000, 10);
    if (t > b.moodUntil) setMood(b, 'happy', 1.5, t);
  }

  /* ---- one bee's step: where it wants to be ---- */
  function think(b, t, out, sy) {
    if (sw.on) { thinkSwarm(b, t); return; }
    if (b.role === 'letter') thinkLetter(b, t, sy);
    else if (b.role === 'comb') thinkComb(b, t, out);
    else thinkWander(b, t);
  }

  /* ---- steering: arrive + separation, acceleration-limited ---- */
  function steer(b, dt, out) {
    // flying out: off the right edge, a little up
    var tx = lerp(b.tx, W + LEN * (2.5 + b.i * 0.4), out), ty = b.ty - out * H * 0.06;
    var dx = tx - b.x, dy = ty - b.y, d = hyp(dx, dy), slowR = LEN * 1.2;
    var maxV = out > 0.01 ? Math.max(b.maxV, 420 * speedScale()) : b.maxV;
    var sp = maxV * Math.min(1, d / slowR);
    var wx = d > 0.01 ? dx / d * sp : 0, wy = d > 0.01 ? dy / d * sp : 0;
    var R = LEN * (sw.on ? 0.5 : 1.05), sx = 0, sy = 0;
    for (var i = 0; i < bees.length; i++) {
      var o = bees[i];
      if (o === b) continue;
      var ox = b.x - o.x, oy = b.y - o.y, od = hyp(ox, oy);
      if (od < R && od > 0.001) { var f = (R - od) / R; sx += ox / od * f; sy += oy / od * f; }
    }
    var sepK = (sw.on ? 90 : b.hold ? 40 : 200) * speedScale();
    wx += sx * sepK; wy += sy * sepK;
    var maxA = Math.max(b.maxA, out > 0.01 ? 1800 * speedScale() : 0);
    var ax = (wx - b.vx) * b.resp, ay = (wy - b.vy) * b.resp, am = hyp(ax, ay);
    if (am > maxA) { ax *= maxA / am; ay *= maxA / am; }
    b.vx += ax * dt; b.vy += ay * dt;
    b.x += b.vx * dt; b.y += b.vy * dt;
    var k = damp(7, dt);
    b.ax += (ax - b.ax) * k; b.ay += (ay - b.ay) * k;
  }

  /* ---- facing, pitch, bank, bob, jets, blink ---- */
  function orient(b, t, dt) {
    var v = hyp(b.vx, b.vy), s = speedScale();
    if (b.hold) b.faceT = b.x > (Z.l + Z.r) / 2 ? -1 : 1; // settled on a cell: turned to the comb's middle
    else if (Math.abs(b.vx) > 26 * s) b.faceT = b.vx > 0 ? 1 : -1;
    b.face += (b.faceT - b.face) * damp(sw.on ? 9 : 6, dt);
    var pt = v > 30 * s && !b.hold ? clamp(Math.atan2(-b.vy, Math.abs(b.vx) + 60 * s), -0.75, 0.75) * 0.55 : (b.hold ? 0.08 : 0);
    b.pitch += (pt - b.pitch) * damp(6, dt);
    var lat = v > 1 ? (b.vx * b.ay - b.vy * b.ax) / v : 0;
    b.roll += (clamp(lat / (2600 * s), -0.4, 0.4) - b.roll) * damp(5, dt);
    var amp = (b.hold ? 0.9 : 5 * (1 - 0.5 * clamp(v / (300 * s), 0, 1))) * s;
    b.bob += (amp - b.bob) * damp(4, dt);
    b.ry = b.y + Math.sin(t * 2.4 + b.ph) * b.bob + Math.sin(t * 23 + b.ph2) * 0.45 * s; // the slow bob and the buzz
    b.jet += (clamp(v / (320 * s) + hyp(b.ax, b.ay) / (2500 * s), b.hold ? 0.08 : 0.28, 1) - b.jet) * damp(6, dt);
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
    for (var i = 0; i < bees.length; i++) { think(bees[i], t, out, sy); steer(bees[i], dt, out); orient(bees[i], t, dt); }
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
    if (b.role === 'letter') { b.ai = TX.ring.length ? (b.i === 0 ? 0 : Math.floor(TX.ring.length / 2)) : 0; b.dir = b.i === 0 ? 1 : -1; hop(b, scrollTop()); }
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
      b.x = W + LEN * (0.8 + k * 0.6); b.y = rnd(TX.top + BH, H - BH); b.vx = -260 * speedScale();
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
      if (a && dlEls().indexOf(a) >= 0) { burst.x = e.clientX; burst.y = e.clientY; burst.until = performance.now() / 1000 + 1.3; schedule(); }
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
