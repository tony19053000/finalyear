/* hive. docs — client-side search, section scroll-spy, "on this page" list, mobile contents toggle. */
(function () {
  'use strict';
  var doc = document;
  var content = doc.getElementById('doc');
  if (!content) return;
  var reduced = window.HIVE && HIVE.reduced;

  /* ---------- build an index of h2/h3 sections ---------- */
  var sections = Array.prototype.slice.call(content.querySelectorAll('.doc-sec'));
  var entries = [];
  sections.forEach(function (sec) {
    var h = sec.querySelector('h1, h2');
    var title = h ? h.textContent.trim() : sec.id;
    var sub = Array.prototype.slice.call(sec.querySelectorAll('h3[id]'));
    // text of the section before the first h3
    var parts = [], node = h ? h.nextElementSibling : sec.firstElementChild, cur = { id: sec.id, title: title, parent: null, text: '' };
    var buckets = [cur];
    Array.prototype.forEach.call(sec.children, function (child) {
      if (child === h) return;
      if (child.tagName === 'H3' && child.id) {
        cur = { id: child.id, title: child.textContent.trim(), parent: title, text: '' };
        buckets.push(cur);
        return;
      }
      cur.text += ' ' + child.textContent;
    });
    buckets.forEach(function (b) { b.text = b.text.replace(/\s+/g, ' ').trim(); entries.push(b); });
    sec._h3 = sub;
    sec._title = title;
  });

  /* ---------- search ---------- */
  var input = doc.getElementById('doc-search');
  var box = doc.getElementById('search-results');
  var active = -1, results = [];
  function esc(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  function highlight(s, terms) {
    var out = esc(s);
    terms.forEach(function (t) {
      if (!t) return;
      var re = new RegExp('(' + esc(t).replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'ig');
      out = out.replace(re, '<mark>$1</mark>');
    });
    return out;
  }
  function snippet(text, terms) {
    var low = text.toLowerCase(), pos = -1;
    for (var i = 0; i < terms.length && pos < 0; i++) pos = low.indexOf(terms[i]);
    if (pos < 0) return text.slice(0, 120) + (text.length > 120 ? '…' : '');
    var start = Math.max(0, pos - 40), end = Math.min(text.length, pos + 90);
    return (start > 0 ? '…' : '') + text.slice(start, end) + (end < text.length ? '…' : '');
  }
  function search(q) {
    var terms = q.toLowerCase().split(/\s+/).filter(function (t) { return t.length > 0; });
    if (!terms.length) return [];
    var scored = [];
    entries.forEach(function (e) {
      var tl = e.title.toLowerCase(), xl = e.text.toLowerCase(), score = 0, all = true;
      terms.forEach(function (t) {
        var inT = tl.indexOf(t) >= 0, inX = xl.indexOf(t) >= 0;
        if (!inT && !inX) all = false;
        if (inT) score += 10;
        if (inX) score += 2 + Math.min(4, xl.split(t).length - 1);
      });
      if (all && score) scored.push({ e: e, score: score });
    });
    scored.sort(function (a, b) { return b.score - a.score; });
    return scored.slice(0, 8).map(function (s) { return s.e; }).map(function (e) {
      return { e: e, terms: terms };
    });
  }
  function render() {
    var q = input.value.trim();
    results = q ? search(q) : [];
    active = results.length ? 0 : -1;
    if (!q) { close(); return; }
    if (!results.length) {
      box.innerHTML = '<p class="sr-empty">No results for “' + esc(q) + '”. Try <b>join</b>, <b>doctor</b> or a code like <b>expired</b>.</p>';
    } else {
      box.innerHTML = results.map(function (r, i) {
        return '<a role="option" id="sr-' + i + '" href="#' + r.e.id + '" class="sr-item' + (i === active ? ' on' : '') + '" aria-selected="' + (i === active) + '">' +
          '<span class="sr-path">' + esc(r.e.parent || 'Guide') + '</span>' +
          '<b>' + highlight(r.e.title, r.terms) + '</b>' +
          '<span class="sr-snip">' + highlight(snippet(r.e.text, r.terms), r.terms) + '</span></a>';
      }).join('');
    }
    box.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    if (active >= 0) input.setAttribute('aria-activedescendant', 'sr-' + active); else input.removeAttribute('aria-activedescendant');
  }
  function close() {
    box.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
  }
  function move(n) {
    if (!results.length) return;
    active = (active + n + results.length) % results.length;
    box.querySelectorAll('.sr-item').forEach(function (a, i) {
      a.classList.toggle('on', i === active);
      a.setAttribute('aria-selected', String(i === active));
      if (i === active) a.scrollIntoView({ block: 'nearest' });
    });
    input.setAttribute('aria-activedescendant', 'sr-' + active);
  }
  function go(id) {
    var t = doc.getElementById(id);
    close();
    if (!t) return;
    t.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' });
    if (history.replaceState) history.replaceState(null, '', '#' + id);
    t.classList.remove('flash'); void t.offsetWidth; t.classList.add('flash');
    closeSide();
  }
  if (input && box) {
    input.addEventListener('input', render);
    input.addEventListener('focus', function () { if (input.value.trim()) render(); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
      else if (e.key === 'Enter') { if (results[active]) { e.preventDefault(); go(results[active].e.id); input.blur(); } }
      else if (e.key === 'Escape') { input.value = ''; close(); input.blur(); }
    });
    box.addEventListener('click', function (e) {
      var a = e.target.closest('.sr-item');
      if (!a) return;
      e.preventDefault();
      go(a.getAttribute('href').slice(1));
    });
    doc.addEventListener('click', function (e) { if (!e.target.closest('.search')) close(); });
    doc.addEventListener('keydown', function (e) {
      var tag = (doc.activeElement && doc.activeElement.tagName) || '';
      if ((e.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(tag)) || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k')) {
        e.preventDefault();
        input.focus();
        input.select();
      }
    });
  }

  /* ---------- mobile contents toggle ---------- */
  var toggle = doc.querySelector('.side-toggle');
  var side = doc.getElementById('side-nav');
  function closeSide() { if (toggle) { toggle.setAttribute('aria-expanded', 'false'); side.classList.remove('open'); } }
  if (toggle && side) {
    toggle.addEventListener('click', function () {
      var open = toggle.getAttribute('aria-expanded') !== 'true';
      toggle.setAttribute('aria-expanded', String(open));
      side.classList.toggle('open', open);
    });
    side.querySelectorAll('a').forEach(function (a) { a.addEventListener('click', closeSide); });
  }

  /* ---------- scroll spy + on this page ---------- */
  var sideLinks = Array.prototype.slice.call(doc.querySelectorAll('.side-nav a'));
  var tocList = doc.getElementById('toc-list');
  var tocTitle = doc.getElementById('toc-title');
  var current = null;
  function setSection(sec) {
    if (sec === current) return;
    current = sec;
    sideLinks.forEach(function (a) {
      var on = a.getAttribute('href') === '#' + sec.id;
      a.classList.toggle('on', on);
      if (on) a.setAttribute('aria-current', 'location'); else a.removeAttribute('aria-current');
    });
    if (tocList) {
      tocTitle.textContent = sec._title;
      tocList.innerHTML = sec._h3.length
        ? sec._h3.map(function (h) { return '<a href="#' + h.id + '">' + esc(h.textContent.trim()) + '</a>'; }).join('')
        : '<span class="toc-none">No subsections</span>';
    }
  }
  var tocLinks = function () { return tocList ? Array.prototype.slice.call(tocList.querySelectorAll('a')) : []; };
  function onScroll() {
    var y = 180, best = sections[0];
    for (var i = 0; i < sections.length; i++) {
      if (sections[i].getBoundingClientRect().top - y <= 0) best = sections[i];
    }
    if ((window.innerHeight + window.scrollY) >= doc.documentElement.scrollHeight - 4) best = sections[sections.length - 1];
    setSection(best);
    var hs = best._h3, cur = null;
    for (var j = 0; j < hs.length; j++) if (hs[j].getBoundingClientRect().top - y <= 0) cur = hs[j];
    tocLinks().forEach(function (a) { a.classList.toggle('on', !!cur && a.getAttribute('href') === '#' + cur.id); });
  }
  var ticking = false;
  window.addEventListener('scroll', function () {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(function () { ticking = false; onScroll(); });
  }, { passive: true });
  onScroll();
})();
