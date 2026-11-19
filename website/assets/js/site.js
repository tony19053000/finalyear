/* hive. — shared behaviour: OS detection, copy buttons, reveal-on-scroll,
   header, mobile menu, mesh texture, placeholder download links. */
(function () {
  'use strict';
  var doc = document;
  var root = doc.documentElement;
  var mq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
  var HIVE = (window.HIVE = window.HIVE || {});

  HIVE.version = '0.17.5';
  HIVE.reduced = mq.matches;

  /* Download links. The docs do not publish real download URLs yet, so these are
     placeholders. Replace each value with the real URL when the release page exists. */
  HIVE.downloads = {
    windows: '#download-windows',          // hive-Setup-0.17.5.exe
    agentDeb: '#download-linux-agent',     // hive-agent_0.17.5_amd64.deb
    controllerDeb: '#download-linux-controller', // hive-controller_0.17.5_amd64.deb
    sums: '#download-sha256sums'           // SHA256SUMS
  };

  /* ---------- OS detection ---------- */
  function detectOS() {
    var ua = navigator.userAgent || '';
    var plat = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || '';
    if (/android|iphone|ipad|ipod/i.test(ua)) return 'other';
    if (/win/i.test(plat) || /windows/i.test(ua)) return 'windows';
    if (/linux|x11|ubuntu/i.test(plat + ' ' + ua) && !/cros/i.test(ua)) return 'linux';
    return 'other';
  }
  HIVE.os = detectOS();
  root.classList.add('os-' + HIVE.os);

  /* ---------- toast + live region ---------- */
  var toastEl, toastTimer, live;
  HIVE.toast = function (html, ms) {
    if (!toastEl) {
      toastEl = doc.createElement('div');
      toastEl.className = 'toast';
      toastEl.setAttribute('role', 'status');
      doc.body.appendChild(toastEl);
    }
    toastEl.innerHTML = html;
    toastEl.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove('show'); }, ms || 3800);
  };
  function announce(text) {
    if (!live) {
      live = doc.createElement('div');
      live.className = 'sr-only';
      live.setAttribute('aria-live', 'polite');
      doc.body.appendChild(live);
    }
    live.textContent = '';
    setTimeout(function () { live.textContent = text; }, 30);
  }

  /* ---------- copy ---------- */
  function fallbackCopy(text) {
    return new Promise(function (resolve, reject) {
      var ta = doc.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none';
      doc.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = doc.execCommand('copy'); } catch (e) { ok = false; }
      ta.remove();
      ok ? resolve() : reject(new Error('copy failed'));
    });
  }
  HIVE.copy = function (text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(function () { return fallbackCopy(text); });
    }
    return fallbackCopy(text);
  };
  var COPY_ICON = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="2"/><path d="M10.5 5.5V4a1.5 1.5 0 0 0-1.5-1.5H4A1.5 1.5 0 0 0 2.5 4v5A1.5 1.5 0 0 0 4 10.5h1.5"/></svg>';
  function textOf(pre) {
    var clone = pre.cloneNode(true);
    clone.querySelectorAll('.p').forEach(function (n) { n.remove(); });
    return clone.textContent.replace(/\s+$/g, '').replace(/^\s+/, '');
  }
  function addCopyButtons(scope) {
    (scope || doc).querySelectorAll('.cmd').forEach(function (box) {
      if (box.querySelector('.copy-btn')) return;
      var pre = box.querySelector('pre');
      if (!pre) return;
      var btn = doc.createElement('button');
      btn.type = 'button';
      btn.className = 'copy-btn';
      var what = box.getAttribute('data-label') || 'command';
      btn.setAttribute('aria-label', 'Copy ' + what);
      btn.innerHTML = COPY_ICON + '<span>Copy</span>';
      btn.addEventListener('click', function () {
        HIVE.copy(textOf(pre)).then(function () {
          btn.classList.add('copied');
          btn.querySelector('span').textContent = 'Copied';
          announce('Copied to clipboard');
          setTimeout(function () {
            btn.classList.remove('copied');
            btn.querySelector('span').textContent = 'Copy';
          }, 1700);
        }, function () {
          HIVE.toast('Could not copy automatically. Select the text and press <b>Ctrl+C</b>.');
        });
      });
      box.appendChild(btn);
    });
  }
  HIVE.addCopyButtons = addCopyButtons;

  /* ---------- placeholder download links ---------- */
  function wireDownloads() {
    doc.querySelectorAll('[data-dl]').forEach(function (a) {
      var key = a.getAttribute('data-dl');
      var href = HIVE.downloads[key];
      if (!href) return;
      a.setAttribute('href', href);
      if (href.charAt(0) === '#') {
        a.addEventListener('click', function (e) {
          e.preventDefault();
          HIVE.toast('<b>Download link not published yet.</b> Get the file from your team\'s release page or from the person who runs your fabric.', 5200);
        });
      }
    });
  }

  /* ---------- reveal on scroll ---------- */
  function reveal() {
    var els = doc.querySelectorAll('.reveal');
    if (!('IntersectionObserver' in window) || HIVE.reduced) {
      els.forEach(function (el) { el.classList.add('in'); });
      return;
    }
    // anything already on screen at load is shown at once, without a transition
    var vh = window.innerHeight || doc.documentElement.clientHeight;
    els.forEach(function (el) {
      if (el.getBoundingClientRect().top < vh) el.classList.add('in', 'now');
    });
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { en.target.classList.add('in'); io.unobserve(en.target); }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -6% 0px' });
    els.forEach(function (el) { if (!el.classList.contains('in')) io.observe(el); });
  }

  /* ---------- header + menu ---------- */
  function header() {
    var h = doc.querySelector('.site-header');
    if (h) {
      var onScroll = function () { h.classList.toggle('scrolled', window.scrollY > 8); };
      onScroll();
      window.addEventListener('scroll', onScroll, { passive: true });
    }
    var btn = doc.querySelector('.menu-btn');
    if (!btn) return;
    function set(open) {
      root.classList.toggle('menu-open', open);
      btn.setAttribute('aria-expanded', String(open));
      btn.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
    }
    btn.addEventListener('click', function () { set(!root.classList.contains('menu-open')); });
    doc.querySelectorAll('.site-nav a').forEach(function (a) { a.addEventListener('click', function () { set(false); }); });
    doc.addEventListener('keydown', function (e) { if (e.key === 'Escape') set(false); });
    window.addEventListener('resize', function () { if (window.innerWidth > 820) set(false); });
  }

  /* ---------- faint web/mesh texture ---------- */
  function mesh(canvas) {
    var ctx = canvas.getContext('2d');
    if (!ctx) return;
    var pts = [], w = 0, h = 0, dpr = 1, running = false, raf = 0, visible = true;
    function size() {
      var r = canvas.getBoundingClientRect();
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = r.width; h = r.height;
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      var n = Math.max(14, Math.min(56, Math.round(w * h / 21000)));
      pts = [];
      for (var i = 0; i < n; i++) {
        pts.push({ x: Math.random() * w, y: Math.random() * h, vx: (Math.random() - .5) * .12, vy: (Math.random() - .5) * .12, honey: Math.random() < .12 });
      }
    }
    function draw() {
      ctx.clearRect(0, 0, w, h);
      var max = 150, i, j, a, b, dx, dy, d;
      for (i = 0; i < pts.length; i++) {
        a = pts[i];
        for (j = i + 1; j < pts.length; j++) {
          b = pts[j]; dx = a.x - b.x; dy = a.y - b.y; d = Math.sqrt(dx * dx + dy * dy);
          if (d < max) {
            ctx.strokeStyle = 'rgba(154,146,132,' + ((1 - d / max) * .13).toFixed(3) + ')';
            ctx.lineWidth = .7;
            ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
          }
        }
      }
      for (i = 0; i < pts.length; i++) {
        a = pts[i];
        ctx.fillStyle = a.honey ? 'rgba(255,201,60,.55)' : 'rgba(154,146,132,.28)';
        ctx.beginPath(); ctx.arc(a.x, a.y, a.honey ? 1.6 : 1.1, 0, Math.PI * 2); ctx.fill();
      }
    }
    function step() {
      for (var i = 0; i < pts.length; i++) {
        var p = pts[i];
        p.x += p.vx; p.y += p.vy;
        if (p.x < -20) p.x = w + 20; if (p.x > w + 20) p.x = -20;
        if (p.y < -20) p.y = h + 20; if (p.y > h + 20) p.y = -20;
      }
      draw();
      raf = requestAnimationFrame(step);
    }
    function start() { if (!running && !HIVE.reduced && visible && !doc.hidden) { running = true; raf = requestAnimationFrame(step); } }
    function stop() { running = false; cancelAnimationFrame(raf); }
    size(); draw();
    var rt;
    window.addEventListener('resize', function () { clearTimeout(rt); rt = setTimeout(function () { size(); draw(); }, 150); });
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (en) { visible = en[0].isIntersecting; visible ? start() : stop(); }).observe(canvas);
    }
    doc.addEventListener('visibilitychange', function () { doc.hidden ? stop() : start(); });
    start();
  }

  function init() {
    header();
    wireDownloads();
    addCopyButtons();
    reveal();
    doc.querySelectorAll('canvas.mesh-canvas').forEach(mesh);
    doc.querySelectorAll('[data-version]').forEach(function (el) { el.textContent = HIVE.version; });
  }
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', init); else init();
})();
