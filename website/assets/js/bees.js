/* hive. — the hero bees (home page only)
   Four Buzz robot bees live in the hero, around the packed honeycomb on the right, and nowhere else.
   They sit on ONE transparent fixed WebGL overlay (pointer-events: none, DPR capped at 1.5, under the
   sticky header) and stay inside a "fly zone": right of the hero text on wide screens, the band under the
   buttons on narrow ones, so they never cover the headline, the text or the buttons.

   Each bee hovers around its own spot with a gentle buzz (bob, small drifts, blinks, a look around) and
   now and then moves to a new spot. One bee at a time visits a comb cell: it flies to the cell's top,
   settles on it, the cell glows (HIVE.comb.flash) and the bee looks happy, then it takes off again.

   Hero only, driven by the story timeline (HIVE.story.T, from home.js): full in the hero (T < 0.45),
   flying out to the right and fading as the first story beat comes in (gone by T = 0.8). Scrolling back
   up brings them back. While they are gone the loop stops, so later beats pay nothing for them.

   Degrades: GLB fails -> flat Buzz sprites on the same overlay; no WebGL -> four still Buzz images in the
   fly zone (fading with the same timeline); reduced motion -> four still bees, no flying, no clock. */
(function () {
  'use strict';
  var doc = document, win = window, HIVE = win.HIVE || (win.HIVE = {});
  if (!doc.body || !doc.body.classList.contains('home')) return;
  var THREE = win.THREE;
  var reduced = !!HIVE.reduced || !!(win.matchMedia && win.matchMedia('(prefers-reduced-motion: reduce)').matches);
  var COUNT = 4;

  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
  function lerp(a, b, t) { return a + (b - a) * t; }
  function rnd(a, b) { return a + Math.random() * (b - a); }
  function hyp(x, y) { return Math.sqrt(x * x + y * y); }
  function damp(k, dt) { return 1 - Math.exp(-k * dt); }
  function smooth(a, b, v) { var t = clamp((v - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); }
  function warn(e) { if (win.console) console.warn('[hive bees]', e); }

  /* ---------------- hero only: how present the bees are at story time T ---------------- */
  function storyT() {
    var s = HIVE.story;
    if (s && typeof s.T === 'number') return s.T;
    return (win.scrollY || win.pageYOffset || 0) / Math.max(1, win.innerHeight); // no timeline: one screen of scroll
  }
  function outOf(T) { return smooth(0.45, 0.8, T); }      // 0 in the hero, 1 once beat 1 is in: they fly off right
  function alphaOf(T) { return 1 - smooth(0.55, 0.8, T); } // and fade out on the way

  /* ---------------- sizes and the fly zone (viewport px) ---------------- */
  var W = 1, H = 1, LEN = 90, BH = 63;
  var MLEN = 0.885, MHT = 0.62; // model length / height in model units (measured when the GLB loads)
  var Z = { l: 0, r: 1, t: 0, b: 1, side: true };
  function measure() {
    W = win.innerWidth || doc.documentElement.clientWidth || 1;
    H = win.innerHeight || doc.documentElement.clientHeight || 1;
    LEN = W < 760 ? clamp(W * 0.15, 46, 66) : Math.min(clamp(H * 0.1, 64, 112), W * 0.12);
    BH = LEN * MHT / MLEN;
  }
  // the hero text (headline, line, buttons) is measured in page coordinates: the hero starts at the top of
  // the page, so at scroll 0 these are viewport coordinates, and as the page scrolls the text only moves up,
  // away from the zone
  function zone() {
    var sy = win.scrollY || win.pageYOffset || 0, right = 0, bottom = 0, top = 68;
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
  }
  function zx(f) { return lerp(Z.l, Z.r, f); }
  function zy(f) { return lerp(Z.t, Z.b, f); }
  function inZone(x, y, m) { return x > Z.l + m && x < Z.r - m && y > Z.t + m && y < Z.b - m; }
  // the four home spots (shares of the zone, and the way each bee faces when still), spread out
  var SPOTS_SIDE = [[0.16, 0.2, -1], [0.74, 0.1, -1], [0.36, 0.68, 1], [0.88, 0.58, -1]];
  var SPOTS_BAND = [[0.1, 0.3, 1], [0.38, 0.8, 1], [0.64, 0.22, -1], [0.92, 0.72, -1]];
  function spots() { return Z.side ? SPOTS_SIDE : SPOTS_BAND; }

  /* ---------------- no WebGL: four still Buzz images in the fly zone ---------------- */
  var imgs = null;
  function stillImages() {
    if (imgs) return;
    var box = doc.createElement('div');
    box.className = 'bees-still';
    box.setAttribute('aria-hidden', 'true');
    imgs = { box: box, list: [] };
    var names = ['c1', 'c1-happy', 'c1', 'c1-think'];
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
    if (doc.fonts && doc.fonts.ready) doc.fonts.ready.then(placeImages);
    HIVE.bees = { mode: function () { return 'images'; }, zone: function () { return Z; },
      visible: function () { return imgs.box.style.visibility === 'hidden' ? 0 : imgs.list.length; } };
  }
  function placeImages() {
    if (!imgs) return;
    measure(); zone();
    var sp = spots(), size = LEN * 1.15;
    imgs.list.forEach(function (im, i) {
      var s = im.style;
      s.width = s.height = size.toFixed(1) + 'px';
      s.left = zx(sp[i][0]).toFixed(1) + 'px'; s.top = zy(sp[i][1]).toFixed(1) + 'px';
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
  var bees = [], ST = { x: 0, y: 0, z: 0, k: 1, face: 1, pitch: 0, roll: 0, carry: false, jet: 0, t: 0, mood: 'neutral', blink: 0, headRoll: 0, headNod: 0, snap: false };
  var ptr = { x: -1e4, y: -1e4, vx: 0, vy: 0, t: -9, lx: 0, ly: 0, lt: 0 };
  var scr = { v: 0, last: 0, d: 0, at: -9, surp: -9 };
  var mgr = { recruit: 0, peel: 0, recycle: 0, notice: 0, noticeCd: 4 };
  var SLOTS = [{ x: 0.84, y: 0.32 }, { x: 0.12, y: 0.6 }, { x: 0.74, y: 0.74 }];
  var P = { x: 0, y: 0 }, P2 = { x: 0, y: 0 };

  /* ---------------- the bees ---------------- */
  var bees = [], ST = { x: 0, y: 0, z: 1, k: 1, face: 1, pitch: 0, roll: 0, carry: false, jet: 0, t: 0, mood: 'neutral', blink: 0, headRoll: 0, headNod: 0, snap: false };
  var visit = { next: 0 }; // when the next cell visit may start (one bee at a time)
  var CP = { x: 0, y: 0 };

  function newBee(look, i) {
    return { look: look, i: i, x: 0, y: 0, vx: 0, vy: 0, ax: 0, ay: 0, tx: 0, ty: 0, maxV: 150,
      hx: 0, hy: 0, moveAt: 0, task: 'hover', cell: -1, phase: '', until: 0, flashed: false, hold: false,
      face: -1, faceT: -1, pitch: 0, roll: 0, bob: 0, ry: 0, jet: 0.5, ph: Math.random() * 6.283, ph2: Math.random() * 6.283,
      mood: 'neutral', moodUntil: 0, moodT: 'neutral', blinkAt: rnd(1, 4), blinkT: -9, headRoll: 0, glanceAt: rnd(3, 7), shown: false };
  }
  function speedScale() { return Math.sqrt(LEN / 150); }
  function setMood(b, m, dur, t) { b.moodT = m; b.moodUntil = t + dur; }
  function home(b) { var s = spots()[b.i]; b.hx = zx(s[0]); b.hy = zy(s[1]); }
  // a new spot near the bee's own home share, clear of the other bees
  function relocate(b, t) {
    var s = spots()[b.i];
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

  /* ---- one bee's step: where it wants to be ---- */
  function think(b, t, out) {
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
        b.maxV = (b.phase === 'go' ? 220 : 90) * s;
        if (b.task === 'visit') return;
      }
    }
    // hovering: a slow figure around the home spot, the buzz
    if (t > b.moveAt) relocate(b, t);
    b.tx = b.hx + Math.sin(t * 0.55 + b.ph) * LEN * 0.45 + Math.sin(t * 1.3 + b.ph2) * LEN * 0.12;
    b.ty = b.hy + Math.sin(t * 0.8 + b.ph2) * BH * 0.35;
    b.maxV = 150 * s;
    // now and then a look around
    if (t > b.glanceAt) { setMood(b, Math.random() < 0.5 ? 'curious' : 'think', rnd(1.2, 2.2), t); b.glanceAt = t + rnd(5, 10); }
    // one visit at a time, only while the bees are fully in the hero
    if (out < 0.01 && t > visit.next && !bees.some(function (o) { return o.task === 'visit'; }) && Math.random() < 0.02) {
      var i = pickCell(b);
      if (i >= 0) { b.task = 'visit'; b.cell = i; b.phase = 'go'; b.flashed = false; b.until = t + 6; HIVE.comb.busy(i, true); }
      else visit.next = t + 2;
    }
  }

  /* ---- steering: arrive + separation, acceleration-limited ---- */
  function steer(b, dt, out) {
    var tx = b.tx + out * (W * 0.42 + LEN * 2.5), ty = b.ty - out * H * 0.06; // flying out to the right
    var dx = tx - b.x, dy = ty - b.y, d = hyp(dx, dy), slowR = LEN * 1.2;
    var maxV = out > 0.01 ? Math.max(b.maxV, 420 * speedScale()) : b.maxV;
    var sp = maxV * Math.min(1, d / slowR);
    var wx = d > 0.01 ? dx / d * sp : 0, wy = d > 0.01 ? dy / d * sp : 0;
    var R = LEN * 1.05, sx = 0, sy = 0;
    for (var i = 0; i < bees.length; i++) {
      var o = bees[i];
      if (o === b) continue;
      var ox = b.x - o.x, oy = b.y - o.y, od = hyp(ox, oy);
      if (od < R && od > 0.001) { var f = (R - od) / R; sx += ox / od * f; sy += oy / od * f; }
    }
    var sepK = (b.hold ? 40 : 200) * speedScale();
    wx += sx * sepK; wy += sy * sepK;
    var maxA = 900 * speedScale() * (out > 0.01 ? 2 : 1);
    var ax = (wx - b.vx) * 4, ay = (wy - b.vy) * 4, am = hyp(ax, ay);
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
    b.face += (b.faceT - b.face) * damp(6, dt);
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
    var out = outOf(storyT());
    for (var i = 0; i < bees.length; i++) { think(bees[i], t, out); steer(bees[i], dt, out); orient(bees[i], t, dt); }
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

  /* ---------------- reduced motion: four still bees in the zone ---------------- */
  function placeStill() {
    zone();
    bees.forEach(function (b) {
      var s = spots()[b.i];
      b.x = zx(s[0]); b.y = b.ry = zy(s[1]); b.face = b.faceT = s[2]; b.pitch = b.roll = 0; b.jet = 0.3; b.headRoll = 0;
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
      size: function () { return { LEN: LEN, BH: BH }; },
      visible: function () { return alpha < 0.01 ? 0 : bees.filter(function (b) { return b.shown; }).length; },
      advance: function (s) {
        var ts = lastT < 0 ? performance.now() : lastT * 1000;
        for (var k = 0; k < s * 60; k++) { ts += 1000 / 60; var tt = ts / 1000; step(tt, 1 / 60); lastT = tt; }
        draw(lastT, false);
      } };
    fade(storyT());
    if (reduced) { placeStill(); drawStill(); return; }
    // the landing: the bees arrive from the right, one after another, each to its home spot
    bees.forEach(function (b, k) {
      home(b); b.moveAt = t + rnd(5, 9);
      b.x = W + LEN * (0.8 + k * 0.6); b.y = b.hy + rnd(-BH, BH); b.vx = -260 * speedScale();
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
  var rt = 0;
  function onResize() {
    clearTimeout(rt);
    rt = setTimeout(function () {
      if (dead) return;
      sizes(); // (setSize clears the canvas: draw again at once, so there is no blank frame)
      if (!built) return;
      if (reduced) { placeStill(); drawStill(); return; }
      var t = performance.now() / 1000;
      bees.forEach(function (b) { if (b.task === 'visit') endVisit(b, t); home(b); });
      drawnEmpty = false;
      if (alpha >= 0.01) draw(lastT > 0 ? lastT : t, false);
    }, 150);
  }
  win.addEventListener('resize', onResize);
  win.addEventListener('load', onResize);
  if (doc.fonts && doc.fonts.ready) doc.fonts.ready.then(onResize);
  canvas.addEventListener('webglcontextlost', function (e) {
    e.preventDefault();
    dead = true; cancelAnimationFrame(raf); raf = 0;
    canvas.style.display = 'none';
    mode = 'images'; stillImages(); // the still images take over
  });
  try { start(); } catch (e) { warn(e); }
})();
