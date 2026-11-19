/* Карта рынков «Ош Базар»: SVG-схема, плавный pan/zoom, статусы мест, поиск, карточка места.
 * Без внешних зависимостей. Данные: GET data-url (см. apps/trade_points/market_map.py). */
(function () {
  'use strict';

  var root = document.getElementById('mm');
  if (!root) return;

  var NS = 'http://www.w3.org/2000/svg';
  var ZERO_ID = [8, 4, 4, 4, 12].map(function (n) { return '0'.repeat(n); }).join('-');
  var REDUCED = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var nf = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
  var nf1 = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });
  var nf2 = new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  var STATUS_ORDER = ['paid', 'debt', 'missing', 'pending', 'inactive', 'free'];
  var STATUS_HINT = {
    paid: 'Нет долга, показание за период принято',
    debt: 'Есть неоплаченные начисления',
    missing: 'Показание за текущий период не передано',
    pending: 'Показание передано и ждёт проверки',
    inactive: 'Точка деактивирована',
    free: 'Место без торговой точки'
  };

  var $ = function (id) { return document.getElementById(id); };
  var svg = $('mm-svg');
  var vp = $('mm-vp');
  var stage = $('mm-stage');
  var tooltip = $('mm-tooltip');
  var drawer = $('mm-drawer');
  var drawerBody = $('mm-drawer-body');
  var stateEl = $('mm-state');
  var searchInput = $('mm-search');
  var suggest = $('mm-suggest');

  var S = {
    data: null,
    labels: {},
    markets: {},          // slug -> market (+ bounds)
    points: {},           // id -> point
    bySlot: {},           // "slug|slot" -> point
    stallEls: {},         // "slug|slot" -> rect
    market: 'all',
    off: new Set(),       // выключенные статусы
    selected: null,       // {slug, slot, point}
    view: { k: 1, x: 0, y: 0 },
    anim: null,
    fitK: 1
  };

  // ------------------------------------------------------------------ utils
  function el(tag, attrs, parent) {
    var node = document.createElementNS(NS, tag);
    if (attrs) for (var key in attrs) if (attrs[key] !== undefined && attrs[key] !== null) node.setAttribute(key, attrs[key]);
    if (parent) parent.appendChild(node);
    return node;
  }
  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function money(value, cur) {
    var n = Number(value || 0);
    return (Math.abs(n) >= 1000 || n % 1 === 0 ? nf.format(n) : nf2.format(n)) + ' ' + (cur || S.currency);
  }
  function pts(list) { return list.map(function (p) { return p[0] + ',' + p[1]; }).join(' '); }
  function norm(s) { return String(s || '').toLowerCase().replace(/ё/g, 'е').replace(/[\s\-_.()+]/g, ''); }
  function initials(name) {
    var parts = String(name || '').trim().split(/\s+/);
    return ((parts[0] || '')[0] || '') + ((parts[1] || '')[0] || '');
  }
  function plural(n, one, few, many) {
    var m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return one;
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
    return many;
  }
  function rot(x, y, deg) {
    var a = deg * Math.PI / 180;
    return [x * Math.cos(a) - y * Math.sin(a), x * Math.sin(a) + y * Math.cos(a)];
  }
  function statusOf(slug, slot) {
    var p = S.bySlot[slug + '|' + slot];
    return p ? p.status : 'free';
  }

  // ------------------------------------------------------------------ data
  function load(isRefresh) {
    var btn = $('mm-refresh');
    btn.classList.add('is-busy');
    if (!isRefresh) showState('loading');
    fetch(root.dataset.dataUrl, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) {
        if (!r.ok) throw new Error(r.status === 403 ? 'Нет доступа к карте' : 'Сервер ответил ' + r.status);
        return r.json();
      })
      .then(function (data) {
        ingest(data);
        if (!isRefresh) render(); else repaint();
        renderKpis();
        renderLegend();
        if (!data.markets.length) { showState('empty'); return; }
        hideState();
        if (!isRefresh) { resize(); applyHash(true); }
        if (S.selected && S.selected.point) openPoint(S.points[S.selected.point.id] || S.selected.point, { fly: false });
        var t = new Date(data.generated_at);
        $('mm-updated').textContent = 'Обновлено в ' + t.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
      })
      .catch(function (err) { showState('error', err.message); })
      .then(function () { btn.classList.remove('is-busy'); root.classList.remove('is-loading'); });
  }

  function ingest(data) {
    S.data = data;
    S.currency = data.currency === 'KGS' ? 'сом' : data.currency;
    S.labels = {};
    data.statuses.forEach(function (s) { S.labels[s.key] = s.label; });
    S.markets = {}; S.points = {}; S.bySlot = {};
    data.markets.forEach(function (m) {
      m.bounds = marketBounds(m);
      S.markets[m.slug] = m;
    });
    data.points.forEach(function (p) {
      S.points[p.id] = p;
      if (p.market && p.slot) S.bySlot[p.market + '|' + p.slot] = p;
      p._search = norm([p.code, p.name, p.block, p.place, p.meter].concat(
        p.tenants.map(function (t) { return t.name + ' ' + t.phone; })).join(' '));
    });
  }

  function marketBounds(m) {
    var o = m.layout.offset, xs = [], ys = [];
    (m.layout.outline || []).forEach(function (p) { xs.push(o[0] + p[0]); ys.push(o[1] + p[1]); });
    if (!xs.length) m.blocks.forEach(function (b) { xs.push(o[0] + b.x, o[0] + b.x + b.w); ys.push(o[1] + b.y, o[1] + b.y + b.h); });
    if (!xs.length) return S.data.bounds;
    return [Math.min.apply(null, xs), Math.min.apply(null, ys), Math.max.apply(null, xs), Math.max.apply(null, ys)];
  }

  // ------------------------------------------------------------------ render
  function render() {
    while (vp.firstChild) vp.removeChild(vp.firstChild);
    S.stallEls = {};
    S.mlabels = [];
    var b = S.data.bounds, pad = 6000;
    el('rect', { x: b[0] - pad, y: b[1] - pad, width: b[2] - b[0] + pad * 2, height: b[3] - b[1] + pad * 2, fill: '#f3f6fa' }, vp);
    el('rect', { x: b[0] - pad, y: b[1] - pad, width: b[2] - b[0] + pad * 2, height: b[3] - b[1] + pad * 2, fill: 'url(#mm-grid-l)' }, vp);

    var roads = el('g', { 'class': 'mm-roads' }, vp);
    var lots = el('g', { 'class': 'mm-lots' }, vp);
    S.data.markets.forEach(function (m) { drawRoads(m, roads); });
    S.data.markets.forEach(function (m) { drawMarket(m, lots); });
    S.ping = el('g', { 'class': 'mm-pings' }, vp);
    applyFilters();
  }

  function drawRoads(m, parent) {
    var L = m.layout, g = el('g', { transform: 'translate(' + L.offset[0] + ' ' + L.offset[1] + ')' }, parent);
    if (L.outline && L.outline.length > 2) {
      el('polygon', { points: pts(L.outline), 'class': 'mm-road-edge', 'stroke-width': 17.5 }, g);
      el('polygon', { points: pts(L.outline), 'class': 'mm-road', 'stroke-width': 16 }, g);
    }
    (L.roads || []).forEach(function (r) {
      var w = r.width || 10;
      el('polyline', { points: pts(r.points), 'class': 'mm-road-edge', 'stroke-width': w + 1.5 }, g);
      el('polyline', { points: pts(r.points), 'class': 'mm-road', 'stroke-width': w }, g);
      el('polyline', { points: pts(r.points), 'class': 'mm-road-mark lod-mid' }, g);
    });
  }

  function drawMarket(m, parent) {
    var L = m.layout;
    var g = el('g', { 'class': 'mm-market', 'data-market': m.slug, transform: 'translate(' + L.offset[0] + ' ' + L.offset[1] + ')', style: '--c:' + m.color }, parent);
    m.el = g;
    if (L.outline && L.outline.length > 2) {
      el('polygon', { points: pts(L.outline), 'class': 'mm-lot', filter: 'url(#mm-lot-shadow)' }, g);
      el('polygon', { points: pts(L.outline), 'class': 'mm-lot-tint' }, g);
    }
    (L.zones || []).forEach(function (z) { drawZone(z, g); });

    m.blocks.forEach(function (blk) {
      var bg = el('g', { transform: 'translate(' + blk.x + ' ' + blk.y + ')' + (blk.rotate ? ' rotate(' + blk.rotate + ')' : '') }, g);
      el('rect', { x: -1.2, y: -1.2, width: blk.w + 2.4, height: blk.h + 2.4, rx: 1.4, 'class': 'mm-plate ' + (blk.tone || 'neutral') }, bg);
      var label = el('text', { x: 0, y: -2.2, 'class': 'mm-block-label lod-mid' }, bg);
      label.textContent = blk.name;
      var sw = blk.slot[0], sh = blk.slot[1];
      blk.slots.forEach(function (s) {
        var key = blk.id + '-' + s[0];
        var rect = el('rect', { x: s[1], y: s[2], width: sw, height: sh, rx: Math.min(sw, sh) * 0.14, 'class': 'mm-stall', 'data-k': m.slug + '|' + key }, bg);
        S.stallEls[m.slug + '|' + key] = rect;
        var p = S.bySlot[m.slug + '|' + key];
        if (p) {
          var t = el('text', { x: s[1] + sw / 2, y: s[2] + sh / 2, 'class': 'mm-stall-label lod-deep' }, bg);
          t.textContent = p.code;
          if (sh > sw * 1.05) t.setAttribute('transform', 'rotate(-90 ' + (s[1] + sw / 2) + ' ' + (s[2] + sh / 2) + ')');
        }
      });
    });

    (L.labels || []).forEach(function (lb) {
      var t = el('text', { x: lb.at[0], y: lb.at[1], 'class': 'mm-zone-label lod-mid', style: lb.size ? 'font-size:' + lb.size + 'px' : null }, g);
      t.textContent = lb.text;
    });
    var ml = L.label || { text: m.name, at: [0, -8] };
    var mt = el('text', { x: ml.at[0], y: ml.at[1], 'class': 'mm-market-label lod-far', 'text-anchor': 'middle' }, g);
    S.mlabels.push(mt);
    var meta = [];
    if (m.area_m2) meta.push(nf.format(m.area_m2) + ' м²');
    if (m.parking_places) meta.push('парковка ' + nf.format(m.parking_places));
    var minY = Math.min.apply(null, (L.outline || [[0, 0]]).map(function (p) { return p[1]; }));
    var above = ml.at[1] < minY;  // подпись над участком — доп. строку выводим выше названия
    var main = document.createElementNS(NS, 'tspan');
    main.textContent = ml.text || m.name;
    if (meta.length && above) {
      var sub1 = el('tspan', { 'class': 'sub', x: ml.at[0], dy: '-1.6em' }, mt);
      sub1.textContent = meta.join(' · ');
      main.setAttribute('x', ml.at[0]);
      main.setAttribute('dy', '1.05em');
      mt.appendChild(main);
    } else {
      mt.appendChild(main);
      if (meta.length) {
        var sub = el('tspan', { 'class': 'sub', x: ml.at[0], dy: '1.35em' }, mt);
        sub.textContent = meta.join(' · ');
      }
    }
  }

  function zoneCenter(z) {
    if (z.rect) return [z.rect[0] + z.rect[2] / 2, z.rect[1] + z.rect[3] / 2];
    var xs = z.polygon.map(function (p) { return p[0]; }), ys = z.polygon.map(function (p) { return p[1]; });
    return [(Math.min.apply(null, xs) + Math.max.apply(null, xs)) / 2, (Math.min.apply(null, ys) + Math.max.apply(null, ys)) / 2];
  }

  function shape(z, cls, parent, dx, dy) {
    if (z.rect) {
      return el('rect', { x: z.rect[0] + (dx || 0), y: z.rect[1] + (dy || 0), width: z.rect[2], height: z.rect[3], rx: 0.8, 'class': cls }, parent);
    }
    var poly = z.polygon.map(function (p) { return [p[0] + (dx || 0), p[1] + (dy || 0)]; });
    return el('polygon', { points: pts(poly), 'class': cls }, parent);
  }

  function drawZone(z, parent) {
    if (!z || (!z.rect && !z.polygon)) return;
    var c = zoneCenter(z);
    var g = el('g', { 'class': 'mm-zone', transform: z.rotate ? 'rotate(' + z.rotate + ' ' + c[0] + ' ' + c[1] + ')' : null }, parent);
    var warm = z.tone === 'warm' ? ' warm' : '';
    var depth = 0.9 + 0.7 * (z.levels || 1);
    if (z.kind === 'building') {
      shape(z, 'mm-bld-wall' + warm, g);
      shape(z, 'mm-bld-roof' + warm, g, -depth * 0.35, -depth);
    } else if (z.kind === 'hall') {
      shape(z, 'mm-bld-wall', g);
      shape(z, 'mm-hall-roof', g, -depth * 0.35, -depth);
      if (z.rect) {
        var r = z.rect, y = r[1] + r[3] / 2 - depth;
        el('line', { x1: r[0] - depth * 0.35 + 1, y1: y, x2: r[0] + r[2] - depth * 0.35 - 1, y2: y, 'class': 'mm-hall-ridge' }, g);
      }
      c = [c[0] - depth * 0.35, c[1] - depth];
    } else if (z.kind === 'parking') {
      shape(z, 'mm-parking', g);
      el('rect', { x: c[0] - 2.6, y: c[1] - 7.6, width: 5.2, height: 5.2, rx: 1.3, 'class': 'mm-parking-badge' }, g);
      var p = el('text', { x: c[0], y: c[1] - 5, 'class': 'mm-parking-badge-text' }, g);
      p.textContent = 'P';
    } else if (z.kind === 'green') {
      shape(z, 'mm-green', g);
    } else if (z.kind === 'plaza') {
      shape(z, 'mm-plaza', g);
    } else {
      shape(z, 'mm-service', g);
    }
    var name = z.name || '';
    if (name) {
      var t = el('text', { x: c[0], y: c[1] + (z.kind === 'parking' ? 1 : 0), 'class': 'mm-zone-label lod-mid' }, g);
      t.textContent = name;
      if (z.kind === 'parking' && z.places) {
        var s = el('text', { x: c[0], y: c[1] + 4.4, 'class': 'mm-zone-label sub lod-mid' }, g);
        s.textContent = nf.format(z.places) + ' ' + plural(z.places, 'место', 'места', 'мест');
      }
    }
  }

  function stallClass(slug, slot) {
    var p = S.bySlot[slug + '|' + slot];
    var st = p ? p.status : 'free';
    var cls = 'mm-stall s-' + st;
    if (st === 'debt') cls += ' l' + Math.max(1, p.debt_level || 1);
    if (S.off.has(st)) cls += ' is-dim';
    if (S.selected && S.selected.slug === slug && S.selected.slot === slot) cls += ' is-selected';
    return cls;
  }

  function applyFilters() {
    for (var key in S.stallEls) {
      var i = key.indexOf('|');
      S.stallEls[key].setAttribute('class', stallClass(key.slice(0, i), key.slice(i + 1)));
    }
    S.data.markets.forEach(function (m) {
      if (m.el) m.el.classList.toggle('is-muted', S.market !== 'all' && S.market !== m.slug);
    });
  }

  function repaint() {
    // Новые данные при той же геометрии: пересобираем схему, сохраняя камеру.
    var view = Object.assign({}, S.view);
    render();
    S.view = view;
    applyView();
  }

  // ------------------------------------------------------------------ KPI
  function renderKpis() {
    var box = $('mm-kpis'), d = S.data, html = '';
    var cards = [{ slug: 'all', name: 'Все рынки', color: '#334155', short_code: String(d.markets.length), meta: metaAll() }]
      .concat(d.markets.map(function (m) {
        var meta = [];
        if (m.area_m2) meta.push(nf.format(m.area_m2) + ' м²');
        if (m.parking_places) meta.push('парковка ' + nf.format(m.parking_places));
        return { slug: m.slug, name: m.name, color: m.color, short_code: m.short_code, meta: meta.join(' · ') || 'Схема рынка' };
      }));
    cards.forEach(function (c) {
      var s = d.summary[c.slug] || {};
      var debt = Number(s.debt_total || 0);
      html +=
        '<button type="button" class="mm-kpi' + (S.market === c.slug ? ' is-active' : '') + '" data-market="' + esc(c.slug) + '" style="--c:' + esc(c.color) + '" aria-pressed="' + (S.market === c.slug) + '">' +
          '<div class="mm-kpi__head"><span class="mm-dot" aria-hidden="true"></span><span class="mm-kpi__name">' + esc(c.name) + '</span>' +
            (c.short_code ? '<span class="mm-kpi__code">' + esc(c.short_code) + '</span>' : '') + '</div>' +
          '<div class="mm-kpi__meta">' + esc(c.meta) + '</div>' +
          '<div class="mm-kpi__label">Задолженность</div>' +
          '<div class="mm-kpi__big' + (debt > 0 ? ' is-debt' : '') + '"><b>' + nf.format(debt) + '</b><span>' + esc(S.currency) + '</span></div>' +
          '<div class="mm-kpi__stats">' +
            '<span><b>' + nf.format(s.points || 0) + '</b>' + plural(s.points || 0, 'точка', 'точки', 'точек') + '</span>' +
            '<span class="' + (s.debtors ? 'is-warn' : '') + '"><b>' + nf.format(s.debtors || 0) + '</b>' + plural(s.debtors || 0, 'должник', 'должника', 'должников') + '</span>' +
            '<span title="Показания ждут проверки"><b>' + nf.format(s.pending || 0) + '</b>на проверке</span>' +
          '</div>' +
          '<div class="mm-progress">' +
            '<div class="mm-progress__row"><span>Показания · ' + esc(d.period.title) + '</span><b>' + nf1.format(s.readings_pct || 0) + '%</b></div>' +
            '<div class="mm-progress__track"><div class="mm-progress__fill" data-w="' + (s.readings_pct || 0) + '"></div></div>' +
            '<div class="mm-progress__row"><span>Занято мест</span><b>' + nf.format(s.placed || 0) + ' из ' + nf.format(s.slots || 0) + '</b></div>' +
          '</div>' +
        '</button>';
    });
    box.innerHTML = html;
    requestAnimationFrame(function () {
      box.querySelectorAll('.mm-progress__fill').forEach(function (f) { f.style.width = Math.min(100, f.dataset.w) + '%'; });
    });
  }
  function metaAll() {
    var area = S.data.markets.reduce(function (a, m) { return a + (m.area_m2 || 0); }, 0);
    return (area ? nf.format(area) + ' м² · ' : '') + S.data.markets.length + ' ' + plural(S.data.markets.length, 'рынок', 'рынка', 'рынков');
  }

  // ------------------------------------------------------------------ legend
  function renderLegend() {
    var scope = S.data.summary[S.market] || S.data.summary.all;
    var counts = scope ? scope.statuses : {};
    var list = $('mm-legend-list');
    list.innerHTML = STATUS_ORDER.map(function (key) {
      return '<li><button type="button" class="mm-legend__item' + (S.off.has(key) ? ' is-off' : '') + '" data-status="' + key + '" aria-pressed="' + !S.off.has(key) + '" title="' + esc(STATUS_HINT[key]) + '">' +
        '<span class="mm-swatch s-' + key + '" aria-hidden="true"></span><span>' + esc(S.labels[key] || key) + '</span><b>' + nf.format(counts[key] || 0) + '</b></button></li>';
    }).join('');
    $('mm-legend-reset').hidden = S.off.size === 0;
    var th = S.data.debt_thresholds || [];
    var debtBox = $('mm-legend-debt');
    debtBox.hidden = !th.length;
    if (th.length) {
      $('mm-legend-ticks').innerHTML = '<span>меньше</span><span>' + th.map(function (t) { return nf.format(Number(t)); }).join(' · ') + '</span><span>больше</span>';
      debtBox.title = 'Интенсивность долга по квартилям, ' + S.currency;
    }
    var un = $('mm-unplaced');
    if (S.data.unplaced) {
      un.hidden = false;
      un.innerHTML = '<b>' + nf.format(S.data.unplaced) + '</b> ' + plural(S.data.unplaced, 'точка', 'точки', 'точек') + ' без места на схеме · <code>manage.py place_trade_points</code>';
    } else un.hidden = true;
  }

  // ------------------------------------------------------------------ camera
  function size() { return [stage.clientWidth, stage.clientHeight]; }

  function applyView() {
    var v = S.view;
    vp.setAttribute('transform', 'translate(' + v.x.toFixed(2) + ' ' + v.y.toFixed(2) + ') scale(' + v.k.toFixed(4) + ')');
    var inv = (1 / v.k).toFixed(5);
    (S.mlabels || []).forEach(function (n) { n.style.setProperty('--inv', inv); });
    var z = v.k < 2.6 ? 'z-far' : v.k < 7 ? 'z-mid' : v.k < 16 ? 'z-near' : 'z-deep';
    if (svg.dataset.z !== z) {
      svg.classList.remove('z-far', 'z-mid', 'z-near', 'z-deep');
      svg.classList.add(z);
      svg.dataset.z = z;
    }
    updateScale();
  }

  function updateScale() {
    var steps = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000];
    var target = 110 / S.view.k, best = steps[0];
    steps.forEach(function (s) { if (s <= target) best = s; });
    $('mm-scale-bar').style.width = Math.round(best * S.view.k) + 'px';
    $('mm-scale-label').textContent = best >= 1000 ? best / 1000 + ' км' : best + ' м';
  }

  function viewFor(bounds, padding) {
    var sz = size(), pad = padding == null ? 56 : padding;
    var leftPad = sz[0] > 900 ? 250 : pad; // место под легенду
    var topPad = sz[0] <= 768 ? 64 : pad;
    var rightPad = root.classList.contains('has-drawer') && sz[0] > 768 ? 410 : pad;
    var w = Math.max(bounds[2] - bounds[0], 1), h = Math.max(bounds[3] - bounds[1], 1);
    var aw = Math.max(sz[0] - leftPad - rightPad, 120), ah = Math.max(sz[1] - pad - topPad, 120);
    var k = Math.min(aw / w, ah / h);
    return { k: k, x: leftPad + (aw - w * k) / 2 - bounds[0] * k, y: topPad + (ah - h * k) / 2 - bounds[1] * k };
  }

  function clampK(k) { return Math.max(S.fitK * 0.4, Math.min(k, 60)); }

  function animateTo(target, duration) {
    if (S.anim) cancelAnimationFrame(S.anim);
    duration = REDUCED ? 0 : (duration == null ? 700 : duration);
    var sz = size(), from = Object.assign({}, S.view);
    // Интерполяция центра в мировых координатах и масштаба в логарифмах — без «дёрганья».
    var c0 = [(sz[0] / 2 - from.x) / from.k, (sz[1] / 2 - from.y) / from.k];
    var c1 = [(sz[0] / 2 - target.x) / target.k, (sz[1] / 2 - target.y) / target.k];
    var l0 = Math.log(from.k), l1 = Math.log(target.k);
    var start = performance.now();
    function frame(now) {
      var t = duration ? Math.min(1, (now - start) / duration) : 1;
      var e = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
      var k = Math.exp(l0 + (l1 - l0) * e);
      var cx = c0[0] + (c1[0] - c0[0]) * e, cy = c0[1] + (c1[1] - c0[1]) * e;
      S.view = { k: k, x: sz[0] / 2 - cx * k, y: sz[1] / 2 - cy * k };
      applyView();
      if (t < 1) S.anim = requestAnimationFrame(frame); else S.anim = null;
    }
    S.anim = requestAnimationFrame(frame);
  }

  function zoomAt(px, py, factor, animate) {
    var v = S.view, k = clampK(v.k * factor), f = k / v.k;
    var target = { k: k, x: px - (px - v.x) * f, y: py - (py - v.y) * f };
    if (animate) animateTo(target, 260); else { S.view = target; applyView(); }
  }

  function fit(animate) {
    var b = S.market !== 'all' && S.markets[S.market] ? S.markets[S.market].bounds : S.data.bounds;
    var target = viewFor(b);
    if (S.market === 'all') S.fitK = target.k;
    if (animate) animateTo(target); else { S.view = target; applyView(); }
  }

  function resize() {
    var all = viewFor(S.data.bounds);
    S.fitK = all.k;
    fit(false);
  }

  // ------------------------------------------------------------------ pointer: pan / pinch / click
  var pointers = new Map(), drag = null, lastTap = 0;
  svg.addEventListener('pointerdown', function (e) {
    if (e.button !== 0 && e.pointerType === 'mouse') return;
    svg.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, [e.clientX, e.clientY]);
    if (S.anim) { cancelAnimationFrame(S.anim); S.anim = null; }
    drag = { moved: 0, start: [e.clientX, e.clientY], view: Object.assign({}, S.view), pinch: null, target: e.target, vel: [0, 0], t: performance.now(), last: [e.clientX, e.clientY] };
    if (pointers.size === 2) startPinch();
  });
  svg.addEventListener('pointermove', function (e) {
    if (!pointers.has(e.pointerId)) { hover(e); return; }
    pointers.set(e.pointerId, [e.clientX, e.clientY]);
    if (!drag) return;
    if (pointers.size >= 2 && drag.pinch) { doPinch(); return; }
    var dx = e.clientX - drag.start[0], dy = e.clientY - drag.start[1];
    drag.moved = Math.max(drag.moved, Math.abs(dx) + Math.abs(dy));
    if (drag.moved > 3) {
      svg.classList.add('is-dragging');
      hideTooltip();
      var now = performance.now(), dt = Math.max(now - drag.t, 1);
      drag.vel = [(e.clientX - drag.last[0]) / dt, (e.clientY - drag.last[1]) / dt];
      drag.t = now; drag.last = [e.clientX, e.clientY];
      S.view = { k: drag.view.k, x: drag.view.x + dx, y: drag.view.y + dy };
      applyView();
    }
  });
  function endPointer(e) {
    if (!pointers.has(e.pointerId)) return;
    pointers.delete(e.pointerId);
    svg.classList.remove('is-dragging');
    if (!drag) return;
    if (drag.pinch) { if (pointers.size < 2) drag = null; return; }
    if (drag.moved <= 4 && e.type === 'pointerup') {
      var now = performance.now();
      if (now - lastTap < 300 && e.pointerType !== 'mouse') {
        var r = svg.getBoundingClientRect();
        zoomAt(e.clientX - r.left, e.clientY - r.top, 2, true);
      } else clickTarget(drag.target);
      lastTap = now;
    } else if (!REDUCED && performance.now() - drag.t < 80) {
      inertia(drag.vel);
    }
    drag = null;
  }
  svg.addEventListener('pointerup', endPointer);
  svg.addEventListener('pointercancel', endPointer);
  svg.addEventListener('pointerleave', function (e) { if (!pointers.size) hideTooltip(); });
  svg.addEventListener('dblclick', function (e) {
    e.preventDefault();
    var r = svg.getBoundingClientRect();
    zoomAt(e.clientX - r.left, e.clientY - r.top, e.shiftKey ? 0.5 : 2, true);
  });

  function inertia(vel) {
    var vx = vel[0] * 16, vy = vel[1] * 16;
    function step() {
      vx *= 0.92; vy *= 0.92;
      if (Math.abs(vx) + Math.abs(vy) < 0.3) { S.anim = null; return; }
      S.view = { k: S.view.k, x: S.view.x + vx, y: S.view.y + vy };
      applyView();
      S.anim = requestAnimationFrame(step);
    }
    S.anim = requestAnimationFrame(step);
  }

  function pinchState() {
    var p = Array.from(pointers.values()), r = svg.getBoundingClientRect();
    return { d: Math.hypot(p[0][0] - p[1][0], p[0][1] - p[1][1]) || 1, c: [(p[0][0] + p[1][0]) / 2 - r.left, (p[0][1] + p[1][1]) / 2 - r.top] };
  }
  function startPinch() { drag.pinch = pinchState(); drag.view = Object.assign({}, S.view); drag.moved = 99; }
  function doPinch() {
    var now = pinchState(), st = drag.pinch, v0 = drag.view;
    var k = clampK(v0.k * now.d / st.d), f = k / v0.k;
    S.view = { k: k, x: now.c[0] - (st.c[0] - v0.x) * f, y: now.c[1] - (st.c[1] - v0.y) * f };
    applyView();
  }

  var wheelAcc = 0, wheelPt = null, wheelRaf = null;
  svg.addEventListener('wheel', function (e) {
    e.preventDefault();
    if (S.anim) { cancelAnimationFrame(S.anim); S.anim = null; }
    var r = svg.getBoundingClientRect();
    var delta = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
    if (!e.ctrlKey && Math.abs(e.deltaX) > Math.abs(e.deltaY) * 1.5) {
      S.view = { k: S.view.k, x: S.view.x - e.deltaX, y: S.view.y }; applyView(); return;
    }
    wheelAcc += -delta * (e.ctrlKey ? 0.01 : 0.0016);
    wheelPt = [e.clientX - r.left, e.clientY - r.top];
    if (!wheelRaf) wheelRaf = requestAnimationFrame(function () {
      wheelRaf = null;
      zoomAt(wheelPt[0], wheelPt[1], Math.exp(Math.max(-1, Math.min(1, wheelAcc))));
      wheelAcc = 0;
    });
  }, { passive: false });

  $('mm-zoom-in').addEventListener('click', function () { var s = size(); zoomAt(s[0] / 2, s[1] / 2, 1.6, true); });
  $('mm-zoom-out').addEventListener('click', function () { var s = size(); zoomAt(s[0] / 2, s[1] / 2, 1 / 1.6, true); });
  $('mm-fit').addEventListener('click', function () { fit(true); });

  svg.addEventListener('keydown', function (e) {
    var s = size(), step = 80;
    var map = { '+': function () { zoomAt(s[0] / 2, s[1] / 2, 1.4, true); }, '=': function () { zoomAt(s[0] / 2, s[1] / 2, 1.4, true); },
      '-': function () { zoomAt(s[0] / 2, s[1] / 2, 1 / 1.4, true); }, '0': function () { fit(true); },
      ArrowLeft: function () { pan(step, 0); }, ArrowRight: function () { pan(-step, 0); },
      ArrowUp: function () { pan(0, step); }, ArrowDown: function () { pan(0, -step); } };
    if (map[e.key]) { e.preventDefault(); map[e.key](); }
  });
  function pan(dx, dy) { animateTo({ k: S.view.k, x: S.view.x + dx, y: S.view.y + dy }, 220); }

  var resizeTimer;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { if (S.data) { S.fitK = viewFor(S.data.bounds).k; } }, 120);
  });

  // ------------------------------------------------------------------ hover / tooltip
  var hovered = null;
  function hover(e) {
    var t = e.target;
    if (!t.classList || !t.classList.contains('mm-stall')) { hideTooltip(); return; }
    var key = t.getAttribute('data-k');
    if (hovered !== key) {
      hovered = key;
      tooltip.innerHTML = tooltipHtml(key);
      tooltip.hidden = false;
    }
    var r = stage.getBoundingClientRect(), x = e.clientX - r.left + 16, y = e.clientY - r.top + 16;
    var w = tooltip.offsetWidth, h = tooltip.offsetHeight;
    if (x + w > r.width - 8) x = e.clientX - r.left - w - 14;
    if (y + h > r.height - 8) y = e.clientY - r.top - h - 14;
    tooltip.style.transform = 'translate3d(' + Math.round(x) + 'px,' + Math.round(y) + 'px,0)';
  }
  function hideTooltip() { hovered = null; tooltip.hidden = true; }

  function tooltipHtml(key) {
    var i = key.indexOf('|'), slug = key.slice(0, i), slot = key.slice(i + 1);
    var p = S.bySlot[key], m = S.markets[slug];
    if (!p) {
      return '<div class="mm-pill s-free">' + esc(S.labels.free) + '</div><div class="mm-tooltip__code">Место ' + esc(slot) + '</div>' +
        '<div class="mm-tooltip__name">' + esc(m ? m.name : '') + '</div>';
    }
    var tenant = p.tenants[0];
    var html = '<div class="mm-pill s-' + p.status + '">' + esc(S.labels[p.status]) + '</div>' +
      '<div class="mm-tooltip__code">' + esc(p.code) + '</div><div class="mm-tooltip__name">' + esc(p.name) + '</div>';
    if (Number(p.debt) > 0) html += '<div class="mm-tooltip__row"><span>Долг</span><b>' + money(p.debt) + '</b></div>';
    if (tenant) html += '<div class="mm-tooltip__row"><span>Арендатор</span><b>' + esc(tenant.name) + '</b></div>';
    html += '<div class="mm-tooltip__row"><span>Место</span><b>' + esc(slot) + ' · ' + esc(m ? m.short_code || m.name : '') + '</b></div>';
    return html;
  }

  // ------------------------------------------------------------------ selection / drawer
  function clickTarget(t) {
    if (!t || !t.classList || !t.classList.contains('mm-stall')) {
      if (S.selected) closeDrawer();
      return;
    }
    var key = t.getAttribute('data-k'), i = key.indexOf('|');
    var slug = key.slice(0, i), slot = key.slice(i + 1), p = S.bySlot[key];
    if (p) openPoint(p, { fly: false }); else openFree(slug, slot);
  }

  function select(slug, slot, point) {
    var prev = S.selected;
    S.selected = { slug: slug, slot: slot, point: point || null };
    [prev, S.selected].forEach(function (sel) {
      if (sel && sel.slug && sel.slot && S.stallEls[sel.slug + '|' + sel.slot]) {
        S.stallEls[sel.slug + '|' + sel.slot].setAttribute('class', stallClass(sel.slug, sel.slot));
      }
    });
    while (S.ping.firstChild) S.ping.removeChild(S.ping.firstChild);
    if (slug && slot) {
      var c = slotCenter(slug, slot);
      if (c) {
        el('circle', { cx: c[0], cy: c[1], r: 3.2, 'class': 'mm-ping a' }, S.ping);
        el('circle', { cx: c[0], cy: c[1], r: 2.4, 'class': 'mm-ping' }, S.ping);
      }
    }
  }

  function slotCenter(slug, slot) {
    var m = S.markets[slug];
    if (!m) return null;
    var dash = slot.lastIndexOf('-'), bid = slot.slice(0, dash), n = Number(slot.slice(dash + 1));
    for (var i = 0; i < m.blocks.length; i++) {
      var b = m.blocks[i];
      if (b.id !== bid) continue;
      for (var j = 0; j < b.slots.length; j++) {
        if (b.slots[j][0] === n) {
          var lx = b.slots[j][1] + b.slot[0] / 2, ly = b.slots[j][2] + b.slot[1] / 2;
          var r = rot(lx, ly, b.rotate || 0);
          return [m.layout.offset[0] + b.x + r[0], m.layout.offset[1] + b.y + r[1]];
        }
      }
    }
    return null;
  }

  function flyToSlot(slug, slot) {
    var c = slotCenter(slug, slot);
    if (!c) return;
    var sz = size(), k = Math.max(S.view.k, 12);
    var right = root.classList.contains('has-drawer') && sz[0] > 768 ? 410 : 0;
    var cx = (sz[0] - right) / 2, cy = sz[0] > 768 ? sz[1] / 2 : sz[1] * 0.22;
    animateTo({ k: k, x: cx - c[0] * k, y: cy - c[1] * k }, 900);
  }

  function openDrawer() {
    root.classList.add('has-drawer');
    drawer.classList.add('is-open');
    drawer.setAttribute('aria-hidden', 'false');
  }
  function closeDrawer() {
    root.classList.remove('has-drawer');
    drawer.classList.remove('is-open');
    drawer.setAttribute('aria-hidden', 'true');
    select(null, null, null);
    if (location.hash.indexOf('p=') !== -1) history.replaceState(null, '', location.pathname + location.search + (S.market !== 'all' ? '#' + S.market : ''));
  }
  $('mm-drawer-close').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && drawer.classList.contains('is-open') && document.activeElement !== searchInput) closeDrawer();
    if (e.key === '/' && !/input|textarea|select/i.test(document.activeElement.tagName)) { e.preventDefault(); searchInput.focus(); }
  });

  function openFree(slug, slot) {
    var m = S.markets[slug];
    select(slug, slot, null);
    openDrawer();
    var add = root.dataset.addUrl + '?market=' + encodeURIComponent(m.id) + '&map_slot=' + encodeURIComponent(slot);
    drawerBody.innerHTML =
      '<div class="mm-d-head"><div class="mm-d-crumbs" style="--c:' + esc(m.color) + '"><span class="mm-dot"></span>' + esc(m.name) + ' · ' + esc(blockName(m, slot)) + '</div>' +
      '<h3 class="mm-d-code">Место ' + esc(slot) + '</h3><p class="mm-d-name">Торговая точка не назначена</p>' +
      '<span class="mm-pill s-free">' + esc(S.labels.free) + '</span></div>' +
      '<div class="mm-d-sec"><div class="mm-d-empty">Чтобы разместить точку здесь, откройте её карточку и в разделе «На карте» укажите рынок «' + esc(m.name) +
      '» и место <code>' + esc(slot) + '</code>.</div></div>' +
      '<div class="mm-d-actions"><a class="is-primary" href="' + esc(add) + '">Создать точку на этом месте</a></div>';
  }

  function blockName(m, slot) {
    var bid = slot.slice(0, slot.lastIndexOf('-'));
    var b = m.blocks.filter(function (x) { return x.id === bid; })[0];
    return b ? b.name : bid;
  }

  function openPoint(p, opts) {
    opts = opts || {};
    select(p.market, p.slot, p);
    openDrawer();
    if (opts.fly !== false && p.slot) flyToSlot(p.market, p.slot);
    var m = p.market ? S.markets[p.market] : null;
    drawerBody.innerHTML = headHtml(p, m) +
      '<div class="mm-d-skel"><span class="mm-sk" style="height:62px"></span><span class="mm-sk" style="height:110px"></span><span class="mm-sk" style="height:84px"></span></div>';
    var hash = '#p=' + encodeURIComponent(p.code);
    if (location.hash !== hash) history.replaceState(null, '', hash);
    var url = S.data.urls.point_detail.replace(ZERO_ID, p.id);
    var token = (S.reqToken = (S.reqToken || 0) + 1);
    fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) { if (!r.ok) throw new Error('Ошибка ' + r.status); return r.json(); })
      .then(function (d) { if (token === S.reqToken) drawerBody.innerHTML = headHtml(p, m, d) + detailHtml(d); })
      .catch(function (err) {
        if (token !== S.reqToken) return;
        drawerBody.innerHTML = headHtml(p, m) + '<div class="mm-d-empty">Не удалось загрузить данные: ' + esc(err.message) + '</div>';
      });
  }

  function headHtml(p, m, d) {
    var status = d ? d.status : p.status;
    var crumbs = m ? '<span class="mm-dot"></span>' + esc(m.name) + (p.slot ? ' · ' + esc(blockName(m, p.slot)) + ' · место ' + esc(p.slot) : '') : 'Не размещена на схеме';
    return '<div class="mm-d-head"><div class="mm-d-crumbs" style="--c:' + esc(m ? m.color : '#64748b') + '">' + crumbs + '</div>' +
      '<h3 class="mm-d-code">' + esc(p.code) + '</h3><p class="mm-d-name">' + esc(p.name) + '</p>' +
      '<span class="mm-pill s-' + status + '" title="' + esc(STATUS_HINT[status]) + '">' + esc(S.labels[status]) + '</span></div>';
  }

  function detailHtml(d) {
    var debt = Number(d.debt), credit = Number(d.credit), html = '';
    html += '<div class="mm-d-grid">' +
      '<div class="mm-d-stat' + (debt > 0 ? ' is-debt' : '') + '"><span>Долг</span><b>' + money(d.debt) + '</b></div>' +
      '<div class="mm-d-stat' + (credit > 0 ? ' is-credit' : '') + '"><span>Аванс</span><b>' + money(d.credit) + '</b></div>' +
      '<div class="mm-d-stat"><span>Неоплачено</span><b>' + d.unpaid_charges + ' ' + plural(d.unpaid_charges, 'счёт', 'счёта', 'счетов') + '</b></div></div>';

    var lr = d.last_reading;
    html += '<div class="mm-d-sec"><h4>Показания · ' + esc(d.period.title) + '</h4>';
    if (lr) {
      html += '<div class="mm-d-card"><dl class="mm-d-kv">' +
        '<dt>Последнее</dt><dd><a href="' + esc(lr.admin_url) + '">' + nf2.format(Number(lr.value)) + ' кВт⋅ч</a></dd>' +
        '<dt>Потребление</dt><dd>' + nf2.format(Number(lr.consumption)) + ' кВт⋅ч</dd>' +
        '<dt>Период</dt><dd>' + esc(lr.period) + '</dd>' +
        '<dt>Статус</dt><dd>' + esc(lr.status_label) + '</dd>' +
        '<dt>Передано</dt><dd>' + esc(lr.submitted_at) + '</dd></dl></div>';
    } else html += '<div class="mm-d-empty">Показаний ещё не было.</div>';
    if (d.current_charge) {
      var c = d.current_charge;
      html += '<div class="mm-d-card" style="margin-top:8px"><dl class="mm-d-kv">' +
        '<dt>Начислено за период</dt><dd>' + money(c.amount) + '</dd>' +
        '<dt>Оплачено</dt><dd>' + money(c.paid) + '</dd>' +
        '<dt>Статус</dt><dd>' + esc(c.status) + '</dd>' +
        (c.due_date ? '<dt>Срок оплаты</dt><dd>' + esc(new Date(c.due_date).toLocaleDateString('ru-RU')) + '</dd>' : '') +
        '</dl></div>';
    }
    html += '</div>';

    html += '<div class="mm-d-sec"><h4>Арендаторы</h4>';
    if (d.tenants.length) {
      html += '<ul class="mm-d-list">' + d.tenants.map(function (t) {
        return '<li class="mm-d-person"><span class="mm-avatar" aria-hidden="true">' + esc(initials(t.name).toUpperCase()) + '</span>' +
          '<div style="min-width:0"><div class="mm-d-person__name">' + esc(t.name) + '</div>' +
          '<div class="mm-d-person__sub">' + esc(t.role) + ' · <a href="tel:' + esc(t.phone) + '">' + esc(t.phone) + '</a></div></div>' +
          '<a href="' + esc(t.admin_url) + '">Профиль</a></li>';
      }).join('') + '</ul>';
    } else html += '<div class="mm-d-empty">Арендатор не привязан.</div>';
    html += '</div>';

    html += '<div class="mm-d-sec"><h4>Счётчики</h4>';
    if (d.meters.length) {
      html += '<ul class="mm-d-list">' + d.meters.map(function (m) {
        return '<li class="mm-d-meter"><a href="' + esc(m.admin_url) + '">' + esc(m.serial) + '</a>' +
          (m.model ? '<span style="color:var(--mm-muted)">' + esc(m.model) + '</span>' : '') +
          '<span style="margin-left:auto;display:flex;gap:4px">' + (m.is_primary ? '<span class="mm-badge">основной</span>' : '') +
          (m.is_active ? '' : '<span class="mm-badge is-off">снят</span>') + '</span></li>';
      }).join('') + '</ul>';
    } else html += '<div class="mm-d-empty">Счётчик не установлен.</div>';
    html += '</div>';

    html += '<div class="mm-d-actions">' +
      '<a class="is-primary" href="' + esc(d.links.change) + '">Открыть торговую точку</a>' +
      '<a href="' + esc(d.links.readings) + '">Показания</a><a href="' + esc(d.links.charges) + '">Начисления</a></div>';
    return html;
  }

  // ------------------------------------------------------------------ search
  var results = [], active = -1, searchTimer;
  searchInput.addEventListener('input', function () { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 90); });
  searchInput.addEventListener('focus', function () { if (searchInput.value.trim()) runSearch(); });
  searchInput.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
    else if (e.key === 'Enter') { e.preventDefault(); if (results.length) choose(results[Math.max(active, 0)]); }
    else if (e.key === 'Escape') { closeSuggest(); searchInput.blur(); }
  });
  document.addEventListener('pointerdown', function (e) { if (!e.target.closest || !e.target.closest('.mm-search')) closeSuggest(); });

  function runSearch() {
    if (!S.data) return;
    var q = norm(searchInput.value), raw = searchInput.value.trim();
    if (!q) { closeSuggest(); return; }
    var scored = [];
    S.data.points.forEach(function (p) {
      var idx = p._search.indexOf(q);
      if (idx === -1) return;
      var score = norm(p.code) === q ? 0 : norm(p.code).indexOf(q) === 0 ? 1 : idx === 0 ? 2 : 3;
      if (S.market !== 'all' && p.market !== S.market) score += 4;
      scored.push([score, p]);
    });
    scored.sort(function (a, b) { return a[0] - b[0] || a[1].code.localeCompare(b[1].code, 'ru'); });
    results = scored.slice(0, 10).map(function (x) { return x[1]; });
    active = results.length ? 0 : -1;
    if (!results.length) {
      suggest.innerHTML = '<li class="mm-suggest__empty">Ничего не найдено по запросу «' + esc(raw) + '»</li>';
    } else {
      suggest.innerHTML = results.map(function (p, i) {
        var m = p.market ? S.markets[p.market] : null, t = p.tenants[0];
        var sub = [t ? t.name + ' · ' + t.phone : 'без арендатора', S.labels[p.status]];
        if (Number(p.debt) > 0) sub.push('долг ' + money(p.debt));
        return '<li role="option" id="mm-opt-' + i + '" aria-selected="' + (i === active) + '" data-i="' + i + '">' +
          '<span class="mm-swatch s-' + p.status + '" style="width:10px;height:10px;border-radius:3px"></span>' +
          '<div style="min-width:0"><div class="mm-suggest__title">' + mark(p.code, raw) + '<small>' + mark(p.name, raw) + '</small></div>' +
          '<div class="mm-suggest__sub">' + esc(sub.join(' · ')) + '</div></div>' +
          (p.slot ? '<span class="mm-suggest__tag" style="--c:' + esc(m.color) + '">' + esc(m.short_code || m.name) + ' · ' + esc(p.slot) + '</span>'
                  : '<span class="mm-suggest__tag is-off">не на схеме</span>') + '</li>';
      }).join('');
    }
    suggest.hidden = false;
    searchInput.setAttribute('aria-expanded', 'true');
    searchInput.setAttribute('aria-activedescendant', active >= 0 ? 'mm-opt-' + active : '');
  }
  function mark(text, q) {
    var s = String(text || ''), i = s.toLowerCase().indexOf(q.toLowerCase());
    if (!q || i === -1) return esc(s);
    return esc(s.slice(0, i)) + '<mark>' + esc(s.slice(i, i + q.length)) + '</mark>' + esc(s.slice(i + q.length));
  }
  function move(d) {
    if (!results.length) return;
    active = (active + d + results.length) % results.length;
    suggest.querySelectorAll('li[role=option]').forEach(function (li, i) { li.setAttribute('aria-selected', i === active); });
    searchInput.setAttribute('aria-activedescendant', 'mm-opt-' + active);
    var cur = $('mm-opt-' + active);
    if (cur) cur.scrollIntoView({ block: 'nearest' });
  }
  suggest.addEventListener('pointerdown', function (e) {
    var li = e.target.closest('li[data-i]');
    if (li) { e.preventDefault(); choose(results[Number(li.dataset.i)]); }
  });
  function closeSuggest() { suggest.hidden = true; searchInput.setAttribute('aria-expanded', 'false'); }
  function choose(p) {
    if (!p) return;
    closeSuggest();
    searchInput.value = p.code;
    if (p.market && S.market !== 'all' && S.market !== p.market) setMarket('all', false);
    var r = stage.getBoundingClientRect();
    if (r.bottom > window.innerHeight || r.top < 0) stage.scrollIntoView({ behavior: REDUCED ? 'auto' : 'smooth', block: 'end' });
    openPoint(p, { fly: true });
  }

  // ------------------------------------------------------------------ market focus / status filter
  function setMarket(slug, animate) {
    S.market = S.markets[slug] ? slug : 'all';
    document.querySelectorAll('#mm-markets .mm-seg__btn').forEach(function (b) {
      var on = b.dataset.market === S.market;
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-selected', on);
    });
    document.querySelectorAll('#mm-kpis .mm-kpi').forEach(function (b) {
      var on = b.dataset.market === S.market;
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-pressed', on);
    });
    applyFilters();
    renderLegend();
    fit(animate !== false);
    if (location.hash.indexOf('p=') === -1) history.replaceState(null, '', S.market === 'all' ? location.pathname + location.search : '#' + S.market);
  }
  $('mm-markets').addEventListener('click', function (e) {
    var b = e.target.closest('[data-market]');
    if (b) setMarket(b.dataset.market);
  });
  $('mm-kpis').addEventListener('click', function (e) {
    var b = e.target.closest('[data-market]');
    if (b) setMarket(b.dataset.market);
  });
  $('mm-legend-list').addEventListener('click', function (e) {
    var b = e.target.closest('[data-status]');
    if (!b) return;
    var key = b.dataset.status;
    if (e.altKey || e.metaKey) { // «только этот статус»
      S.off = new Set(STATUS_ORDER.filter(function (s) { return s !== key; }));
    } else if (S.off.has(key)) S.off.delete(key); else S.off.add(key);
    if (S.off.size === STATUS_ORDER.length) S.off.clear();
    applyFilters();
    renderLegend();
  });
  $('mm-legend-reset').addEventListener('click', function () { S.off.clear(); applyFilters(); renderLegend(); });
  $('mm-refresh').addEventListener('click', function () { load(true); });

  function applyHash(initial) {
    var h = decodeURIComponent(location.hash.replace(/^#/, ''));
    if (h.indexOf('p=') === 0) {
      var code = h.slice(2), p = S.data.points.filter(function (x) { return x.code === code; })[0];
      if (p) { setMarket('all', false); openPoint(p, { fly: true }); return; }
    }
    if (S.markets[h]) { setMarket(h, !initial); return; }
    if (initial) fit(false);
  }
  window.addEventListener('hashchange', function () { if (S.data) applyHash(false); });

  // ------------------------------------------------------------------ states
  function showState(kind, message) {
    stateEl.classList.remove('is-hidden');
    var card;
    if (kind === 'loading') {
      card = '<div class="mm-spinner" aria-hidden="true"></div><div class="mm-state__title">Загружаем схему рынков…</div><div class="mm-state__text">Считаем долги и показания по каждой точке</div>';
    } else if (kind === 'empty') {
      card = '<div class="mm-state__icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2z"/><path d="M9 4v14M15 6v14"/></svg></div>' +
        '<div class="mm-state__title">Рынки ещё не созданы</div><div class="mm-state__text">Выполните <code>python manage.py seed_markets</code> или добавьте рынок со схемой в админке.</div>' +
        '<a class="mm-btn" href="' + esc(root.dataset.marketsUrl) + '">Перейти к рынкам</a>';
    } else {
      card = '<div class="mm-state__icon is-error" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg></div>' +
        '<div class="mm-state__title">Не удалось загрузить карту</div><div class="mm-state__text">' + esc(message || '') + '</div>' +
        '<button type="button" class="mm-btn" id="mm-retry">Повторить</button>';
    }
    stateEl.innerHTML = '<div class="mm-state__card" role="status">' + card + '</div>';
    var retry = $('mm-retry');
    if (retry) retry.addEventListener('click', function () { load(false); });
  }
  function hideState() { stateEl.classList.add('is-hidden'); }

  load(false);
})();
