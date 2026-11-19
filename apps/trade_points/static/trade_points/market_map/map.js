/* Карта рынков «Ош Базар»: схема на <canvas>, плавный pan/zoom, статусы мест, поиск, карточка места.
 * Без внешних зависимостей. Данные: GET data-url (см. apps/trade_points/market_map.py).
 *
 * Отрисовка (только по requestAnimationFrame, когда что-то изменилось):
 *   1. сетка-подложка — несколько линий в экранных координатах;
 *   2. статичная основа (дороги, участки, здания, парковки, подложки блоков) — заранее
 *      отрисована в offscreen-canvas двух разрешений и копируется drawImage; при сильном
 *      приближении рисуется векторно (это десятки фигур);
 *   3. места — Path2D на блок и на цвет (закэшированы, пересобираются только при новых данных),
 *      блоки вне экрана пропускаются, фильтр по статусу — прозрачностью группы;
 *   4. подписи — в экранных координатах, по уровню детализации; коды мест — только в кадре
 *      и только когда карта не движется.
 * Попадание курсора — по индексу блоков (с учётом поворота) и бинарному поиску по сетке. */
(function () {
  'use strict';

  var root = document.getElementById('mm');
  if (!root) return;

  var ZERO_ID = [8, 4, 4, 4, 12].map(function (n) { return '0'.repeat(n); }).join('-');
  var REDUCED = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var nf = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
  var nf1 = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });
  var nf2 = new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  var FONT = getComputedStyle(root).fontFamily || 'system-ui, sans-serif';
  var STATUS_ORDER = ['paid', 'debt', 'missing', 'pending', 'inactive', 'free'];
  var STATUS_HINT = {
    paid: 'Нет долга, показание за период принято',
    debt: 'Есть неоплаченные начисления',
    missing: 'Показание за текущий период не передано',
    pending: 'Показание передано и ждёт проверки',
    inactive: 'Точка деактивирована',
    free: 'Место без торговой точки'
  };
  var LOD_MID = 2.6, LOD_DEEP = 16;          // css px на метр
  var TIERS = [1.5, 4];                       // разрешения кэша основы, device px на метр
  var SETTLE_MS = 120;

  var $ = function (id) { return document.getElementById(id); };
  var canvas = $('mm-canvas');
  var ctx = canvas.getContext('2d');
  var stage = $('mm-stage');
  var tooltip = $('mm-tooltip');
  var drawer = $('mm-drawer');
  var drawerBody = $('mm-drawer-body');
  var stateEl = $('mm-state');
  var searchInput = $('mm-search');
  var suggest = $('mm-suggest');
  var pingEl = $('mm-ping');

  var css = getComputedStyle(root);
  function cssVar(name, fallback) { return (css.getPropertyValue(name) || '').trim() || fallback; }
  var COLORS = {
    paid: cssVar('--mm-paid', '#009e73'),
    d1: cssVar('--mm-debt-1', '#f4a582'), d2: cssVar('--mm-debt-2', '#e7743d'),
    d3: cssVar('--mm-debt-3', '#d55e00'), d4: cssVar('--mm-debt-4', '#9a3d06'),
    missing: cssVar('--mm-missing', '#f0d43a'),
    pending: cssVar('--mm-pending', '#56b4e9'),
    free: '#ffffff'
  };
  var FILL_KEYS = ['free', 'paid', 'missing', 'pending', 'inactive', 'd1', 'd2', 'd3', 'd4'];
  var PLATE = { neutral: ['#edf1f5', '#e1e7ef'], red: ['#f8ebe7', '#f0d9d1'], blue: ['#e8eff8', '#d6e2f1'] };

  var S = {
    data: null, labels: {}, markets: {}, points: {}, bySlot: {},
    market: 'all', off: new Set(), selected: null, hover: null,
    view: { k: 1, x: 0, y: 0 }, anim: null, fitK: 1,
    W: 0, H: 0, dpr: 1, moving: false, settleTimer: 0, raf: 0,
    tiers: {}, world: null, pathsVersion: 0
  };

  // ------------------------------------------------------------------ utils
  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function money(value, cur) {
    var n = Number(value || 0);
    return (Math.abs(n) >= 1000 || n % 1 === 0 ? nf.format(n) : nf2.format(n)) + ' ' + (cur || S.currency);
  }
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
  function fillKey(p) {
    if (!p) return 'free';
    if (p.status === 'debt') return 'd' + Math.min(4, Math.max(1, p.debt_level || 1));
    return p.status;
  }
  function statusOfKey(key) { return key.charAt(0) === 'd' && key.length === 2 ? 'debt' : key; }
  function hasRoundRect() { return typeof Path2D !== 'undefined' && typeof Path2D.prototype.roundRect === 'function'; }
  var ROUND = hasRoundRect();
  function rrect(path, x, y, w, h, r) { if (ROUND && r > 0) path.roundRect(x, y, w, h, r); else path.rect(x, y, w, h); }

  // ------------------------------------------------------------------ patterns (CanvasPattern)
  function tile(w, h, paint) {
    var c = document.createElement('canvas');
    c.width = w; c.height = h;
    paint(c.getContext('2d'), w, h);
    return c;
  }
  var PATTERNS = {};
  function makePattern(name, tileCanvas, unitsPerTile, rotateDeg) {
    var p = ctx.createPattern(tileCanvas, 'repeat');
    var m = new DOMMatrix();
    if (rotateDeg) m = m.rotate(rotateDeg);
    m = m.scale(unitsPerTile / tileCanvas.width);
    p.setTransform(m);
    PATTERNS[name] = p;
  }
  function initPatterns() {
    makePattern('inactive', tile(28, 28, function (c, w, h) {
      c.fillStyle = '#b8c2d0'; c.fillRect(0, 0, w, h);
      c.fillStyle = '#8592a6'; c.fillRect(0, 0, 11, h);
    }), 1.4, 45);
    makePattern('service', tile(48, 48, function (c, w, h) {
      c.fillStyle = '#e8ecf2'; c.fillRect(0, 0, w, h);
      c.fillStyle = '#d5dce6'; c.fillRect(0, 0, 14, h);
    }), 2.4, 45);
    makePattern('bays', tile(52, 120, function (c, w, h) {
      c.fillStyle = '#eef1f6'; c.fillRect(0, 0, w, h);
      c.fillStyle = '#d6dde8'; c.fillRect(0, 0, 4.4, 100);
    }), 2.6, 0);
  }

  // ------------------------------------------------------------------ data
  function load(isRefresh) {
    var btn = $('mm-refresh');
    btn.classList.add('is-busy');
    if (!isRefresh) showState('loading');
    var t0 = performance.now();
    fetch(root.dataset.dataUrl, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) {
        if (!r.ok) throw new Error(r.status === 403 ? 'Нет доступа к карте' : 'Сервер ответил ' + r.status);
        return r.json();
      })
      .then(function (data) {
        var t1 = performance.now();
        ingest(data);
        prepare();
        renderKpis();
        renderLegend();
        if (!data.markets.length) { showState('empty'); return; }
        hideState();
        if (!isRefresh) { resizeCanvas(); S.fitK = viewFor(S.data.bounds).k; applyHash(true); }
        drawNow();
        root.dataset.timing = JSON.stringify({ fetch: Math.round(t1 - t0), build: Math.round(performance.now() - t1) });
        scheduleTiers();
        if (S.selected && S.selected.point) {
          var fresh = S.points[S.selected.point.id];
          if (fresh) openPoint(fresh, { fly: false });
        }
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
    data.markets.forEach(function (m) { S.markets[m.slug] = m; });
    data.points.forEach(function (p) {
      S.points[p.id] = p;
      if (p.market && p.slot) S.bySlot[p.market + '|' + p.slot] = p;
      p._search = norm([p.code, p.name, p.block, p.place, p.meter].concat(
        p.tenants.map(function (t) { return t.name + ' ' + t.phone; })).join(' '));
    });
  }

  /* Геометрия: трансформации блоков, рамки для отсечения, индекс попаданий, словарь мест. */
  function prepare() {
    S.pathsVersion++;
    var all = [];
    S.data.markets.forEach(function (m) {
      var L = m.layout, ox = L.offset[0], oy = L.offset[1];
      m.ox = ox; m.oy = oy;
      m.slotIndex = {};
      m.outlineWorld = (L.outline || []).map(function (p) { return [ox + p[0], oy + p[1]]; });
      var xs = [], ys = [];
      m.outlineWorld.forEach(function (p) { xs.push(p[0]); ys.push(p[1]); });
      m.blocks.forEach(function (b) {
        var a = (b.rotate || 0) * Math.PI / 180;
        b.cos = Math.cos(a); b.sin = Math.sin(a);
        b.wx = ox + b.x; b.wy = oy + b.y;
        b.sw = b.slot[0]; b.sh = b.slot[1];
        var corners = [[-1.5, -4], [b.w + 1.5, -4], [b.w + 1.5, b.h + 1.5], [-1.5, b.h + 1.5]].map(function (c) {
          return [b.wx + c[0] * b.cos - c[1] * b.sin, b.wy + c[0] * b.sin + c[1] * b.cos];
        });
        b.bbox = [
          Math.min.apply(null, corners.map(function (c) { return c[0]; })),
          Math.min.apply(null, corners.map(function (c) { return c[1]; })),
          Math.max.apply(null, corners.map(function (c) { return c[0]; })),
          Math.max.apply(null, corners.map(function (c) { return c[1]; }))
        ];
        corners.forEach(function (c) { xs.push(c[0]); ys.push(c[1]); });
        // Индекс: уникальные x/y ячеек → бинарный поиск.
        var ux = {}, uy = {};
        b.slots.forEach(function (s) { ux[s[1]] = 1; uy[s[2]] = 1; });
        b.xs = Object.keys(ux).map(Number).sort(function (p, q) { return p - q; });
        b.ys = Object.keys(uy).map(Number).sort(function (p, q) { return p - q; });
        var xi = {}, yi = {};
        b.xs.forEach(function (v, i) { xi[v] = i; });
        b.ys.forEach(function (v, i) { yi[v] = i; });
        b.cells = {};
        b.slots.forEach(function (s, i) {
          b.cells[xi[s[1]] * 4096 + yi[s[2]]] = i;
          m.slotIndex[b.id + '-' + s[0]] = { b: b, s: s };
        });
        b.paths = null;
      });
      m.bounds = xs.length ? [Math.min.apply(null, xs), Math.min.apply(null, ys), Math.max.apply(null, xs), Math.max.apply(null, ys)] : S.data.bounds;
      all.push(m.bounds);
    });
    var bd = S.data.bounds, pad = 30;
    S.world = [bd[0] - pad, bd[1] - pad, bd[2] + pad, bd[3] + pad];
    S.tiers = {};
  }

  /* Path2D для блока: по одному пути на цвет + общий путь обводки. */
  function blockPaths(m, b) {
    if (b.paths && b.pathsVersion === S.pathsVersion) return b.paths;
    var groups = {};
    var r = Math.min(b.sw, b.sh) * 0.14;
    for (var i = 0; i < b.slots.length; i++) {
      var s = b.slots[i];
      var key = fillKey(S.bySlot[m.slug + '|' + b.id + '-' + s[0]]);
      var g = groups[key] || (groups[key] = new Path2D());
      rrect(g, s[1], s[2], b.sw, b.sh, r);
    }
    b.paths = groups;
    b.pathsVersion = S.pathsVersion;
    return groups;
  }

  // ------------------------------------------------------------------ static base
  function zoneCenter(z) {
    if (z.rect) return [z.rect[0] + z.rect[2] / 2, z.rect[1] + z.rect[3] / 2];
    var xs = z.polygon.map(function (p) { return p[0]; }), ys = z.polygon.map(function (p) { return p[1]; });
    return [(Math.min.apply(null, xs) + Math.max.apply(null, xs)) / 2, (Math.min.apply(null, ys) + Math.max.apply(null, ys)) / 2];
  }
  function polyPath(c, pts, dx, dy) {
    c.beginPath();
    pts.forEach(function (p, i) { if (i) c.lineTo(p[0] + dx, p[1] + dy); else c.moveTo(p[0] + dx, p[1] + dy); });
    c.closePath();
  }
  function shapePath(c, z, dx, dy, grow) {
    dx = dx || 0; dy = dy || 0; grow = grow || 0;
    if (z.rect) {
      c.beginPath();
      var x = z.rect[0] + dx - grow, y = z.rect[1] + dy - grow, w = z.rect[2] + grow * 2, h = z.rect[3] + grow * 2;
      if (c.roundRect) c.roundRect(x, y, w, h, 0.8 + grow); else c.rect(x, y, w, h);
    } else polyPath(c, z.polygon, dx, dy);
  }
  /* Мягкая тень без фильтров: несколько полупрозрачных слоёв со смещением. */
  function softShadow(c, path, dx, dy, layers) {
    c.fillStyle = '#0f172a';
    for (var i = 0; i < layers.length; i++) {
      c.globalAlpha = layers[i][1];
      path(dx, dy, layers[i][0]);
      c.fill();
    }
    c.globalAlpha = 1;
  }

  /* Рисует всю статичную основу в контекст с уже установленной матрицей «мир → устройство».
     px — сколько device px в одной единице (для толщины линий). */
  function drawBase(c, px, marks) {
    var markets = S.data.markets;
    c.lineJoin = 'round'; c.lineCap = 'round';
    // Дороги: под всеми участками.
    markets.forEach(function (m) {
      var L = m.layout;
      c.save(); c.translate(m.ox, m.oy);
      if (L.outline && L.outline.length > 2) {
        polyPath(c, L.outline, 0, 0);
        c.strokeStyle = '#cfd7e2'; c.lineWidth = 17.5; c.stroke();
        c.strokeStyle = '#dde3eb'; c.lineWidth = 16; c.stroke();
      }
      (L.roads || []).forEach(function (r) {
        var w = r.width || 10;
        c.beginPath();
        r.points.forEach(function (p, i) { if (i) c.lineTo(p[0], p[1]); else c.moveTo(p[0], p[1]); });
        c.strokeStyle = '#cfd7e2'; c.lineWidth = w + 1.5; c.stroke();
        c.strokeStyle = '#dde3eb'; c.lineWidth = w; c.stroke();
        if (marks) {
          c.save();
          c.setLineDash([3, 3]); c.strokeStyle = 'rgba(255,255,255,.9)'; c.lineWidth = 0.45; c.lineCap = 'butt';
          c.stroke();
          c.restore();
        }
      });
      c.restore();
    });
    // Участки, зоны, подложки блоков.
    markets.forEach(function (m) {
      var L = m.layout;
      c.save(); c.translate(m.ox, m.oy);
      if (L.outline && L.outline.length > 2) {
        softShadow(c, function (dx, dy, g) { polyPath(c, L.outline, dx, dy + g * 0.4); }, 0, 1.4, [[1.6, 0.022], [0.8, 0.03], [0.2, 0.035]]);
        polyPath(c, L.outline, 0, 0);
        c.fillStyle = '#fbfcfd'; c.fill();
        c.globalAlpha = 0.035; c.fillStyle = m.color; c.fill(); c.globalAlpha = 1;
        c.globalAlpha = 0.5; c.strokeStyle = m.color; c.lineWidth = 1.4 / px; c.stroke(); c.globalAlpha = 1;
      }
      (L.zones || []).forEach(function (z) { drawZone(c, z, px); });
      m.blocks.forEach(function (b) {
        c.save();
        c.translate(b.x, b.y);
        if (b.rotate) c.rotate(b.rotate * Math.PI / 180);
        var tone = PLATE[b.tone] || PLATE.neutral;
        c.beginPath();
        if (c.roundRect) c.roundRect(-1.2, -1.2, b.w + 2.4, b.h + 2.4, 1.4); else c.rect(-1.2, -1.2, b.w + 2.4, b.h + 2.4);
        c.fillStyle = tone[0]; c.fill();
        c.strokeStyle = tone[1]; c.lineWidth = 0.8 / px; c.stroke();
        c.restore();
      });
      c.restore();
    });
  }

  function roofGradient(c, z, dx, dy, kind, warm) {
    var r = z.rect || [0, 0, 1, 1];
    var x = r[0] + dx, y = r[1] + dy, g;
    if (kind === 'hall') {
      g = c.createLinearGradient(x, y, x, y + r[3]);
      g.addColorStop(0, '#f8fafc'); g.addColorStop(0.5, '#eef2f7'); g.addColorStop(0.5, '#e4e9f1'); g.addColorStop(1, '#edf1f6');
    } else {
      g = c.createLinearGradient(x, y, x + r[2], y + r[3]);
      g.addColorStop(0, warm ? '#fff8e6' : '#ffffff'); g.addColorStop(1, warm ? '#f6e7bf' : '#e9eef5');
    }
    return g;
  }

  function drawZone(c, z, px) {
    if (!z || (!z.rect && !z.polygon)) return;
    var cc = zoneCenter(z);
    c.save();
    if (z.rotate) { c.translate(cc[0], cc[1]); c.rotate(z.rotate * Math.PI / 180); c.translate(-cc[0], -cc[1]); }
    var warm = z.tone === 'warm';
    var depth = 0.9 + 0.7 * (z.levels || 1);
    if (z.kind === 'building' || z.kind === 'hall') {
      var hall = z.kind === 'hall';
      var rx = -depth * 0.35, ry = -depth;
      shapePath(c, z); c.fillStyle = warm ? '#e3cf9b' : '#c9d2de'; c.fill();
      softShadow(c, function (dx, dy, g) { shapePath(c, z, rx + dx, ry + dy, g); }, 0.5, 1.1, [[0.9, 0.035], [0.45, 0.05], [0.1, 0.06]]);
      shapePath(c, z, rx, ry);
      c.fillStyle = z.rect ? roofGradient(c, z, rx, ry, hall ? 'hall' : 'bld', warm) : (warm ? '#fbf0d2' : '#f3f6fa');
      c.fill();
      c.strokeStyle = hall ? '#c3ccd9' : (warm ? '#e2cf98' : '#cbd5e1'); c.lineWidth = 0.8 / px; c.stroke();
      if (hall && z.rect) {
        var r = z.rect, y = r[1] + r[3] / 2 + ry;
        c.beginPath(); c.moveTo(r[0] + rx + 1, y); c.lineTo(r[0] + r[2] + rx - 1, y);
        c.strokeStyle = '#cfd7e2'; c.lineWidth = 0.35; c.stroke();
      }
    } else if (z.kind === 'parking') {
      shapePath(c, z); c.fillStyle = PATTERNS.bays; c.fill();
      c.save(); c.setLineDash([4 / px, 3 / px]); c.strokeStyle = '#cdd5e1'; c.lineWidth = 1 / px; c.stroke(); c.restore();
      c.beginPath();
      if (c.roundRect) c.roundRect(cc[0] - 2.6, cc[1] - 7.6, 5.2, 5.2, 1.3); else c.rect(cc[0] - 2.6, cc[1] - 7.6, 5.2, 5.2);
      c.fillStyle = '#3b5b8c'; c.fill();
    } else {
      var style = { green: ['#e3f1e7', '#cfe5d6'], plaza: ['#f1f3f7', '#e1e6ee'] }[z.kind] || [PATTERNS.service, '#d3dae5'];
      shapePath(c, z); c.fillStyle = style[0]; c.fill();
      c.strokeStyle = style[1]; c.lineWidth = 0.8 / px; c.stroke();
    }
    c.restore();
  }

  /* Кэш основы в двух разрешениях (строится в фоне после первого кадра). */
  function buildTier(scale) {
    var w = S.world, W = Math.ceil((w[2] - w[0]) * scale), H = Math.ceil((w[3] - w[1]) * scale);
    var cv = document.createElement('canvas');
    cv.width = W; cv.height = H;
    var c = cv.getContext('2d');
    c.setTransform(scale, 0, 0, scale, -w[0] * scale, -w[1] * scale);
    drawBase(c, scale, scale >= 3);
    S.tiers[scale] = { canvas: cv, scale: scale };
  }
  function scheduleTiers() {
    var version = S.pathsVersion, queue = TIERS.slice().reverse();
    function next() {
      if (version !== S.pathsVersion || !queue.length) return;
      var s = queue.shift();
      if (!S.tiers[s]) buildTier(s);
      invalidate();
      (window.requestIdleCallback || function (fn) { return setTimeout(fn, 30); })(next);
    }
    setTimeout(next, 0);
  }
  function pickTier(sc) {
    if (S.tiers[1.5] && sc <= 1.9) return S.tiers[1.5];
    if (S.tiers[4] && sc <= (S.moving ? 8 : 5.2)) return S.tiers[4];
    if (S.tiers[1.5] && S.moving && sc <= 2.6) return S.tiers[1.5];
    return null;
  }

  // ------------------------------------------------------------------ frame
  function resizeCanvas() {
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var W = stage.clientWidth, H = stage.clientHeight;
    if (W === S.W && H === S.H && dpr === S.dpr) return false;
    S.W = W; S.H = H; S.dpr = dpr;
    canvas.width = Math.max(1, Math.round(W * dpr));
    canvas.height = Math.max(1, Math.round(H * dpr));
    return true;
  }
  function invalidate() { if (!S.raf) S.raf = requestAnimationFrame(function () { S.raf = 0; draw(); }); }
  function drawNow() { if (S.raf) { cancelAnimationFrame(S.raf); S.raf = 0; } draw(); }
  function markMoving() {
    S.moving = true;
    clearTimeout(S.settleTimer);
    S.settleTimer = setTimeout(function () { S.moving = false; invalidate(); }, SETTLE_MS);
  }
  function setView(v, sync) {
    S.view = v;
    markMoving();
    if (sync) drawNow(); else invalidate();
  }

  function draw() {
    if (!S.data || !S.W) return;
    var v = S.view, dpr = S.dpr, k = v.k, sc = k * dpr;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    drawGrid(v, dpr);

    var view = [(-v.x) / k, (-v.y) / k, (S.W - v.x) / k, (S.H - v.y) / k];
    var tier = pickTier(sc), w = S.world;
    if (tier) {
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = S.moving ? 'low' : 'high';
      ctx.drawImage(tier.canvas, dpr * (k * w[0] + v.x), dpr * (k * w[1] + v.y),
        dpr * k * (w[2] - w[0]), dpr * k * (w[3] - w[1]));
    } else {
      ctx.setTransform(sc, 0, 0, sc, dpr * v.x, dpr * v.y);
      drawBase(ctx, sc, k >= LOD_MID);
    }

    drawStalls(v, dpr, view);
    drawMuted(v, dpr);
    drawOutlines(v, dpr);
    drawLabels(v, dpr, view);
    updateOverlays();
  }

  function drawGrid(v, dpr) {
    var k = v.k;
    [[10, '#dfe5ee', 0.5], [50, '#d3dbe6', 1]].forEach(function (g) {
      var step = g[0] * k;
      if (step < 7) return;
      var x0 = Math.floor(-v.x / k / g[0]) * g[0], y0 = Math.floor(-v.y / k / g[0]) * g[0];
      ctx.beginPath();
      for (var x = x0; x * k + v.x <= S.W; x += g[0]) { var px = Math.round(dpr * (x * k + v.x)) + 0.5; ctx.moveTo(px, 0); ctx.lineTo(px, canvas.height); }
      for (var y = y0; y * k + v.y <= S.H; y += g[0]) { var py = Math.round(dpr * (y * k + v.y)) + 0.5; ctx.moveTo(0, py); ctx.lineTo(canvas.width, py); }
      ctx.strokeStyle = g[1];
      ctx.globalAlpha = Math.min(1, (step - 7) / 14) * g[2] + (g[0] === 50 ? 0.25 : 0);
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.globalAlpha = 1;
    });
  }

  function blockTransform(b, v, dpr) {
    var s = dpr * v.k;
    ctx.setTransform(s * b.cos, s * b.sin, -s * b.sin, s * b.cos, dpr * (v.k * b.wx + v.x), dpr * (v.k * b.wy + v.y));
  }
  function inView(bb, view) { return !(bb[2] < view[0] || bb[0] > view[2] || bb[3] < view[1] || bb[1] > view[3]); }

  function drawStalls(v, dpr, view) {
    var lw = 0.6 / v.k;
    S.data.markets.forEach(function (m) {
      m.blocks.forEach(function (b) {
        if (!inView(b.bbox, view)) return;
        var groups = blockPaths(m, b);
        blockTransform(b, v, dpr);
        ctx.lineWidth = lw;
        for (var i = 0; i < FILL_KEYS.length; i++) {
          var key = FILL_KEYS[i], path = groups[key];
          if (!path) continue;
          ctx.globalAlpha = S.off.has(statusOfKey(key)) ? 0.14 : 1;
          ctx.fillStyle = key === 'inactive' ? PATTERNS.inactive : COLORS[key];
          ctx.fill(path);
          ctx.strokeStyle = key === 'free' ? '#cdd6e2' : 'rgba(15,23,42,.16)';
          ctx.stroke(path);
        }
        ctx.globalAlpha = 1;
      });
    });
  }

  /* Фокус на рынке: остальные участки «приглушаются» полупрозрачной заливкой цветом фона. */
  function drawMuted(v, dpr) {
    if (S.market === 'all') return;
    var s = dpr * v.k;
    ctx.setTransform(s, 0, 0, s, dpr * v.x, dpr * v.y);
    ctx.fillStyle = 'rgba(243,246,250,.64)';
    S.data.markets.forEach(function (m) {
      if (m.slug === S.market || !m.outlineWorld.length) return;
      polyPath(ctx, m.outlineWorld, 0, 0);
      ctx.fill();
    });
  }

  function drawOutline(key, width) {
    var hit = slotByKey(key);
    if (!hit) return;
    var v = S.view;
    blockTransform(hit.b, v, S.dpr);
    var p = new Path2D();
    rrect(p, hit.s[1], hit.s[2], hit.b.sw, hit.b.sh, Math.min(hit.b.sw, hit.b.sh) * 0.14);
    ctx.lineWidth = width / v.k;
    ctx.strokeStyle = '#0f172a';
    ctx.stroke(p);
  }
  function drawOutlines() {
    if (S.hover && (!S.selected || S.hover !== S.selected.slug + '|' + S.selected.slot)) drawOutline(S.hover, 1.6);
    if (S.selected && S.selected.slot) drawOutline(S.selected.slug + '|' + S.selected.slot, 2.4);
  }
  function slotByKey(key) {
    var i = key.indexOf('|'), m = S.markets[key.slice(0, i)];
    return m ? m.slotIndex[key.slice(i + 1)] || null : null;
  }

  // ------------------------------------------------------------------ labels
  function text(str, x, y, size, weight, fill, halo, align, baseline, angle) {
    ctx.setTransform(1, 0, 0, 1, x, y);
    if (angle) ctx.rotate(angle);
    ctx.font = weight + ' ' + size.toFixed(1) + 'px ' + FONT;
    ctx.textAlign = align || 'center';
    ctx.textBaseline = baseline || 'middle';
    if (halo) { ctx.lineJoin = 'round'; ctx.strokeStyle = '#fff'; ctx.lineWidth = halo; ctx.strokeText(str, 0, 0); }
    ctx.fillStyle = fill;
    ctx.fillText(str, 0, 0);
  }
  function toScreen(wx, wy) { var v = S.view; return [S.dpr * (wx * v.k + v.x), S.dpr * (wy * v.k + v.y)]; }

  function drawLabels(v, dpr, view) {
    var k = v.k, mid = k >= LOD_MID, deep = k >= LOD_DEEP;
    S.data.markets.forEach(function (m) {
      var muted = S.market !== 'all' && S.market !== m.slug;
      ctx.globalAlpha = muted ? 0.38 : 1;
      var L = m.layout;
      (L.zones || []).forEach(function (z) {
        if (!z.rect && !z.polygon) return;
        var c = zoneCenter(z), depth = 0.9 + 0.7 * (z.levels || 1);
        if (z.kind === 'building' || z.kind === 'hall') { c = [c[0] - depth * 0.35, c[1] - depth]; }
        var wx = m.ox + c[0], wy = m.oy + c[1];
        var angle = z.rotate ? z.rotate * Math.PI / 180 : 0;
        if (z.kind === 'parking') {
          var p = toScreen(wx, wy - 5), ps = 3.6 * k * dpr;
          if (ps >= 4 * dpr) text('P', p[0], p[1], Math.min(ps, 30 * dpr), 800, '#fff');
        }
        if (!mid || !z.name) return;
        var size = Math.min(3.1 * k, 26) * dpr, sp = toScreen(wx, wy + (z.kind === 'parking' ? 1 : 0));
        text(z.name, sp[0], sp[1], size, 650, '#475569', 0.7 * k * dpr > 3 * dpr ? 3 * dpr : 0.7 * k * dpr, 'center', 'middle', angle);
        if (z.kind === 'parking' && z.places) {
          var sub = toScreen(wx, wy + 4.4);
          text(nf.format(z.places) + ' ' + plural(z.places, 'место', 'места', 'мест'), sub[0], sub[1],
            Math.min(2.3 * k, 18) * dpr, 550, '#64748b', Math.min(0.6 * k, 2.4) * dpr);
        }
      });
      (L.labels || []).forEach(function (lb) {
        if (!mid) return;
        var p = toScreen(m.ox + lb.at[0], m.oy + lb.at[1]);
        text(lb.text, p[0], p[1], Math.min((lb.size || 3.1) * k, 26) * dpr, 650, '#475569', 2 * dpr);
      });
      if (mid) {
        m.blocks.forEach(function (b) {
          if (!inView(b.bbox, view)) return;
          var lx = 0, ly = -2.2;
          var p = toScreen(b.wx + lx * b.cos - ly * b.sin, b.wy + lx * b.sin + ly * b.cos);
          text(b.name, p[0], p[1], Math.min(2.5 * k, 20) * dpr, 700, '#64748b', Math.min(0.6 * k, 2.4) * dpr, 'left', 'alphabetic',
            b.rotate ? b.rotate * Math.PI / 180 : 0);
        });
      }
      if (deep && !S.moving) drawStallCodes(m, v, dpr, view);
      if (!deep) drawMarketLabel(m, dpr);
      ctx.globalAlpha = 1;
    });
    ctx.setTransform(1, 0, 0, 1, 0, 0);
  }

  function drawMarketLabel(m, dpr) {
    var L = m.layout, ml = L.label || { text: m.name, at: [0, -8] };
    var p = toScreen(m.ox + ml.at[0], m.oy + ml.at[1]);
    var meta = [];
    if (m.area_m2) meta.push(nf.format(m.area_m2) + ' м²');
    if (m.parking_places) meta.push('парковка ' + nf.format(m.parking_places));
    var minY = Math.min.apply(null, (L.outline || [[0, 0]]).map(function (q) { return q[1]; }));
    var above = ml.at[1] < minY, big = 17 * dpr, small = 10.5 * dpr;
    text(ml.text || m.name, p[0], p[1], big, 750, m.color, 4 * dpr, 'center', 'alphabetic');
    if (meta.length) {
      var dy = above ? -big * 1.05 : small * 1.6;
      text(meta.join(' · '), p[0], p[1] + dy, small, 600, '#64748b', 3 * dpr, 'center', 'alphabetic');
    }
  }

  function drawStallCodes(m, v, dpr, view) {
    m.blocks.forEach(function (b) {
      if (!inView(b.bbox, view)) return;
      var vertical = b.sh > b.sw * 1.05;
      var base = (b.rotate || 0) * Math.PI / 180 - (vertical ? Math.PI / 2 : 0);
      var room = (vertical ? b.sh : b.sw) * v.k * dpr * 0.86;
      var size = Math.min(0.95 * v.k * dpr, (vertical ? b.sw : b.sh) * v.k * dpr * 0.42);
      ctx.font = '700 ' + size.toFixed(1) + 'px ' + FONT;
      for (var i = 0; i < b.slots.length; i++) {
        var s = b.slots[i], p = S.bySlot[m.slug + '|' + b.id + '-' + s[0]];
        if (!p) continue;
        var cx = s[1] + b.sw / 2, cy = s[2] + b.sh / 2;
        var wx = b.wx + cx * b.cos - cy * b.sin, wy = b.wy + cx * b.sin + cy * b.cos;
        if (wx < view[0] || wx > view[2] || wy < view[1] || wy > view[3]) continue;
        var q = toScreen(wx, wy);
        ctx.setTransform(1, 0, 0, 1, q[0], q[1]);
        if (base) ctx.rotate(base);
        var tw = ctx.measureText(p.code).width;
        if (tw > room) ctx.scale(room / tw, room / tw);
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.globalAlpha = (S.off.has(p.status) ? 0.2 : 0.8) * (S.market !== 'all' && S.market !== m.slug ? 0.38 : 1);
        ctx.fillStyle = '#0f172a';
        ctx.fillText(p.code, 0, 0);
      }
    });
    ctx.globalAlpha = 1;
  }

  // ------------------------------------------------------------------ DOM overlays (масштаб, пульс)
  var lastScale = '', lastPing = '';
  function updateOverlays() {
    var steps = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000];
    var target = 110 / S.view.k, best = steps[0];
    steps.forEach(function (s) { if (s <= target) best = s; });
    var sig = best + ':' + Math.round(best * S.view.k);
    if (sig !== lastScale) {
      lastScale = sig;
      $('mm-scale-bar').style.width = Math.round(best * S.view.k) + 'px';
      $('mm-scale-label').textContent = best >= 1000 ? best / 1000 + ' км' : best + ' м';
    }
    var c = S.selected && S.selected.slot ? slotCenter(S.selected.slug, S.selected.slot) : null;
    if (!c) { if (lastPing !== 'off') { pingEl.hidden = true; lastPing = 'off'; } return; }
    var x = c[0] * S.view.k + S.view.x, y = c[1] * S.view.k + S.view.y;
    var scale = Math.max(0.45, Math.min(2.4, S.view.k / 10));
    var t = 'translate3d(' + x.toFixed(1) + 'px,' + y.toFixed(1) + 'px,0) scale(' + scale.toFixed(3) + ')';
    if (t !== lastPing) { pingEl.style.transform = t; pingEl.hidden = false; lastPing = t; }
  }

  // ------------------------------------------------------------------ camera
  function size() { return [S.W || stage.clientWidth, S.H || stage.clientHeight]; }

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
  function stopAnim() { if (S.anim) { cancelAnimationFrame(S.anim); S.anim = null; } }

  function animateTo(target, duration) {
    stopAnim();
    duration = REDUCED ? 0 : (duration == null ? 700 : duration);
    if (!duration) { setView(target); return; }
    var sz = size(), from = Object.assign({}, S.view);
    // Интерполяция центра в мировых координатах и масштаба в логарифмах — без «дёрганья».
    var c0 = [(sz[0] / 2 - from.x) / from.k, (sz[1] / 2 - from.y) / from.k];
    var c1 = [(sz[0] / 2 - target.x) / target.k, (sz[1] / 2 - target.y) / target.k];
    var l0 = Math.log(from.k), l1 = Math.log(target.k);
    var start = performance.now();
    function frame(now) {
      var t = Math.min(1, (now - start) / duration);
      var e = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
      var k = Math.exp(l0 + (l1 - l0) * e);
      var cx = c0[0] + (c1[0] - c0[0]) * e, cy = c0[1] + (c1[1] - c0[1]) * e;
      S.anim = t < 1 ? requestAnimationFrame(frame) : null;
      setView({ k: k, x: sz[0] / 2 - cx * k, y: sz[1] / 2 - cy * k }, true);
    }
    S.anim = requestAnimationFrame(frame);
  }

  function zoomAt(px, py, factor, animate) {
    var v = S.view, k = clampK(v.k * factor), f = k / v.k;
    var target = { k: k, x: px - (px - v.x) * f, y: py - (py - v.y) * f };
    if (animate) animateTo(target, 260); else setView(target);
  }

  function fit(animate) {
    var b = S.market !== 'all' && S.markets[S.market] ? S.markets[S.market].bounds : S.data.bounds;
    var target = viewFor(b);
    if (S.market === 'all') S.fitK = target.k;
    if (animate) animateTo(target); else setView(target);
  }

  // ------------------------------------------------------------------ hit-testing
  function lowerBound(arr, v) {
    var lo = 0, hi = arr.length - 1, ans = -1;
    while (lo <= hi) { var mid = (lo + hi) >> 1; if (arr[mid] <= v) { ans = mid; lo = mid + 1; } else hi = mid - 1; }
    return ans;
  }
  /* css px внутри холста → "slug|ключ места" или null. */
  function hitTest(px, py) {
    if (!S.data) return null;
    var v = S.view, wx = (px - v.x) / v.k, wy = (py - v.y) / v.k;
    var markets = S.data.markets;
    for (var mi = 0; mi < markets.length; mi++) {
      var m = markets[mi];
      if (S.market !== 'all' && S.market !== m.slug) continue;
      for (var bi = 0; bi < m.blocks.length; bi++) {
        var b = m.blocks[bi], bb = b.bbox;
        if (wx < bb[0] || wx > bb[2] || wy < bb[1] || wy > bb[3]) continue;
        var dx = wx - b.wx, dy = wy - b.wy;
        var lx = dx * b.cos + dy * b.sin, ly = -dx * b.sin + dy * b.cos;
        if (lx < 0 || ly < 0 || lx > b.w || ly > b.h) continue;
        var xi = lowerBound(b.xs, lx), yi = lowerBound(b.ys, ly);
        if (xi < 0 || yi < 0 || lx > b.xs[xi] + b.sw || ly > b.ys[yi] + b.sh) continue;
        var idx = b.cells[xi * 4096 + yi];
        if (idx === undefined) continue;
        return m.slug + '|' + b.id + '-' + b.slots[idx][0];
      }
    }
    return null;
  }
  function localPoint(e) { var r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; }

  // ------------------------------------------------------------------ pointer: pan / pinch / click
  var pointers = new Map(), drag = null, lastTap = 0;
  canvas.addEventListener('pointerdown', function (e) {
    if (e.button !== 0 && e.pointerType === 'mouse') return;
    canvas.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, [e.clientX, e.clientY]);
    stopAnim();
    drag = { moved: 0, start: [e.clientX, e.clientY], view: Object.assign({}, S.view), pinch: null, vel: [0, 0], t: performance.now(), last: [e.clientX, e.clientY] };
    if (pointers.size === 2) startPinch();
  });
  canvas.addEventListener('pointermove', function (e) {
    if (!pointers.has(e.pointerId)) { hover(e); return; }
    pointers.set(e.pointerId, [e.clientX, e.clientY]);
    if (!drag) return;
    if (pointers.size >= 2 && drag.pinch) { doPinch(); return; }
    var dx = e.clientX - drag.start[0], dy = e.clientY - drag.start[1];
    drag.moved = Math.max(drag.moved, Math.abs(dx) + Math.abs(dy));
    if (drag.moved > 3) {
      canvas.classList.add('is-dragging');
      hideTooltip();
      var now = performance.now(), dt = Math.max(now - drag.t, 1);
      drag.vel = [(e.clientX - drag.last[0]) / dt, (e.clientY - drag.last[1]) / dt];
      drag.t = now; drag.last = [e.clientX, e.clientY];
      setView({ k: drag.view.k, x: drag.view.x + dx, y: drag.view.y + dy });
    }
  });
  function endPointer(e) {
    if (!pointers.has(e.pointerId)) return;
    pointers.delete(e.pointerId);
    canvas.classList.remove('is-dragging');
    if (!drag) return;
    if (drag.pinch) { if (pointers.size < 2) drag = null; return; }
    if (drag.moved <= 4 && e.type === 'pointerup') {
      var now = performance.now(), p = localPoint(e);
      if (now - lastTap < 300 && e.pointerType !== 'mouse') zoomAt(p[0], p[1], 2, true);
      else clickAt(p[0], p[1]);
      lastTap = now;
    } else if (!REDUCED && performance.now() - drag.t < 80) {
      inertia(drag.vel);
    }
    drag = null;
  }
  canvas.addEventListener('pointerup', endPointer);
  canvas.addEventListener('pointercancel', endPointer);
  canvas.addEventListener('pointerleave', function () { if (!pointers.size) clearHover(); });
  canvas.addEventListener('dblclick', function (e) {
    e.preventDefault();
    var p = localPoint(e);
    zoomAt(p[0], p[1], e.shiftKey ? 0.5 : 2, true);
  });

  function inertia(vel) {
    var vx = vel[0] * 16, vy = vel[1] * 16;
    function step() {
      vx *= 0.92; vy *= 0.92;
      if (Math.abs(vx) + Math.abs(vy) < 0.3) { S.anim = null; return; }
      S.anim = requestAnimationFrame(step);
      setView({ k: S.view.k, x: S.view.x + vx, y: S.view.y + vy }, true);
    }
    S.anim = requestAnimationFrame(step);
  }

  function pinchState() {
    var p = Array.from(pointers.values()), r = canvas.getBoundingClientRect();
    return { d: Math.hypot(p[0][0] - p[1][0], p[0][1] - p[1][1]) || 1, c: [(p[0][0] + p[1][0]) / 2 - r.left, (p[0][1] + p[1][1]) / 2 - r.top] };
  }
  function startPinch() { drag.pinch = pinchState(); drag.view = Object.assign({}, S.view); drag.moved = 99; }
  function doPinch() {
    var now = pinchState(), st = drag.pinch, v0 = drag.view;
    var k = clampK(v0.k * now.d / st.d), f = k / v0.k;
    setView({ k: k, x: now.c[0] - (st.c[0] - v0.x) * f, y: now.c[1] - (st.c[1] - v0.y) * f });
  }

  var wheelAcc = 0, wheelPt = null, wheelRaf = null;
  canvas.addEventListener('wheel', function (e) {
    e.preventDefault();
    stopAnim();
    hideTooltip();
    var p = localPoint(e);
    var delta = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
    if (!e.ctrlKey && Math.abs(e.deltaX) > Math.abs(e.deltaY) * 1.5) {
      setView({ k: S.view.k, x: S.view.x - e.deltaX, y: S.view.y }); return;
    }
    wheelAcc += -delta * (e.ctrlKey ? 0.01 : 0.0016);
    wheelPt = p;
    if (!wheelRaf) wheelRaf = requestAnimationFrame(function () {
      wheelRaf = null;
      var v = S.view, k = clampK(v.k * Math.exp(Math.max(-1, Math.min(1, wheelAcc)))), f = k / v.k;
      wheelAcc = 0;
      setView({ k: k, x: wheelPt[0] - (wheelPt[0] - v.x) * f, y: wheelPt[1] - (wheelPt[1] - v.y) * f }, true);
    });
  }, { passive: false });

  $('mm-zoom-in').addEventListener('click', function () { var s = size(); zoomAt(s[0] / 2, s[1] / 2, 1.6, true); });
  $('mm-zoom-out').addEventListener('click', function () { var s = size(); zoomAt(s[0] / 2, s[1] / 2, 1 / 1.6, true); });
  $('mm-fit').addEventListener('click', function () { fit(true); });

  canvas.addEventListener('keydown', function (e) {
    var s = size(), step = 80;
    var map = { '+': function () { zoomAt(s[0] / 2, s[1] / 2, 1.4, true); }, '=': function () { zoomAt(s[0] / 2, s[1] / 2, 1.4, true); },
      '-': function () { zoomAt(s[0] / 2, s[1] / 2, 1 / 1.4, true); }, '0': function () { fit(true); },
      ArrowLeft: function () { pan(step, 0); }, ArrowRight: function () { pan(-step, 0); },
      ArrowUp: function () { pan(0, step); }, ArrowDown: function () { pan(0, -step); } };
    if (map[e.key]) { e.preventDefault(); map[e.key](); }
  });
  function pan(dx, dy) { animateTo({ k: S.view.k, x: S.view.x + dx, y: S.view.y + dy }, 220); }

  if (window.ResizeObserver) {
    new ResizeObserver(function () {
      if (resizeCanvas() && S.data) { S.fitK = viewFor(S.data.bounds).k; drawNow(); }
    }).observe(stage);
  } else {
    window.addEventListener('resize', function () { if (resizeCanvas() && S.data) drawNow(); });
  }

  // ------------------------------------------------------------------ hover / tooltip
  function hover(e) {
    var p = localPoint(e), key = hitTest(p[0], p[1]);
    if (!key) { clearHover(); return; }
    canvas.classList.add('is-pointer');
    if (S.hover !== key) {
      S.hover = key;
      tooltip.innerHTML = tooltipHtml(key);
      tooltip.hidden = false;
      invalidate();
    }
    var r = stage.getBoundingClientRect(), x = e.clientX - r.left + 16, y = e.clientY - r.top + 16;
    var w = tooltip.offsetWidth, h = tooltip.offsetHeight;
    if (x + w > r.width - 8) x = e.clientX - r.left - w - 14;
    if (y + h > r.height - 8) y = e.clientY - r.top - h - 14;
    tooltip.style.transform = 'translate3d(' + Math.round(x) + 'px,' + Math.round(y) + 'px,0)';
  }
  function hideTooltip() { tooltip.hidden = true; }
  function clearHover() {
    hideTooltip();
    canvas.classList.remove('is-pointer');
    if (S.hover) { S.hover = null; invalidate(); }
  }

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
  function clickAt(px, py) {
    var key = hitTest(px, py);
    if (!key) { if (S.selected) closeDrawer(); return; }
    var i = key.indexOf('|'), slug = key.slice(0, i), slot = key.slice(i + 1), p = S.bySlot[key];
    if (p) openPoint(p, { fly: false }); else openFree(slug, slot);
  }

  function select(slug, slot, point) {
    S.selected = slug || point ? { slug: slug, slot: slot, point: point || null } : null;
    invalidate();
  }

  function slotCenter(slug, slot) {
    var m = S.markets[slug], hit = m && m.slotIndex[slot];
    if (!hit) return null;
    var b = hit.b, lx = hit.s[1] + b.sw / 2, ly = hit.s[2] + b.sh / 2;
    return [b.wx + lx * b.cos - ly * b.sin, b.wy + lx * b.sin + ly * b.cos];
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
    var hit = m.slotIndex[slot];
    return hit ? hit.b.name : slot.slice(0, slot.lastIndexOf('-'));
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
  function mark(textValue, q) {
    var s = String(textValue || ''), i = s.toLowerCase().indexOf(q.toLowerCase());
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
    renderLegend();
    fit(animate !== false);
    invalidate();
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
    renderLegend();
    invalidate();
  });
  $('mm-legend-reset').addEventListener('click', function () { S.off.clear(); renderLegend(); invalidate(); });
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

  initPatterns();
  resizeCanvas();
  load(false);
})();
