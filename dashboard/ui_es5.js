// ============================================================
// Dashboard JS — ES5 universal (compatible con cualquier navegador)
// v1.9.0 (21-oct-26): pestañas y lenguaje simple. El dueño: "ahorita el
// dashboard es un desastre, necesito que sea amigable". La página era una
// sola columna con 80 partidos y columnas de tres números; ahora "Hoy"
// responde ¿apuesto algo?, ¿cómo vamos? y qué jugaría el programa.
// ============================================================

// ── Almacenamiento seguro + errores visibles ──
function _storeGet(key, dflt){ try { return localStorage.getItem(key) || dflt; } catch(e){ return dflt; } }
function _storeSet(key, val){ try { localStorage.setItem(key, val); } catch(e){} }
window.onerror = function(msg, src, line){
  var el = document.getElementById('err');
  el.style.display = 'block';
  el.textContent = '⚠️ Error interno: ' + msg + ' (línea ' + line + ')';
};

// ── Helpers ──
function money(v){ return (v >= 0 ? '+' : '') + Number(v).toFixed(2) + 'u'; }
function pct(v){ return v === null || v === undefined ? '—' : (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '%'; }
function p100(v){ return v === null || v === undefined ? '—' : Math.round(v * 100) + '%'; }
function esc(s){ return String(s === null || s === undefined ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function el(id){ return document.getElementById(id); }

// Fechas UTC → hora de México (UTC-6 fijo desde 2022, cálculo manual ES5)
var MX_MESES = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'];
var MX_MESES_L = ['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'];
var MX_DIAS = ['domingo','lunes','martes','miércoles','jueves','viernes','sábado'];
function p2(n){ return (n < 10 ? '0' : '') + n; }
function _mx(s){
  var t = s === undefined ? new Date() : new Date(s);
  if (isNaN(t.getTime())) return null;
  return new Date(t.getTime() - 6 * 3600000);
}
function mxdate(s){
  var mx = _mx(s);
  if (!mx) return String(s).slice(0, 16);
  return p2(mx.getUTCDate()) + ' ' + MX_MESES[mx.getUTCMonth()] + ', ' + p2(mx.getUTCHours()) + ':' + p2(mx.getUTCMinutes());
}
function mxtime(s){ var mx = _mx(s); return mx ? p2(mx.getUTCHours()) + ':' + p2(mx.getUTCMinutes()) : ''; }
function mxday(s){ var mx = _mx(s); return mx ? mx.getUTCFullYear() + '-' + p2(mx.getUTCMonth() + 1) + '-' + p2(mx.getUTCDate()) : ''; }
function mxdaylabel(s){
  var mx = _mx(s);
  if (!mx) return '';
  var name = mxday(s) === mxday(new Date().getTime() + 86400000) ? 'Mañana' : MX_DIAS[mx.getUTCDay()];
  return name.charAt(0).toUpperCase() + name.slice(1) + ' ' + mx.getUTCDate() + ' de ' + MX_MESES_L[mx.getUTCMonth()];
}

// "2026-10-12" (lunes) → "12 – 18 oct"
function weekLabel(iso){
  var t = new Date(String(iso).slice(0, 10) + 'T12:00:00Z');
  if (isNaN(t.getTime())) return esc(iso);
  var e = new Date(t.getTime() + 6 * 86400000);
  return t.getUTCDate() + (t.getUTCMonth() === e.getUTCMonth() ? '' : ' ' + MX_MESES[t.getUTCMonth()]) +
         ' – ' + e.getUTCDate() + ' ' + MX_MESES[e.getUTCMonth()];
}

function err(msg){
  var e = el('err');
  e.style.display = 'block';
  e.textContent = '⚠️ ' + msg;
}

// ── Sin conexión con el programa (v1.6.0) ──
// status 0 = el navegador no llegó al programa: cerrado o reiniciándose. El
// supervisor lo vuelve a abrir solo; la página conserva lo último que
// mostró, avisa arriba y se recarga sola en cuanto el programa responde.
var OFFLINE_MSG = 'El programa del dashboard no responde';
var _offlineSince = null, _offlineTimer = null;
function _offlineBanner(){
  var b = el('offline');
  if (!b) {
    b = document.createElement('div');
    b.id = 'offline';
    b.style.cssText = 'position:sticky;top:0;z-index:10;background:#3a2a14;color:#fbbf24;' +
                      'padding:10px 14px;border-radius:8px;margin:10px 0;font-size:.9rem';
    document.body.insertBefore(b, document.body.firstChild);
  }
  return b;
}
function serverDown(){
  var now = new Date().getTime();
  if (_offlineSince === null) _offlineSince = now;
  var mins = Math.floor((now - _offlineSince) / 60000);
  _offlineBanner().textContent = mins < 2
    ? '⏳ ' + OFFLINE_MSG + ': se está reiniciando solo. Esta página se actualiza sola cuando vuelva.'
    : '⚠️ ' + OFFLINE_MSG + ' desde hace ' + mins + ' min. Ábrelo de nuevo (BettingDashboard.exe) ' +
      'o reinicia la PC: arranca solo con Windows. Esta página se actualiza sola cuando vuelva.';
  if (!_offlineTimer) _offlineTimer = setInterval(_probeServer, 5000);
}
function _probeServer(){
  var x = new XMLHttpRequest();
  x.open('GET', '/api/health?t=' + new Date().getTime(), true);
  x.onreadystatechange = function(){
    if (x.readyState !== 4) return;
    if (x.status === 200) { clearInterval(_offlineTimer); location.reload(); }
    else serverDown();
  };
  x.send();
}

// XHR GET + JSON (compatibilidad universal; fetch no existe en navegadores viejos)
function getJSON(url, ok, fail){
  try {
    var x = new XMLHttpRequest();
    x.open('GET', url, true);
    x.onreadystatechange = function(){
      if (x.readyState !== 4) return;
      if (x.status === 0) { serverDown(); return; }       // programa caído: aviso arriba
      if (x.status !== 200) { if (fail) fail('HTTP ' + x.status); return; }
      var d;
      try { d = JSON.parse(x.responseText); } catch(e2) { if (fail) fail('JSON inválido'); return; }
      if (d.ok === false) { if (fail) fail(d.msg || 'sin datos'); return; }
      if (ok) ok(d);
    };
    x.send();
  } catch(e) { if (fail) fail(e.message); }
}

function emptyRow(cols, msg){ return '<tr><td colspan="' + cols + '" class="muted">' + esc(msg) + '</td></tr>'; }
function resultPill(key, label){
  var k = String(key || 'pending').toLowerCase();
  var cls = k === 'win' || k === 'half_win' ? 'win' : k === 'loss' || k === 'half_loss' ? 'loss' :
            k === 'pending' ? 'pending' : k === 'cancelled' ? 'cancelled' : 'push';
  var icon = cls === 'win' ? '✅ ' : cls === 'loss' ? '❌ ' : '';
  return '<span class="pill ' + cls + '">' + icon + esc(label) + '</span>';
}

// ── Pestañas ──
var TABS = ['hoy', 'proximos', 'resultados', 'dinero', 'sistema'];
var _chartsDrawn = {};
function showTab(name){
  if (TABS.indexOf(name) < 0) name = 'hoy';
  var btns = el('tabs').getElementsByTagName('button');
  for (var i = 0; i < btns.length; i++) btns[i].className = 'tab' + (btns[i].getAttribute('data-tab') === name ? ' on' : '');
  for (var j = 0; j < TABS.length; j++) el('tab-' + TABS[j]).className = 'pane' + (TABS[j] === name ? ' on' : '');
  _storeSet('tab', name);
  // Chart.js no dibuja en un contenedor oculto: las gráficas se dibujan
  // la primera vez que se ve su pestaña
  if (name === 'dinero') drawCharts(['bankchart', 'daily']);
}
(function(){
  var btns = el('tabs').getElementsByTagName('button');
  for (var i = 0; i < btns.length; i++) {
    btns[i].onclick = function(){ showTab(this.getAttribute('data-tab')); };
  }
})();

// Secciones plegables (▸ Ver …)
(function(){
  var heads = document.querySelectorAll ? document.querySelectorAll('.foldhead') : [];
  for (var i = 0; i < heads.length; i++) {
    heads[i].onclick = function(){
      var f = this.parentNode, open = f.className.indexOf('open') < 0;
      f.className = open ? 'fold open' : 'fold';
      this.textContent = (open ? '▾ ' : '▸ ') + this.textContent.replace(/^[▸▾] /, '');
      if (open && f.innerHTML.indexOf('id="equity"') >= 0) drawCharts(['equity', 'clvchart', 'bymarket', 'byleague']);
    };
  }
})();

// ── Partidos: Hoy y Próximos días ──
var _matches = [], _lineups = {};
var SIDE_ES = { home: 'Local', draw: 'Empate', away: 'Visitante' };

function matchState(m){
  if (m.real_bet) return ['yes', '✅ Apuesta real', 'Tienes una apuesta real en este partido'];
  if (m.old_model || !m.pick || !m.pick.label) return ['no', 'Sin análisis nuevo', 'Se actualiza en la siguiente corrida del programa'];
  if (m.pick.pd_value) return ['yes', '✅ Vale la pena', 'PlayDoit suele pagar el mínimo o más. Confírmalo con una captura al bot'];
  if (m.pick.value) return ['maybe', '🟡 Revisa PlayDoit', 'El mejor momio de Europa sí paga el mínimo; PlayDoit normalmente no. Manda una captura al bot para confirmarlo'];
  return ['no', '❌ No conviene', 'PlayDoit no paga lo suficiente para tener ventaja'];
}

function teams(m){
  var parts = String(m.match).split(' vs ');
  return { home: parts[0] || 'Local', away: parts[1] || 'Visitante' };
}

function matchHtml(m, i){
  var st = matchState(m), pk = m.pick || {}, t = teams(m);
  var pick = '<span class="muted">—</span>', price = '';
  if (!m.old_model && pk.label) {
    pick = '<div class="lbl2">' + (pk.lean ? 'El programa jugaría' : 'Lo más probable') + '</div><b>' + esc(pk.label) +
           '</b> <span class="muted">(' + p100(pk.model) + ')</span>';
    price = '<div class="lbl2">Necesitas en PlayDoit</div><b>' + esc(pk.min_us || '—') + ' o mejor</b>' +
            (pk.pd_est_us ? '<div class="lbl2" title="' + (pk.pd_learned ? 'Estimado con lo que PlayDoit pagó en tus capturas' :
               'Estimado con el promedio de las casas: PlayDoit suele pagar parecido') + '">paga aprox. ' + esc(pk.pd_est_us) + '</div>' : '');
  }
  var lu = _lineups[m.match];
  var more = [];
  if (m.old_model) more.push('Analizado con el modelo anterior a la corrección; se actualiza en la siguiente corrida.');
  else {
    var mod = [], pin = [];
    for (var j = 0; j < m.sides.length; j++) {
      var name = m.sides[j].side === 'home' ? t.home : m.sides[j].side === 'away' ? t.away : 'Empate';
      mod.push(esc(name) + ' ' + p100(m.sides[j].model));
      pin.push(p100(m.sides[j].pin));
    }
    more.push('<b>Probabilidades del programa:</b> ' + mod.join(' · ') + ' &nbsp;<span title="Pinnacle sin margen: el precio más preciso del mercado">(Pinnacle: ' + pin.join(' · ') + ')</span>');
    if (pk.label && pk.market !== undefined)
      more.push('<b>Por qué ' + esc(pk.label).toLowerCase() + ':</b> el programa le da ' + p100(pk.model) + ' y el mercado ' + p100(pk.market) +
                (pk.lean ? '.' : ' (no ve nada mejor que el mercado: es solo lo más probable).'));
    var g = [];
    if (m.lam) g.push('goles esperados ' + m.lam[0].toFixed(1) + ' – ' + m.lam[1].toFixed(1));
    if (m.likely) g.push('marcador más probable ' + m.likely.score + ' (' + p100(m.likely.prob) + ')');
    if (m.p_over25 !== null && m.p_over25 !== undefined) g.push('más de 2.5 goles ' + p100(m.p_over25));
    if (m.p_btts !== null && m.p_btts !== undefined) g.push('ambos anotan ' + p100(m.p_btts));
    if (g.length) more.push('<b>Goles:</b> ' + g.join(' · '));
    var mins = [];
    for (var k = 0; k < m.sides.length; k++) mins.push(SIDE_ES[m.sides[k].side] + ' ' + (m.sides[k].min_us || '—'));
    more.push('<b>Mínimo en PlayDoit por resultado:</b> ' + mins.join(' · '));
  }
  if (lu) more.push('<b>Alineaciones:</b> ' + esc(lu.text));
  var sk = statsKey(m.key, m.match_date), open = !!_open[sk];
  return '<div class="match' + (open ? ' open' : '') + '" id="m' + i + '"><div class="mrow" onclick="toggleMatch(' + i + ')">' +
         '<div class="mtime">' + mxtime(m.match_date) + '</div>' +
         '<div class="mname"><b>' + esc(m.match) + '</b><div class="lbl2">' + esc(m.league) + '</div></div>' +
         '<div class="mpick">' + pick + '</div><div class="mprice">' + price + '</div>' +
         '<div class="mverdict"><span class="pill ' + st[0] + '" title="' + esc(st[2]) + '">' + st[1] + '</span></div>' +
         '</div><div class="mmore">' + more.join('<br>') +
         (m.key ? '<div class="stats" id="st' + i + '">' + (_statsCache[sk] || '') + '</div>' : '') + '</div></div>';
}
function toggleMatch(i){
  var box = el('m' + i), m = _matches[i];
  if (!box) return;
  var open = box.className.indexOf('open') < 0, sk = statsKey(m.key, m.match_date);
  box.className = open ? 'match open' : 'match';
  if (open) _open[sk] = true; else delete _open[sk];
  if (open && m.key) loadStats('st' + i, m.key, m.match_date);
}

// ── Estadísticas del partido (v1.10.0): últimos 5 de cada equipo con
// resultado, goles, córners, tarjetas y su once; alineación del partido;
// últimos 5 entre ellos. Se piden al abrir el partido y se guardan. ──
var _open = {}, _statsCache = {};
function statsKey(key, date){ return String(key) + '|' + String(date); }
function shortDate(iso){
  var p = String(iso).slice(0, 10).split('-');
  return p.length === 3 ? parseInt(p[2], 10) + ' ' + MX_MESES[parseInt(p[1], 10) - 1] + ' ' + p[0].slice(2) : esc(iso);
}
function toggleXi(a){
  var row = a.parentNode.parentNode.nextSibling;
  if (!row) return;
  var show = row.style.display === 'none';
  row.style.display = show ? '' : 'none';
  a.textContent = show ? 'once ▾' : 'once ▸';
}
function teamBlock(t){
  var s = t.summary, h = '<div class="tblock"><div class="tname">' + esc(t.team) + '</div>';
  if (!s) return h + '<div class="muted small">Sin partidos anteriores en la base.</div></div>';
  h += '<div class="small">Últimos ' + s.n + ': <b>' + s.w + ' G · ' + s.d + ' E · ' + s.l + ' P</b> · goles ' + s.goals_for +
       ' a favor y ' + s.goals_against + ' en contra' +
       (s.corners_for !== null ? ' · córners ' + s.corners_for + ' a favor y ' + s.corners_against + ' en contra' : ' · <span class="muted">sin datos de córners</span>') +
       (s.cards !== null ? ' · ' + s.cards + ' tarjetas por partido' : '') + '</div>';
  h += '<table class="mini"><thead><tr><th>Fecha</th><th>Rival</th><th>Resultado</th><th title="A favor - en contra">Córners</th><th title="Del partido, roja = 1">Tarjetas</th><th></th></tr></thead><tbody>';
  for (var i = 0; i < t.matches.length; i++) {
    var m = t.matches[i], rc = m.result === 'G' ? 'pos' : m.result === 'P' ? 'neg' : '';
    h += '<tr><td>' + shortDate(m.date) + '</td><td>' + (m.venue === 'L' ? 'vs ' : 'en ') + esc(m.opponent) +
         '<div class="lbl2">' + (m.venue === 'L' ? 'de local' : 'de visitante') + (m.league ? ' · ' + esc(m.league) : '') + '</div></td><td class="' + rc + '"><b>' +
         m.result + '</b> ' + m.score + '</td><td>' + (m.corners ? m.corners[0] + ' - ' + m.corners[1] : '—') + '</td><td>' +
         (m.cards === null ? '—' : m.cards) + '</td><td>' + (m.xi ? '<a class="xi" onclick="toggleXi(this)">once ▸</a>' : '') + '</td></tr>';
    if (m.xi) h += '<tr style="display:none"><td colspan="6" class="small">' + esc(m.xi.formation) + ': ' + esc(m.xi.players.join(', ')) + '</td></tr>';
  }
  h += '</tbody></table>';
  var lu = t.lineup;
  h += '<div class="small" style="margin-top:8px"><b>' + (lu ? (lu.confirmed ? '✅ Alineación confirmada' : 'Alineación probable') : 'Alineación') + '</b>' +
       (lu ? ' (' + esc(lu.formation) + ')' + (lu.confirmed ? '' : ' <span class="muted">— los que más han sido titulares en sus últimos ' + lu.from +
             ' partidos; la confirmada sale ~1 h antes</span>') + ': ' + esc(lu.players.join(', ')) :
             ': <span class="muted">sin datos de alineaciones para este equipo</span>') + '</div>';
  return h + '</div>';
}
function statsHtml(d){
  var h = '<div class="statsgrid">' + teamBlock(d.home) + teamBlock(d.away) + '</div>';
  h += '<div class="tname" style="margin-top:12px">Entre ellos</div>';
  if (!d.h2h.length) return h + '<div class="muted small">Sin partidos entre ellos en la base.</div>';
  h += '<table class="mini"><thead><tr><th>Fecha</th><th>Partido</th><th>Marcador</th><th>Córners</th></tr></thead><tbody>';
  for (var i = 0; i < d.h2h.length; i++) {
    var x = d.h2h[i];
    h += '<tr><td>' + shortDate(x.date) + '</td><td>' + esc(x.match) + '</td><td><b>' + x.score + '</b></td><td>' +
         (x.corners ? x.corners[0] + ' - ' + x.corners[1] : '—') + '</td></tr>';
  }
  return h + '</tbody></table>';
}
function loadStats(id, key, date){
  var box = el(id), sk = statsKey(key, date);
  if (!box) return;
  if (_statsCache[sk]) { box.innerHTML = _statsCache[sk]; return; }
  if (box.getAttribute('data-loading')) return;
  box.setAttribute('data-loading', '1');
  box.innerHTML = '<div class="muted small">Cargando estadísticas…</div>';
  var parts = String(key).split(' vs ');
  getJSON('/api/match?home=' + encodeURIComponent(parts[0]) + '&away=' + encodeURIComponent(parts[1] || '') +
          '&date=' + encodeURIComponent(date), function(d){
    _statsCache[sk] = statsHtml(d);
    var b = el(id);
    if (b) { b.innerHTML = _statsCache[sk]; b.removeAttribute('data-loading'); }
  }, function(msg){ box.innerHTML = '<div class="muted small">' + esc(msg) + '</div>'; box.removeAttribute('data-loading'); });
}

// ── ¿Quién tuvo razón? (v1.10.0): el programa o el mercado, el que le dio
// más probabilidad a lo que pasó ──
function whoHtml(w){
  return w === 'programa' ? '<span class="who p">🤖 Programa</span>' :
         w === 'mercado' ? '<span class="who m">🏦 Mercado</span>' : '<span class="muted">—</span>';
}
function whyText(r){
  if (!r.winner) return r.result_key === 'pending' ? 'Todavía no se juega (o no tiene resultado).' :
    'Sin comparación: devolución o falta la probabilidad del mercado.';
  var happened = r.result_key === 'win' || r.result_key === 'half_win';
  return (happened ? '✅ Pasó' : '❌ No pasó') + ': ' + esc(r.label) + '. El programa le daba <b>' + p100(r.model) +
         '</b> y el mercado <b>' + p100(r.market) + '</b>. Tuvo razón <b>el ' + r.winner + '</b>: ' +
         (happened ? 'le dio más probabilidad a lo que pasó.' : 'le dio menos probabilidad a algo que no pasó.');
}

function renderToday(){
  var today = mxday(), list = [], html = '';
  for (var i = 0; i < _matches.length; i++) if (mxday(_matches[i].match_date) === today) list.push(i);
  for (var j = 0; j < list.length; j++) html += matchHtml(_matches[list[j]], list[j]);
  el('todaylist').innerHTML = html || '<div class="card muted">Hoy ya no quedan partidos por jugar. Mira la pestaña <b>Próximos días</b>.</div>';
  renderVerdict(list);
}

function renderVerdict(list){
  var real = [], good = [], maybe = [];
  for (var i = 0; i < list.length; i++) {
    var m = _matches[list[i]], st = matchState(m)[1];
    if (m.real_bet) real.push(m);
    else if (st.indexOf('Vale la pena') >= 0) good.push(m);
    else if (st.indexOf('Revisa') >= 0) maybe.push(m);
  }
  var v = el('verdict'), items = function(arr){
    var s = '';
    for (var k = 0; k < arr.length; k++) {
      var pk = arr[k].pick || {};
      s += '<br>• <b>' + esc(arr[k].match) + '</b> (' + mxtime(arr[k].match_date) + '): ' + esc(pk.label || '') +
           (pk.min_us ? ' — en PlayDoit a ' + esc(pk.min_us) + ' o mejor' : '');
    }
    return s;
  };
  var alerts = 0;
  for (var a = 0; a < list.length; a++) {
    var lu = _lineups[_matches[list[a]].match];
    if (lu && lu.level === 'alert') alerts++;
  }
  var alertNote = alerts ? '<br><span style="color:var(--yellow)">⚠️ Alerta de alineaciones en ' + alerts + ' partido' +
    (alerts > 1 ? 's' : '') + ': toca el partido para ver el detalle y el momio mínimo.</span>' : '';
  if (real.length) {
    v.className = 'verdict yes';
    v.innerHTML = '<div class="big">✅ Sí: tienes ' + real.length + ' apuesta' + (real.length > 1 ? 's' : '') + ' real' + (real.length > 1 ? 'es' : '') + ' hoy</div>' + items(real).slice(4) + alertNote;
  } else if (good.length) {
    v.className = 'verdict yes';
    v.innerHTML = '<div class="big">✅ Hay ' + good.length + ' partido' + (good.length > 1 ? 's' : '') + ' que vale' + (good.length > 1 ? 'n' : '') + ' la pena</div>' +
                  'Solo si PlayDoit paga el mínimo o más (mándale una captura al bot para confirmarlo):' + items(good) + alertNote;
  } else if (!list.length) {
    v.className = 'verdict no';
    v.innerHTML = '<div class="big">Hoy ya no quedan partidos</div><span class="muted">Los siguientes están en la pestaña Próximos días.</span>';
  } else {
    v.className = 'verdict no';
    v.innerHTML = '<div class="big">Hoy no apuestes</div>' +
      'Revisamos ' + list.length + ' partido' + (list.length > 1 ? 's' : '') + ' de hoy y en ninguno PlayDoit paga lo suficiente para tener ventaja' +
      (maybe.length ? ' (en ' + maybe.length + ' solo el mejor momio de Europa llega al mínimo)' : '') + '. ' +
      '<span class="muted">Es lo normal: el sistema solo arriesga dinero cuando le gana a Pinnacle por 2 puntos.</span>' + alertNote;
  }
}

function renderNext(){
  var today = mxday(), only = el('onlyvalue') && el('onlyvalue').checked, html = '', lastDay = '', n = 0;
  for (var i = 0; i < _matches.length; i++) {
    var m = _matches[i], d = mxday(m.match_date);
    if (d === today) continue;
    if (only && matchState(m)[0] === 'no') continue;
    if (d !== lastDay) { html += '<div class="day">' + mxdaylabel(m.match_date) + '</div>'; lastDay = d; }
    html += matchHtml(m, i);
    n++;
  }
  el('nextlist').innerHTML = n ? html : '<div class="card muted">' + (only ? 'Ningún partido de los próximos días vale la pena por ahora.' : 'Sin partidos analizados en los próximos días.') + '</div>';
}

function loadPredictions(){
  getJSON('/api/predictions', function(d){
    _matches = d.matches || [];
    renderToday();
    renderNext();
    loadLineups();
  }, function(msg){
    el('todaylist').innerHTML = '<div class="card muted">' + esc(msg) + '</div>';
    el('nextlist').innerHTML = '<div class="card muted">' + esc(msg) + '</div>';
    el('verdict').innerHTML = '<div class="big">Sin datos del programa</div><span class="muted">' + esc(msg) + '</span>';
  });
}

// ── Alineaciones (Sistema, y una línea en cada partido de hoy) ──
function _luTeam(t){
  if (!t || !t.published) return '<span class="muted">sin publicar</span>';
  var out = esc(t.formation || '');
  if (t.known === false) return out + ' <span class="muted">(sin historial)</span>';
  var miss = t.missing || [];
  if (!miss.length) return out + ' · once habitual';
  var names = [];
  for (var i = 0; i < miss.length; i++) names.push(esc(miss[i].name) + (miss[i].bench ? ' <span class="muted">(banca)</span>' : ''));
  return out + ' · faltan ' + miss.length + ': ' + names.join(', ');
}
function loadLineups(){
  var body = el('lubody');
  getJSON('/api/lineups', function(d){
    var html = '';
    _lineups = {};
    for (var i = 0; i < d.matches.length; i++) {
      var m = d.matches[i];
      _lineups[m.match] = m;
      var color = m.level === 'alert' || m.level === 'weak' ? 'var(--yellow)' : m.level === 'pending' ? 'var(--muted)' : 'var(--text)';
      html += '<tr><td>' + mxdate(m.match_date) + '</td><td>' + esc(m.match) + '<div class="lbl2">' + esc(m.league) +
              '</div></td><td>' + _luTeam(m.home) + '</td><td>' + _luTeam(m.away) +
              '</td><td style="color:' + color + '">' + esc(m.text) + '</td></tr>';
    }
    body.innerHTML = html || emptyRow(5, 'Sin partidos hoy');
    if (_matches.length) renderToday();
  }, function(msg){ body.innerHTML = emptyRow(5, msg); });
}

// ── ¿Cómo vamos? (tarjetas de Hoy) ──
var _sum = { picks: undefined, real: undefined, fav: undefined, bank: undefined, right: null };
function card(lbl, val, note, cls){
  return '<div class="card"><div class="lbl">' + lbl + '</div><div class="val ' + (cls || '') + '">' + val +
         '</div><div class="note">' + note + '</div></div>';
}
function renderHowWeGo(){
  var h = '', p = _sum.picks;
  if (p === undefined) h += card('🎯 Picks del programa', '…', 'Cargando');
  else if (!p) h += card('🎯 Picks del programa', 'Todavía sin resultados', 'Se guardan desde el 21-oct. Cada pick se califica unas horas después de que termina su partido (en la corrida de la noche o en la de las 6:30 am).');
  else h += card('🎯 Picks del programa', p.won + ' ✅ · ' + p.lost + ' ❌',
                 (p.priced ? money(p.profit) + ' a 1u por pick, al momio de PlayDoit aprox.' : 'Sin momio de PlayDoit para calcular unidades') +
                 (_sum.right ? ' Tuvo razón el programa en ' + _sum.right.programa + ' y el mercado en ' + _sum.right.mercado + '.' : '') +
                 ' Detalle en <b>Cómo nos ha ido</b>.', p.priced ? (p.profit >= 0 ? 'pos' : 'neg') : '');
  var bankTxt = _sum.bank === undefined || _sum.bank === null ? '—' : _sum.bank.toFixed(1) + 'u';
  var realNote = 'Cargando';
  if (_sum.real) {
    var last = _sum.real.last;
    realNote = _sum.real.pending ? _sum.real.pending + ' apuesta(s) pendiente(s).' :
      'Sin apuestas reales desde ' + (last ? mxdate(last).slice(0, 6) : 'el 25-sep') + ': en modo recolección solo se apuesta si el momio le gana a Pinnacle, y no ha pasado.';
  }
  h += card('💰 Dinero real (bankroll)', bankTxt, realNote);
  var f = _sum.fav;
  if (f === undefined) h += card('🎯 ¿Acierta al favorito?', '…', 'Cargando');
  else if (!f) h += card('🎯 ¿Acierta al favorito?', '—', 'Sin partidos con resultado todavía.');
  else h += card('🎯 ¿Acierta al favorito?', 'Programa ' + p100(f.s.model_hit) + ' · Pinnacle ' + p100(f.s.pin_hit),
                 f.fixed ? 'Desde la corrección del modelo (' + f.s.n + ' partidos).' :
                 'Últimos 14 días, casi todo con el modelo anterior (' + f.s.n + ' partidos). El corregido se mide desde el 22-oct.');
  el('howwego').innerHTML = h;
}

// ── Cómo nos ha ido ──
function loadPicks(){
  var body = el('picklist'), k = el('pickkpis');
  getJSON('/api/picks?days=30', function(d){
    _sum.picks = d.all;
    _sum.right = d.right;
    _pickRows = d.rows;
    renderHowWeGo();
    function kcard(title, s, note){
      if (!s) return card(title, '—', note + ' Sin resultados todavía.');
      return card(title, s.won + ' ✅ · ' + s.lost + ' ❌',
                  (s.priced ? money(s.profit) + ' a 1u' + (s.roi !== null ? ' (' + pct(s.roi) + ')' : '') : 'sin momio para unidades') + '. ' + note,
                  s.priced ? (s.profit >= 0 ? 'pos' : 'neg') : '');
    }
    k.innerHTML = kcard('Todos los picks', d.all, 'Uno por partido.') +
                  kcard('Con ventaja según el programa', d.lean, 'Donde veía 3+ puntos más que el mercado.') +
                  kcard('Los que pagaban el mínimo en PlayDoit', d.value, 'Los que habrías apostado.') +
                  card('¿Quién tuvo razón?', '🤖 ' + d.right.programa + ' · 🏦 ' + d.right.mercado,
                       'Programa contra mercado: el que le dio más probabilidad a lo que pasó.');
    var html = '';
    for (var i = 0; i < d.rows.length; i++) {
      var r = d.rows[i];
      html += '<tr class="clickable" onclick="togglePick(' + i + ')"><td>' + mxdate(r.match_date) + '</td><td>' + esc(r.match) +
              '<div class="lbl2">' + esc(r.league) + '</div></td><td>' +
              esc(r.label) + (r.lean ? '' : ' <span class="muted small">(lo más probable)</span>') + '</td><td>' +
              p100(r.model) + ' vs ' + p100(r.market) + '</td><td>' + esc(r.playdoit_us || '—') +
              '</td><td>' + resultPill(r.result_key, r.result) + '</td><td>' + whoHtml(r.winner) + '</td><td>' +
              (r.profit === null ? '' : '<span class="' + (r.profit >= 0 ? 'pos' : 'neg') + '">' + money(r.profit) + '</span>') + '</td></tr>' +
              '<tr id="pd' + i + '" style="display:none"><td colspan="8"><div class="small">' + whyText(r) +
              '</div><div class="stats" id="pds' + i + '"></div></td></tr>';
    }
    body.innerHTML = html || emptyRow(8, 'Sin picks con resultado todavía');
  }, function(msg){
    _sum.picks = null;
    renderHowWeGo();
    k.innerHTML = card('Picks del programa', 'Todavía sin resultados', esc(msg));
    body.innerHTML = emptyRow(8, msg);
  });
}
var _pickRows = [];
function togglePick(i){
  var row = el('pd' + i), r = _pickRows[i];
  if (!row || !r) return;
  var show = row.style.display === 'none';
  row.style.display = show ? '' : 'none';
  if (show && r.key) loadStats('pds' + i, r.key, r.match_date);
}

function loadScoreboard(){
  getJSON('/api/scoreboard', function(d){
    var html = '', pending = 0;
    for (var i = 0; i < d.real.length; i++) {
      var r = d.real[i];
      html += '<tr><td>' + esc(r.label) + '</td><td>' + r.n + '</td><td>' + r.won + '</td><td>' + r.lost + '</td><td>' + r.staked.toFixed(2) +
              'u</td><td class="' + (r.profit >= 0 ? 'pos' : 'neg') + '">' + (r.n ? money(r.profit) : '—') + '</td><td>' + r.cancelled + '</td></tr>';
    }
    el('realbody').innerHTML = html || emptyRow(7, 'Sin apuestas reales');
    _sum.real = { last: d.last_real_bet, pending: _sum.real ? _sum.real.pending : 0 };
    renderHowWeGo();
    var wh = '';
    for (var j = d.weeks.length - 1; j >= 0; j--) {
      var w = d.weeks[j];
      wh += '<tr><td>' + weekLabel(w.week) + '</td><td>' + (w.old_model ? '<span class="muted">con error</span>' : '<b>corregido</b>') +
            '</td><td>' + w.n + '</td><td>' + w.won + '</td><td>' + w.lost + '</td><td class="' + (w.profit_1u >= 0 ? 'pos' : 'neg') + '">' +
            money(w.profit_1u) + '</td></tr>';
    }
    el('weekbody').innerHTML = wh || emptyRow(6, 'Sin propuestas resueltas todavía');
  }, function(msg){ el('realbody').innerHTML = emptyRow(7, msg); el('weekbody').innerHTML = emptyRow(6, msg); });
}

function loadHistory(){
  var body = el('histbody'), k = el('histkpis');
  getJSON('/api/predictions/history?days=14', function(d){
    _sum.fav = d.fixed ? { s: d.fixed, fixed: true } : (d.all ? { s: d.all, fixed: false } : null);
    renderHowWeGo();
    function cards(sm, title){
      if (!sm) return '';
      return card(title + ' · acertó el favorito', 'Programa ' + p100(sm.model_hit) + ' · Pinnacle ' + p100(sm.pin_hit), sm.n + ' partidos.') +
             card(title + ' · probabilidad que le dio a lo que pasó', 'Programa ' + p100(sm.model_p_actual) + ' · Pinnacle ' + p100(sm.pin_p_actual),
                  'Más alto es mejor.');
    }
    k.innerHTML = (d.fixed ? cards(d.fixed, 'Desde la corrección') : card('Desde la corrección', '—', 'El modelo corregido se mide desde el 22-oct.')) +
                  cards(d.all, 'Últimos 14 días');
    var html = '';
    for (var i = 0; i < d.rows.length; i++) {
      var r = d.rows[i];
      html += '<tr><td>' + mxdate(r.match_date) + (r.fixed ? '' : ' <span class="muted" title="Antes de la corrección del modelo">·</span>') +
              '</td><td>' + esc(r.match) + '<div class="lbl2">' + esc(r.league) + '</div></td><td>' + r.score + ' (' + SIDE_ES[r.outcome] +
              ')</td><td>' + _p3(r.model) + '</td><td>' + _p3(r.pin) + '</td><td>' + (r.model_hit ? '✅' : '❌') +
              '</td><td>' + (r.pin_hit ? '✅' : '❌') + '</td></tr>';
    }
    body.innerHTML = html || emptyRow(7, 'Sin partidos con resultado todavía');
  }, function(msg){ _sum.fav = null; renderHowWeGo(); body.innerHTML = emptyRow(7, msg); k.innerHTML = ''; });
}
function _p3(arr){
  if (!arr) return '—';
  var out = [];
  for (var i = 0; i < arr.length; i++) out.push(arr[i] === null || arr[i] === undefined ? '—' : Math.round(arr[i] * 100));
  return out.join(' / ');
}

function loadProposals(){
  var body = el('propbody'), sum = el('propsum');
  getJSON('/api/proposals?days=14', function(d){
    var sh = '';
    for (var i = 0; i < d.summary.length; i++) {
      var g = d.summary[i];
      sh += '<tr><td>' + esc(g.decision) + '</td><td>' + g.n + '</td><td>' + g.won + '</td><td class="' +
            (g.profit_1u >= 0 ? 'pos' : 'neg') + '">' + money(g.profit_1u) + '</td><td>' + pct(g.roi) + '</td></tr>';
    }
    sum.innerHTML = sh || emptyRow(5, 'Todavía sin propuestas resueltas');
    var html = '';
    for (var j = 0; j < d.rows.length; j++) {
      var r = d.rows[j];
      var dec = esc(r.decision) + (r.fake_odds ? ' <span class="muted" title="Momio de prueba, no real">(momio de prueba)</span>' : '');
      html += '<tr><td>' + mxdate(r.match_date) + '</td><td>' + esc(r.match) + '</td><td>' + esc(r.market) + '</td><td>' +
              (r.probability === null ? '—' : p100(r.probability)) + '</td><td>' + (r.odds_us || '—') +
              '</td><td>' + (r.min_us || '—') + '</td><td>' + dec + '</td><td>' + resultPill(r.result_key, r.result) +
              '</td><td>' + whoHtml(r.winner) + '</td><td>' + (r.profit_1u === null ? '' : money(r.profit_1u)) + '</td></tr>';
    }
    body.innerHTML = html || emptyRow(10, 'Sin propuestas');
  }, function(msg){ body.innerHTML = emptyRow(10, msg); sum.innerHTML = ''; });
}

// ── Dinero real ──
function loadKpis(){
  getJSON('/api/kpis', function(d){
    _sum.bank = d.bankroll;
    _sum.real = { last: _sum.real ? _sum.real.last : null, pending: d.pending };
    renderHowWeGo();
    el('kpis').innerHTML =
      card('Bankroll', d.bankroll === null ? '—' : d.bankroll.toFixed(1) + 'u', 'Empezó en 100u.') +
      card('Ganancia (' + (d.window || '90d') + ')', money(d.profit), 'Suma de lo ganado y perdido.', d.profit >= 0 ? 'pos' : 'neg') +
      card('Rendimiento', pct(d.roi), 'Ganancia ÷ lo apostado.', d.roi >= 0 ? 'pos' : 'neg') +
      card('Acierto', d.win_rate === null ? '—' : (d.win_rate * 100).toFixed(1) + '%', d.resolved + ' apuestas resueltas.') +
      card('Pendientes', d.pending, 'Apuestas reales esperando resultado.') +
      card('CLV medio', d.clv_avg === null ? '—' : (d.clv_avg >= 0 ? '+' : '') + (d.clv_avg * 100).toFixed(2) + '%',
           'Cuánto mejor fue tu momio que el de cierre (' + d.clv_n + ' apuestas). Positivo = buena señal.', d.clv_avg >= 0 ? 'pos' : 'neg');
    el('sub').textContent = 'Actualizado: ' + new Date().toLocaleString('es-MX') + ' · solo lectura';
  }, function(m){ err('Indicadores: ' + m); });
}

// Gráficas: se dibujan la primera vez que se ven (Chart.js v2 no dibuja
// en un contenedor oculto)
function cctx(id){ return el(id).getContext('2d'); }
var _equityData = null;
function _withEquity(fn){
  if (_equityData) { fn(_equityData); return; }
  getJSON('/api/equity', function(d){ _equityData = d; fn(d); }, function(m){ err('Curva: ' + m); });
}
function drawCharts(ids){
  for (var i = 0; i < ids.length; i++) {
    var id = ids[i];
    if (_chartsDrawn[id]) continue;
    _chartsDrawn[id] = true;
    if (id === 'bankchart') _withEquity(function(d){
      var bank = [];
      for (var k = 0; k < d.cumulative.length; k++) bank.push(100 + d.cumulative[k]);
      new Chart(cctx('bankchart'), { type: 'line',
        data: { labels: d.dates, datasets: [{ data: bank, borderWidth: 2, pointRadius: 0, borderColor: '#eab308' }] },
        options: { legend: { display: false }, scales: { xAxes: [{ ticks: { maxTicksLimit: 6 } }] } } });
    });
    else if (id === 'daily') _withEquity(function(d){
      var colors = [];
      for (var k = 0; k < d.daily.length; k++) colors.push(d.daily[k] >= 0 ? '#22c55e' : '#ef4444');
      new Chart(cctx('daily'), { type: 'bar',
        data: { labels: d.daily_dates, datasets: [{ data: d.daily, backgroundColor: colors }] },
        options: { legend: { display: false }, scales: { xAxes: [{ ticks: { maxTicksLimit: 10 } }] } } });
    });
    else if (id === 'equity') _withEquity(function(d){
      new Chart(cctx('equity'), { type: 'line',
        data: { labels: d.dates, datasets: [{ data: d.cumulative, borderWidth: 2, pointRadius: 0, borderColor: '#3b82f6', fill: true, backgroundColor: 'rgba(59,130,246,.08)' }] },
        options: { legend: { display: false }, scales: { xAxes: [{ ticks: { maxTicksLimit: 8 } }] } } });
    });
    else if (id === 'clvchart') loadClv();
    else if (id === 'bymarket') loadBy('market', 'bymarket');
    else if (id === 'byleague') loadBy('league', 'byleague');
  }
}

function loadBy(dim, id){
  getJSON('/api/by/' + dim, function(d){
    var colors = [];
    for (var i = 0; i < d.roi.length; i++) colors.push(d.roi[i] >= 0 ? '#22c55e' : '#ef4444');
    new Chart(cctx(id), { type: 'horizontalBar',
      data: { labels: d.labels, datasets: [{ label: 'Rendimiento %', data: d.roi, backgroundColor: colors }] },
      options: { legend: { display: false },
        tooltips: { callbacks: { afterLabel: function(c){ return 'n=' + d.n[c.index] + ' · acierto ' + d.wr[c.index] + '%'; } } },
        scales: { xAxes: [{ ticks: { callback: function(v){ return v + '%'; } } }] } } });
  }, function(m){ err(dim + ': ' + m); });
}

// Filtros de mercado y liga de la tabla de apuestas
function loadFilters(){
  var dims = [['market', 'fmarket'], ['league', 'fleague']];
  for (var k = 0; k < dims.length; k++) {
    (function(dim, sid){
      getJSON('/api/by/' + dim, function(d){
        // valor = clave cruda ("under_3.5"), texto = nombre visible: la API
        // filtra por la clave (con el nombre, la tabla quedaba vacía)
        var keys = d.keys || d.labels, sel = el(sid);
        for (var j = 0; j < d.labels.length; j++) sel.add(new Option(d.labels[j], keys[j]));
      }, function(){});
    })(dims[k][0], dims[k][1]);
  }
}

function loadClv(){
  getJSON('/api/clv', function(d){
    var ptsG = [], ptsR = [];
    for (var i = 0; i < d.x.length; i++) {
      if (d.wins[i]) ptsG.push({ x: d.x[i], y: d.clv[i] }); else ptsR.push({ x: d.x[i], y: d.clv[i] });
    }
    new Chart(cctx('clvchart'), { type: 'scatter', data: { datasets: [
      { label: 'ganada', data: ptsG, backgroundColor: '#22c55e' },
      { label: 'perdida', data: ptsR, backgroundColor: '#ef4444' }
    ] }, options: { legend: { display: false }, scales: { yAxes: [{ ticks: { callback: function(v){ return (v * 100).toFixed(1) + '%'; } } }] } } });
  }, function(){ /* sin datos de CLV aún */ });
}

// ── Tabla de apuestas ──
var _betsRaw = [];
function loadBets(){
  var q = 'status=' + encodeURIComponent(el('fstatus').value) +
          '&market=' + encodeURIComponent(el('fmarket').value) +
          '&league=' + encodeURIComponent(el('fleague').value) +
          '&limit=150';
  var body = el('betsbody');
  getJSON('/api/bets?' + q, function(d){
    _betsRaw = d.bets;
    var html = '';
    for (var i = 0; i < d.bets.length; i++) {
      var b = d.bets[i];
      var rk = String(b.result_key || b.result || 'pending').toLowerCase();
      var off = rk === 'cancelled';   // cancelada antes del partido: no se apuesta
      var prof = (rk === 'pending' || off) ? '' : money(b.profit || 0);
      var clv = b.clv === '' ? '—' : (Number(b.clv) * 100).toFixed(1) + '%';
      // Formato americano, como PlayDoit (+128 / -140). La mejor cuota
      // europea es solo referencia; lo que decide es el mínimo de PlayDoit.
      var minUs = off ? '<span title="Cancelada antes del partido: ya no tiene valor">no apostar</span>' :
                  b.min_us ? '<span title="' + (b.min_hint || '') + '">' + b.min_us + ' o mejor</span>' : '—';
      html += '<tr><td>' + mxdate(b.match_date) + '</td><td>' + b.match + '</td><td>' +
              (b.league || '') + '</td><td>' + b.market + '</td><td>' +
              (b.probability * 100).toFixed(0) + '%</td><td title="decimal ' + b.odds + '">' +
              (b.odds_us || '—') + '</td><td>' + minUs + '</td><td>' +
              b.stake + 'u</td><td>' + resultPill(rk, b.result) + '</td><td>' + prof + '</td><td>' + clv + '</td></tr>';
    }
    body.innerHTML = html || emptyRow(11, 'Sin apuestas');
  }, function(m){ body.innerHTML = emptyRow(11, m); });
}

// ── Goleadores ──
function loadScorers(){
  var body = el('scorersbody');
  getJSON('/api/scorers', function(d){
    var html = '';
    for (var i = 0; i < d.picks.length; i++) {
      var p = d.picks[i];
      html += '<tr><td>' + mxdate(p.match_date) + '</td><td>' + p.match + '</td><td>' + p.player +
              '</td><td>' + p.team + '</td><td>' + (p.probability * 100).toFixed(0) + '%</td><td>' +
              (p.fair_us || '—') + '</td><td>' + resultPill(p.result_key || p.result, p.result) + '</td></tr>';
    }
    body.innerHTML = html || emptyRow(7, 'Sin goleadores aún');
  }, function(){ body.innerHTML = emptyRow(7, 'Sin goleadores aún'); });
}

// ── Panel de control GitHub ──
function loadGh(){
  var msg = el('ghmsg'), body = el('ghbody');
  getJSON('/api/gh/status', function(d){
    if (!d.configured) {
      msg.innerHTML = '⚠️ ' + d.msg + ' — créalo en github.com/settings/tokens (fine-grained, permiso <b>Actions: Read and write</b> del repo) y agrégalo a tu .env como GH_TOKEN=xxx';
      body.innerHTML = '';
      return;
    }
    if (!d.ok) { msg.textContent = '⚠️ ' + d.msg; return; }
    msg.textContent = 'Últimas corridas del sistema:';
    var html = '';
    for (var i = 0; i < d.runs.length; i++) {
      var r = d.runs[i];
      var icon = r.status !== 'completed' ? '🔄' : (r.conclusion === 'success' ? '✅' : '❌');
      html += '<tr><td>' + r.name + '</td><td>' + icon + ' ' + r.status + '</td><td>' +
              (r.conclusion || '—') + '</td><td>' + r.created + '</td><td><a href="' + r.url +
              '" target="_blank" style="color:var(--blue)">ver</a></td></tr>';
    }
    body.innerHTML = html;
  }, function(m){ msg.textContent = '⚠️ ' + m; });
}

function dispatch(wf){
  var msg = el('ghmsg');
  msg.textContent = 'Disparando ' + wf + '…';
  try {
    var x = new XMLHttpRequest();
    x.open('POST', '/api/gh/dispatch/' + wf, true);
    x.setRequestHeader('Content-Type', 'application/json');
    x.onreadystatechange = function(){
      if (x.readyState !== 4) return;
      var d = null;
      try { d = JSON.parse(x.responseText); } catch(e2) {}
      if (d && d.ok) { msg.textContent = '✅ ' + d.msg + ' — aparece abajo en segundos'; loadGh(); }
      else if (x.status === 0) { msg.textContent = '⚠️ ' + OFFLINE_MSG; serverDown(); }
      else { msg.textContent = '⚠️ ' + ((d && d.msg) || ('HTTP ' + x.status)); }
    };
    x.send();
  } catch(e) { msg.textContent = '⚠️ ' + e.message; }
}

// ── Diagnóstico IA ──
function loadNarrative(){
  var sec = el('narrsec');
  getJSON('/api/narrative', function(d){
    sec.style.display = 'block';
    el('narrtext').textContent = d.narrative;
    el('narreng').textContent = 'Generado por ' + d.engine + ' · ' + d.created;
  }, function(){ sec.style.display = 'none'; });
}

function genNarrative(){
  var b = el('genbtn');
  b.textContent = '⏳ Generando (~1 min)…';
  b.disabled = true;
  var x = new XMLHttpRequest();
  x.open('POST', '/api/narrative/generate', true);
  x.onreadystatechange = function(){
    if (x.readyState !== 4) return;
    b.textContent = '🧠 Generar diagnóstico ahora';
    b.disabled = false;
    if (x.status === 200) loadNarrative();
  };
  x.send();
}

// ── Export CSV ──
function exportCSV(){
  if (!_betsRaw.length) { alert('Sin apuestas que exportar'); return; }
  var cols = [];
  for (var k in _betsRaw[0]) cols.push(k);
  var lines = [cols.join(',')];
  for (var i = 0; i < _betsRaw.length; i++) {
    var vals = [];
    for (var j = 0; j < cols.length; j++) {
      var v = String(_betsRaw[i][cols[j]] === null || _betsRaw[i][cols[j]] === undefined ? '' : _betsRaw[i][cols[j]]);
      vals.push(v.indexOf(',') >= 0 ? '"' + v + '"' : v);
    }
    lines.push(vals.join(','));
  }
  var blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
  var a = document.createElement('a');
  if (window.navigator && window.navigator.msSaveOrOpenBlob) {
    window.navigator.msSaveOrOpenBlob(blob, 'apuestas.csv');
  } else {
    a.href = URL.createObjectURL(blob);
    a.download = 'apuestas.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }
}

// ── Versión + banner de actualización ──
function loadVersion(){
  getJSON('/api/version', function(d){
    el('ver').textContent = 'v' + d.current;
    if (d.update_available) {
      var u = el('upd');
      u.style.display = 'block';
      u.innerHTML = '🔄 Nueva versión disponible: <b>v' + d.latest + '</b> (tienes v' + d.current + ') — <a href="' + d.release_url + '" target="_blank" style="color:var(--green)">descargar</a>';
    }
  }, function(){});
}

// ── Auto-refresco (datos; las gráficas se redibujan con F5) ──
var _refreshTimer = null;
function loadData(){
  loadKpis(); loadPredictions(); loadPicks(); loadScoreboard(); loadHistory(); loadProposals();
  loadBets(); loadScorers(); loadGh();
}
function setRefresh(mins){
  if (_refreshTimer) clearInterval(_refreshTimer);
  if (mins > 0) _refreshTimer = setInterval(loadData, mins * 60000);
}

// ── ARRANQUE ──
// Layout de gráficas (r14-fix): Chart.js v2 necesita contenedor con altura
// fija y position:relative, o el canvas se desborda sobre lo de abajo.
if (window.Chart && Chart.defaults && Chart.defaults.global) {
  Chart.defaults.global.maintainAspectRatio = false;
  Chart.defaults.global.responsive = true;
}
// Filtros: hasta el 24-sep-26 no tenían evento de cambio.
function _onChange(id, fn){ var x = el(id); if (x) x.onchange = fn; }
_onChange('fstatus', loadBets);
_onChange('fmarket', loadBets);
_onChange('fleague', loadBets);
_onChange('onlyvalue', renderNext);
_onChange('frefresh', function(){
  var mins = parseInt(el('frefresh').value, 10) || 0;
  _storeSet('refreshMins', String(mins));
  setRefresh(mins);
});
(function(){
  var sel = el('frefresh');
  if (sel) sel.value = _storeGet('refreshMins', '5');
})();

renderHowWeGo();
showTab((location.hash || '').replace('#', '') || _storeGet('tab', 'hoy'));
loadVersion();
loadData();
loadFilters();
loadNarrative();
setRefresh(parseInt(_storeGet('refreshMins', '5'), 10) || 0);   // auto-refresco (5 min por defecto)
