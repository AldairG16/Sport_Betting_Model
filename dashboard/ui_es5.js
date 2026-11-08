// ============================================================
// Dashboard JS — ES5 universal (compatible con cualquier navegador)
// v2.0.0 (22-oct-26): partidos, equipos y gráficas. El dueño: "necesito un
// mejor dashboard, que sea entendible, sin tanto texto innecesario… quiero
// ver solo los partidos, equipos, gráficas y los partidos anteriores desde
// septiembre y los próximos". Poco texto: barras, iconos y tooltips.
// ============================================================

function _storeGet(key, dflt){ try { return localStorage.getItem(key) || dflt; } catch(e){ return dflt; } }
function _storeSet(key, val){ try { localStorage.setItem(key, val); } catch(e){} }
window.onerror = function(msg, src, line){
  var e = document.getElementById('err');
  e.style.display = 'block';
  e.textContent = '⚠️ Error interno: ' + msg + ' (línea ' + line + ')';
};

// ── Helpers ──
function el(id){ return document.getElementById(id); }
function esc(s){ return String(s === null || s === undefined ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function money(v){ return (v >= 0 ? '+' : '') + Number(v).toFixed(2) + 'u'; }
function p100(v){ return v === null || v === undefined ? '—' : Math.round(v * 100) + '%'; }
function p2(n){ return (n < 10 ? '0' : '') + n; }
var MESES = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'];
var DIAS = ['Domingo','Lunes','Martes','Miércoles','Jueves','Viernes','Sábado'];
// UTC → hora de México (UTC-6 fijo desde 2022)
function mx(s){ var t = new Date(s); return isNaN(t.getTime()) ? null : new Date(t.getTime() - 6 * 3600000); }
function mxtime(s){ var d = mx(s); return d ? p2(d.getUTCHours()) + ':' + p2(d.getUTCMinutes()) : ''; }
function mxday(s){ var d = mx(s === undefined ? new Date().getTime() : s); return d ? d.getUTCFullYear() + '-' + p2(d.getUTCMonth() + 1) + '-' + p2(d.getUTCDate()) : ''; }
// "2026-10-22" → "Jueves 22 oct"; hoy y mañana con nombre
function dayLabel(ymd){
  if (ymd === mxday()) return 'Hoy';
  if (ymd === mxday(new Date().getTime() + 86400000)) return 'Mañana';
  var p = ymd.split('-'), d = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2], 12));
  return DIAS[d.getUTCDay()] + ' ' + (+p[2]) + ' ' + MESES[+p[1] - 1];
}
function shortDate(ymd){ var p = String(ymd).slice(0, 10).split('-'); return (+p[2]) + ' ' + MESES[+p[1] - 1]; }
function err(msg){ var e = el('err'); e.style.display = 'block'; e.textContent = '⚠️ ' + msg; }

// ── Programa caído / versión nueva ──
// El 21-oct el dueño siguió viendo la página vieja en una pestaña abierta
// antes de actualizar: si el programa cambia de versión, la página se recarga.
var _offTimer = null;
function serverDown(){
  var b = el('offline');
  if (!b) {
    b = document.createElement('div');
    b.id = 'offline';
    b.style.cssText = 'position:sticky;top:0;z-index:10;background:#3a2a14;color:#fbbf24;padding:8px 12px;border-radius:8px;margin:8px 0;font-size:.85rem';
    document.body.insertBefore(b, document.body.firstChild);
  }
  b.textContent = '⏳ El programa se está reiniciando. La página vuelve sola.';
  if (!_offTimer) _offTimer = setInterval(checkServer, 5000);
}
function checkServer(){
  var x = new XMLHttpRequest();
  x.open('GET', '/api/health?t=' + new Date().getTime(), true);
  x.onreadystatechange = function(){
    if (x.readyState !== 4) return;
    if (x.status === 0) { serverDown(); return; }
    var d = null;
    try { d = JSON.parse(x.responseText); } catch(e) {}
    if (_offTimer || (d && window.APP_V && d.version && d.version !== window.APP_V)) location.reload();
  };
  x.send();
}
setInterval(checkServer, 60000);

// Consultas en curso y la última respuesta de cada una: el botón "Actualizar"
// sabe cuándo terminó y si algo cambió (v2.5.3)
var _pending = 0, _lastResp = {}, _changed = 0;
function getJSON(url, ok, fail){
  var key = url.replace(/[?&]fresh=1/, ''), sent = false;
  try {
    var x = new XMLHttpRequest();
    x.open('GET', url, true);
    x.onreadystatechange = function(){
      if (x.readyState !== 4) return;
      try {
        if (x.status === 0) { serverDown(); return; }
        if (x.status !== 200) { if (fail) fail('HTTP ' + x.status); return; }
        var d;
        try { d = JSON.parse(x.responseText); } catch(e2) { if (fail) fail('JSON inválido'); return; }
        if (d.ok === false) { if (fail) fail(d.msg || 'sin datos'); return; }
        if (key in _lastResp && _lastResp[key] !== x.responseText) _changed++;
        _lastResp[key] = x.responseText;
        if (ok) ok(d);
      } finally { _pending--; refreshDone(); }     // después de ok(): sus consultas anidadas ya cuentan
    };
    _pending++; sent = true;
    x.send();
  } catch(e) {
    if (sent) { _pending--; refreshDone(); }
    if (fail) fail(e.message);
  }
}

// ── Actualizar ahora (v2.5.3, 29-oct-26) ──
// El dueño: "necesito un botón de refresh para estar monitoreando los
// resultados cuando cambia algo". Vuelve a leer el encabezado, Próximos y la
// pestaña abierta (las otras, al abrirlas) y dice a qué hora y si algo cambió.
// Solo, cada 5 min, hace lo mismo menos Equipos y Gráficas (no cierra lo que
// se está viendo).
var _refreshing = false, _firstDone = false;
function refreshAll(manual){
  if (_refreshing) return;
  _refreshing = true; _changed = 0;
  var b = el('refresh');
  if (b) { b.disabled = true; b.innerHTML = '⏳ Actualizando…'; }
  var cur = _storeGet('tab2', 'proximos');
  for (var t in _loaded) if (t !== cur) _loaded[t] = false;
  _detCache = {};
  loadHeader();
  loadUpcoming();
  if (cur === 'anteriores') loadPast(true);
  else if (cur === 'resultados') loadResults(true);
  else if (cur === 'ligas') loadLeagues(true);
  else if (cur === 'parlay') loadParlay();
  else if (manual && cur === 'equipos') loadTeams();
  else if (manual && cur === 'graficas') loadCharts(true);
  refreshDone();
}
function refreshDone(){
  if (!_refreshing || _pending > 0) return;
  _refreshing = false;
  var b = el('refresh'), n = mx(new Date().getTime()), u = el('updated');
  if (!b || !u) return;                               // HTML anterior a v2.5.3
  b.disabled = false; b.innerHTML = '🔄 Actualizar';
  // la primera carga no tiene con qué comparar: solo la hora
  u.textContent = 'Actualizado ' + p2(n.getUTCHours()) + ':' + p2(n.getUTCMinutes()) +
                  (!_firstDone ? '' : _changed ? ' · hay cambios' : ' · sin cambios');
  u.className = 'small ' + (_firstDone && _changed ? 'chg' : 'muted');
  _firstDone = true;
}

// ── Pestañas ──
var TABS = ['proximos', 'parlay', 'anteriores', 'resultados', 'ligas', 'equipos', 'graficas'], _loaded = {};
function showTab(name){
  if (TABS.indexOf(name) < 0) name = 'proximos';
  var b = el('tabs').getElementsByTagName('button');
  for (var i = 0; i < b.length; i++) b[i].className = 'tab' + (b[i].getAttribute('data-tab') === name ? ' on' : '');
  for (var j = 0; j < TABS.length; j++) el('tab-' + TABS[j]).className = 'pane' + (TABS[j] === name ? ' on' : '');
  _storeSet('tab2', name);
  if (_loaded[name]) return;
  _loaded[name] = true;
  if (name === 'anteriores') loadPast();
  else if (name === 'parlay') loadParlay();
  else if (name === 'resultados') loadResults();
  else if (name === 'ligas') loadLeagues();
  else if (name === 'equipos') loadTeams();
  else if (name === 'graficas') loadCharts();
}
(function(){
  var b = el('tabs').getElementsByTagName('button');
  for (var i = 0; i < b.length; i++) b[i].onclick = function(){ showTab(this.getAttribute('data-tab')); };
})();

// ── Barra de probabilidades (local / empate / visitante) ──
function probBar(p, thin, title){
  if (!p || p[0] === null || p[0] === undefined) return '';
  var lab = function(v){ return thin || v < 0.12 ? '' : Math.round(v * 100); };
  return '<div class="pb' + (thin ? ' thin' : '') + '" title="' + esc(title || '') + '">' +
         '<div class="h" style="width:' + (p[0] * 100) + '%">' + lab(p[0]) + '</div>' +
         '<div class="d" style="width:' + (p[1] * 100) + '%">' + lab(p[1]) + '</div>' +
         '<div class="a" style="width:' + (p[2] * 100) + '%">' + lab(p[2]) + '</div></div>';
}
function _sides(m, key){ var a = []; for (var i = 0; i < m.sides.length; i++) a.push(m.sides[i][key]); return a; }

// ── Detalle de un partido: últimos 5, alineación y entre ellos ──
var _detCache = {};
function formBadges(ms){
  var h = '<div class="form">';
  for (var i = 0; i < ms.length; i++) {
    var m = ms[i];
    h += '<span class="f ' + m.result + '"' + (m.friendly ? ' style="opacity:.45"' : '') + ' title="' +
         esc(shortDate(m.date) + ' · ' + (m.venue === 'L' ? 'vs ' : 'en ') + m.opponent + ' · ' + m.score + (m.league ? ' · ' + m.league : '')) + '">' + m.result + '</span>';
  }
  return h + '</div>';
}
function teamPanel(t){
  var h = '<div class="tp"><h3>' + esc(t.team) + '</h3>';
  if (!t.summary) return h + '<div class="muted small">Sin partidos anteriores</div></div>';
  var s = t.summary;
  // v2.3: todas las competiciones; los amistosos se ven pero no cuentan en los promedios
  h += formBadges(t.matches) + '<div class="small muted">Goles ' + s.goals_for + ' - ' + s.goals_against +
       (s.over25 !== undefined ? ' · Más de 2.5: ' + s.over25 + ' de ' + s.n + ' · Ambos anotan: ' + s.btts + ' de ' + s.n : '') +
       (s.corners_for !== null ? ' · Córners ' + s.corners_for + ' - ' + s.corners_against : '') +
       (s.cards !== null ? ' · Tarjetas ' + s.cards : '') + (s.friendlies ? ' · sin amistosos' : '') + '</div>';
  h += '<table><thead><tr><th>Fecha</th><th>Rival</th><th>Res.</th><th class="hide-sm">Comp.</th><th>Córn.</th><th>Tarj.</th></tr></thead><tbody>';
  for (var i = 0; i < t.matches.length; i++) {
    var m = t.matches[i], cls = m.result === 'G' ? 'pos' : m.result === 'P' ? 'neg' : '';
    h += '<tr' + (m.friendly ? ' style="opacity:.55"' : '') + ' title="' + esc(m.xi ? m.xi.formation + ': ' + m.xi.players.join(', ') : '') + '"><td>' + shortDate(m.date) + '</td><td>' +
         (m.venue === 'L' ? 'vs ' : '@ ') + esc(m.opponent) + '</td><td class="' + cls + '"><b>' + m.score + '</b></td><td class="hide-sm small muted">' +
         esc(m.league || '') + '</td><td>' +
         (m.corners ? m.corners[0] + '-' + m.corners[1] : '—') + '</td><td>' + (m.cards === null ? '—' : m.cards) + '</td></tr>';
  }
  h += '</tbody></table>';
  var lu = t.lineup;
  if (lu) h += '<div class="xi">' + (lu.confirmed ? '✅ ' : '👥 ') + '<b>' + esc(lu.formation) + '</b> ' + esc(lu.players.join(', ').replace(/ \(\d+ de \d+\)/g, '')) + '</div>';
  return h + '</div>';
}
// Lo que vio el programa en cada mercado (v2.4.6, 26-oct-26): por qué eligió ese pick y no otro
function marketsHtml(d){
  if (!d.markets || !d.markets.length) return '';
  var rows = '';
  for (var i = 0; i < d.markets.length; i++) {
    var m = d.markets[i], picked = d.picked && m.market === d.picked;
    var us = m.min_us === null || m.min_us === undefined ? '—' : (m.min_us > 0 ? '+' : '') + m.min_us;
    rows += '<tr' + (picked ? ' style="font-weight:bold"' : '') + '><td>' + famIcon(m.family) + ' ' + esc(m.label) +
            (picked ? ' <span class="small">← pick</span>' : '') + '</td><td>' + p100(m.model) + '</td><td>' + p100(m.market_prob) +
            (m.estimated ? '<span class="muted" title="Sin momio real de esta línea: lo de las casas se calcula de su línea de 2.5">*</span>' : '') +
            '</td><td class="' + (m.gap >= 0 ? 'pos' : 'neg') + '">' + (m.gap >= 0 ? '+' : '') + (m.gap * 100).toFixed(1) + '</td>' +
            '<td title="Para que valga la pena, PlayDoit tiene que pagar esto o más">' + us + '</td></tr>';
  }
  return '<div class="small" style="margin-top:8px"><b>Cómo ve cada apuesta</b> <span class="muted">· qué tan probable la ve el programa y qué tan probable ' +
         'la ven las casas (de cada 100). Diferencia: cuánto más (verde) o menos (rojo) la ve el programa · * sin momio real, no puede ser el pick</span>' +
         '<table><thead><tr><th>Apuesta</th><th>Programa</th><th>Casas</th><th>Diferencia</th><th>Momio mínimo en PlayDoit</th></tr></thead><tbody>' +
         rows + '</tbody></table></div>';
}
// El ángulo de goles (v2.4.7, 26-oct-26): un equipo que recibe 2+ por partido
function goalsHtml(d){
  var a = d.goals_angle;
  if (!a) return '';
  var who = [], i;
  for (i = 0; i < a.teams.length; i++) who.push(esc(a.teams[i].team) + ' recibe <b>' + a.teams[i].goals_against + '</b> goles por partido');
  var h = '<div class="small" style="margin-top:8px"><b>⚽ Goles</b> · ' + who.join(' y ') + ' (últimos ' + a.teams[0].n + ').<br>';
  var alt = a.alternative;
  if (alt && d.picked === alt.market) {
    h += 'El pick ya es de goles: <b>' + esc(alt.label) + '</b>.';
  } else if (alt) {
    h += 'Otra opción de goles: <b>' + esc(alt.label) + '</b> · el programa la ve en ' + p100(alt.model) + ' y las casas en ' + p100(alt.market_prob) +
         '. No es el pick porque el pick le saca más ventaja a las casas.';
  } else if (a.markets.length) {
    var x = [];
    for (i = 0; i < a.markets.length; i++) x.push(esc(a.markets[i].label) + ': programa ' + p100(a.markets[i].model) + ' · casas ' + p100(a.markets[i].market_prob));
    h += x.join(' · ') + '. Las casas ya cuentan con esos goles y el programa no ve nada extra: no hay pick de goles.';
  } else {
    h += 'Sin momios de goles para comparar.';
  }
  var e = a.evidence;
  if (e && e.n) h += '<br><span class="muted">En ' + Number(e.n).toLocaleString() + ' partidos parecidos, más de 2.5 goles salió ' + p100(e.hit) +
                     ' de las veces y las casas esperaban ' + p100(e.expected) + ': apostarlo solo porque un equipo recibe muchos goles dejó ' + (e.roi >= 0 ? '+' : '') + (e.roi * 100).toFixed(1) +
                     '%. Por eso el pick de goles sale solo cuando el programa ve más que las casas.</span>';
  return h + '</div>';
}
function detailHtml(d){
  var h = '<div class="two">' + teamPanel(d.home) + teamPanel(d.away) + '</div>';
  if (d.h2h && d.h2h.length) {
    var x = [];
    for (var i = 0; i < d.h2h.length; i++) x.push('<span title="' + esc(d.h2h[i].match + (d.h2h[i].league ? ' · ' + d.h2h[i].league : '')) + '">' + shortDate(d.h2h[i].date) + ' <b>' + d.h2h[i].score + '</b></span>');
    h += '<div class="h2h"><span class="muted">Entre ellos:</span> ' + x.join(' · ') + '</div>';
  }
  return h + goalsHtml(d) + marketsHtml(d);
}
function openDetail(id, key, date){
  var box = el(id), ck = key + '|' + date;
  if (!box) return;
  var row = box.parentNode;
  row.className = row.className.indexOf('open') >= 0 ? row.className.replace(' open', '') : row.className + ' open';
  if (row.className.indexOf('open') < 0) return;
  if (_detCache[ck]) { box.innerHTML = _detCache[ck]; return; }
  box.innerHTML = '<div class="muted small">Cargando…</div>';
  var p = String(key).split(' vs ');
  getJSON('/api/match?home=' + encodeURIComponent(p[0]) + '&away=' + encodeURIComponent(p[1] || '') + '&date=' + encodeURIComponent(date),
    function(d){ _detCache[ck] = detailHtml(d); var b = el(id); if (b) b.innerHTML = _detCache[ck]; },
    function(msg){ box.innerHTML = '<div class="muted small">' + esc(msg) + '</div>'; });
}

// ── Próximos ──
var _up = [], _upDay = '', _upFam = '', _alerts = {};
// v2.1: el pick es la apuesta que se registra y se mide (la misma que se ve)
var FAM_ICON = { 'Ganador': '🏆', 'Goles': '⚽', 'Ambos anotan': '🔁', 'Córners': '⛳', 'Tarjetas': '🟨' };
function famIcon(f){ return FAM_ICON[f] || '🎯'; }
function upRow(m, i){
  var pk = m.pick || {}, t = String(m.match).split(' vs ');
  var model = _sides(m, 'model'), pin = _sides(m, 'pin');
  var tip = 'Programa: ' + p100(model[0]) + ' / ' + p100(model[1]) + ' / ' + p100(model[2]) +
            '  ·  Pinnacle: ' + p100(pin[0]) + ' / ' + p100(pin[1]) + ' / ' + p100(pin[2]);
  // v2.2: todos los partidos analizados tienen pick; 🎯 = pasa la regla
  var pick = pk.label ? (pk.rule ? '🎯 ' : '') + famIcon(pk.family) + ' <b>' + esc(pk.label) + '</b>' +
             (pk.playdoit_us ? ' <span class="muted">' + esc(pk.playdoit_us) + '</span>' : '') + whyTag(pk) + lineupTag(pk, m.lineups_covered) :
             noPick(pk.reason);
  var tipPick = pk.label ? 'Qué tan probable lo ve: el programa ' + p100(pk.model) + ', las casas ' + p100(pk.market_prob) +
                ' · el momio es el aproximado de PlayDoit' +
                (pk.rule ? ' · 🎯 apuesta recomendada' : (pk.star_note || pk.reason ? ' · sin 🎯: ' + (pk.star_note || pk.reason) : '')) : (pk.reason || '');
  var al = _alerts[m.match] ? ' <span title="' + esc(_alerts[m.match]) + '">⚠️</span>' : '';
  return '<div class="m" id="u' + i + '"><div class="mr" onclick="openUp(' + i + ')">' +
         '<div class="time">' + mxtime(m.match_date) + '<span class="small muted">' + esc(m.league) + '</span></div>' +
         '<div class="t h">' + esc(t[0]) + '</div>' +
         '<div class="pbw">' + probBar(model, false, tip) + probBar(pin, true, tip) + '</div>' +
         '<div class="t">' + esc(t[1] || '') + al + '</div>' +
         '<div class="pk" title="' + esc(tipPick) + '">' + pick + '</div>' +
         '<div class="ic">' + (m.real_bet ? '<span title="Apuesta real">💰</span>' : '') + '</div></div>' +
         '<div class="det" id="ud' + i + '"></div></div>';
}
function openUp(i){ var m = _up[i]; if (m && m.key) openDetail('ud' + i, m.key, m.match_date); }
function renderUp(){
  var lg = el('uleague').value, html = '', last = '', n = 0;
  for (var i = 0; i < _up.length; i++) {
    var m = _up[i], d = mxday(m.match_date);
    if (_upDay && d !== _upDay) continue;
    if (lg && m.league !== lg) continue;
    if (_upFam && (!m.pick || m.pick.family !== _upFam)) continue;
    if (d !== last) { html += '<div class="day">' + dayLabel(d) + '</div>'; last = d; }
    html += upRow(m, i);
    n++;
  }
  el('uplist').innerHTML = n ? html : '<div class="muted">Sin partidos.</div>';
}
function renderDayBar(){
  var days = [], seen = {};
  for (var i = 0; i < _up.length; i++) { var d = mxday(_up[i].match_date); if (!seen[d]) { seen[d] = 1; days.push(d); } }
  var h = '<span class="pill' + (_upDay ? '' : ' on') + '" onclick="setDay(\'\')">Todos</span>';
  for (var j = 0; j < days.length; j++) h += '<span class="pill' + (_upDay === days[j] ? ' on' : '') + '" onclick="setDay(\'' + days[j] + '\')">' + dayLabel(days[j]) + '</span>';
  el('daybar').innerHTML = h;
}
function setDay(d){ _upDay = d; renderDayBar(); renderUp(); }
function fillSelect(id, values){
  var sel = el(id), cur = sel.value;
  while (sel.options.length > 1) sel.remove(1);
  for (var i = 0; i < values.length; i++) sel.add(new Option(values[i], values[i]));
  sel.value = cur;
}
function loadUpcoming(){
  getJSON('/api/predictions', function(d){
    _up = d.matches || [];
    var lg = {}, list = [];
    for (var i = 0; i < _up.length; i++) if (_up[i].league && !lg[_up[i].league]) { lg[_up[i].league] = 1; list.push(_up[i].league); }
    fillSelect('uleague', list.sort());
    renderDayBar();
    renderUp();
    // hoy: cuántos partidos y cuántos 🎯 (hasta v2.9.1 decía "🎯 Hoy: 6 picks" contando
    // todos los picks, aunque ninguno fuera 🎯)
    var today = mxday(), n = 0, good = 0;
    for (var j = 0; j < _up.length; j++) if (mxday(_up[j].match_date) === today) { n++; if (_up[j].pick && _up[j].pick.label && _up[j].pick.rule) good++; }
    el('chiptoday').innerHTML = n ? '📅 Hoy: ' + n + (n === 1 ? ' partido' : ' partidos') + ' · 🎯 <b>' + good + '</b> recomendado' + (good === 1 ? '' : 's') : 'Hoy: sin partidos';
    getJSON('/api/lineups', function(l){
      _alerts = {};
      for (var k = 0; k < l.matches.length; k++) if (l.matches[k].level === 'alert') _alerts[l.matches[k].match] = l.matches[k].text;
      renderUp();
    }, function(){});
  }, function(msg){ el('uplist').innerHTML = '<div class="muted">' + esc(msg) + '</div>'; });
}
el('uleague').onchange = renderUp;
el('ufam').onchange = function(){ _upFam = this.value; renderUp(); };

// Sin pick: el motivo a la vista, no solo al pasar el mouse (v2.4.1, 24-oct-26: los
// partidos de la K-League se veían vacíos sin decir por qué)
function noPick(reason){
  var r = String(reason || '');
  var txt = /poca historia/i.test(r) ? 'Sin análisis · pocos partidos de estos equipos para opinar' :
            /momio/i.test(r) ? 'Sin pick · no hay momios para comparar' :
            r ? 'Sin pick · ' + shortWhy(r) : 'Sin análisis';
  return '<span class="muted" title="' + esc(r || 'El programa no analizó este partido') + '">' + esc(txt) + '</span>';
}

// Por qué no es 🎯, en palabras normales (v2.9.2, 6-nov-26, el dueño: "necesito que el
// texto sea más amigable, no entiendo nada eso de al favorito de 65%"). Desde v2.9.2 el
// pick trae star_note: lo que le falta al favorito para ser 🎯 ("no hay favorito claro",
// "Arsenal paga muy poco (-650)"…). Los picks de antes solo traen el motivo guardado
// (reason), que aquí se traduce.
function shortWhy(reason){
  var r = String(reason || '');
  if (!r) return '';
  if (/^Poca historia/i.test(r)) return 'pocos partidos de estos equipos para opinar';
  if (/^Sin ventaja/i.test(r)) return 'el programa ve lo mismo que las casas';
  if (/demasiado/i.test(r)) return 'el programa no coincide con las casas';
  if (/se movi/i.test(r)) return 'el momio se movió en contra';
  if (/^Alineación: a /i.test(r)) return r.replace(/^Alineación: /i, '').replace(/ habituales.*$/, '');
  if (/^Alineación/i.test(r)) return 'le faltan titulares';
  if (/ligas distintas/i.test(r)) return 'equipos de ligas distintas';
  if (/^Región /i.test(r)) return 'en ' + r.split(' ')[1] + ' el programa viene perdiendo';
  if (/favorito de 65|^No es el muy favorito/i.test(r)) return 'no era el muy favorito';
  if (/momio/i.test(r)) return 'el momio no está en rango';
  return noteShort(r);
}
// Lo corto de una nota (antes de los dos puntos); con minúscula si no empieza con un equipo
function noteShort(note){
  var s = String(note || '').split(':')[0];
  return /^(No|Liga|Sin) /.test(s) ? s.charAt(0).toLowerCase() + s.slice(1) : s;
}
// Marcador de ESPN del día (v2.4.4): en vivo o final, provisional hasta la noche
function liveTag(lv){
  if (!lv) return '';
  return lv.status === 'live' ? ' <span class="small muted" title="En juego (ESPN)">● ' + esc(lv.clock || 'en vivo') + '</span>' :
         ' <span class="small muted" title="Marcador final de ESPN; se confirma en la corrida de la noche">final</span>';
}
// El pick antes o después de la alineación (v2.5.0, 26-oct-26: "ponemos los picks
// mucho antes de que la lista de alineaciones esté lista"). Sale ~1 h antes y el pick
// se recalcula con ella: ⏳ todavía no la vio, 📋 ya la vio.
function lineupTag(pk, covered){
  if (!pk || !pk.label) return '';
  if (pk.lineup) return '<div class="small" title="' + esc('El pick ya se revisó con la alineación (' + pk.lineup + ')') + '">📋 ya con alineación</div>';
  if (covered === false) return '<div class="muted small" title="ESPN no publica alineaciones de esta liga: el pick no las ve. Revisa la alineación antes de apostar.">ESPN no publica alineación</div>';
  return '<div class="muted small" title="La alineación sale ~1 h antes del partido y ahí se revisa el pick (puede cambiar). Para apostar, espera a que salga.">⏳ falta la alineación</div>';
}
function whyTag(pk){
  if (!pk || pk.rule || !(pk.reason || pk.star_note)) return '';
  var txt = pk.star_note ? noteShort(pk.star_note) : shortWhy(pk.reason);
  if (pk.star_note && /^Poca historia/i.test(pk.reason || '')) txt += ' · ⚠️ pocos partidos de estos equipos';
  return '<div class="muted small" title="' + esc(pk.star_note || pk.reason) + '">sin 🎯: ' + esc(txt) + '</div>';
}

// ── Anteriores (desde septiembre) ──
var _past = [], _pastShown = 150, _pastOnly = false;
function ok(v){ return v === true ? '✅' : v === false ? '❌' : '<span class="muted">·</span>'; }
function pastRow(r, i){
  var f = r.fav, pk = r.pick, bets = '';
  for (var b = 0; b < r.bets.length; b++) {
    var x = r.bets[b], won = x.result === 'win' || x.result === 'half_win';
    bets += '<span title="' + esc(x.market) + '" class="' + (x.profit >= 0 ? 'pos' : 'neg') + '">' + (won ? '✅' : x.result === 'push' ? '↔' : '❌') +
            (x.profit !== null ? ' ' + money(x.profit) : '') + '</span> ';
  }
  var side = { home: 'Local', draw: 'Empate', away: 'Visitante' };
  var pickTxt = noPick('');
  if (pk && pk.none) pickTxt = noPick(pk.reason);
  else if (pk) {
    var played = pk.result === 'win' || pk.result === 'loss' || pk.result === 'half_win' || pk.result === 'half_loss';
    pickTxt = '<span' + (pk.old_model ? ' style="opacity:.6"' : '') + ' title="' + esc(pk.label + ' · qué tan probable lo veía: el programa ' + p100(pk.model) + ', las casas ' + p100(pk.market) +
              (pk.winner ? ' · tuvo razón ' + (pk.winner === 'mercado' ? 'el mercado (las casas)' : 'el programa') : '') +
              (pk.old_model ? ' · versión del programa con error (antes del 22 oct)' : '') +
              (pk.rule ? ' · 🎯 apuesta recomendada' : (pk.star_note || pk.reason ? ' · sin 🎯: ' + (pk.star_note || pk.reason) : '')) +
              (pk.rebuilt ? ' · rehecho con la predicción guardada antes del partido' : '') +
              (pk.provisional ? ' · provisional: ya terminó, cuenta en los totales cuando se cobre' : '')) + '">' + (pk.rebuilt ? '↺ ' : '') +
              (played ? ok(pk.result === 'win' || pk.result === 'half_win') : '⏳') + ' ' + (pk.rule ? '🎯 ' : '') + famIcon(pk.family) + ' ' + esc(pk.label) +
              (pk.profit !== null && played ? ' <span class="' + (pk.profit >= 0 ? 'pos' : 'neg') + '">' + money(pk.profit) + '</span>' : '') + '</span>' +
              whyTag(pk);
  }
  return '<tr class="r" onclick="openPast(' + i + ')"><td class="hide-sm muted small">' + esc(r.league) + '</td><td style="text-align:right">' + esc(r.home) +
         '</td><td class="sc">' + (r.score ? r.score[0] + ' - ' + r.score[1] + liveTag(r.live) :
                                  '<span class="muted" title="Ya empezó; el marcador llega en unos minutos">⏳</span>') +
         '</td><td>' + esc(r.away) + '</td><td>' +
         (r.corners ? r.corners[0] + '-' + r.corners[1] : '<span class="muted">—</span>') + '</td><td class="hide-sm">' +
         (r.cards === null ? '<span class="muted">—</span>' : r.cards) + '</td>' +
         '<td title="' + esc(f ? 'Programa: ' + side[f.model] + ' ' + p100(f.model_p) : '') + '">' + ok(f ? f.model_hit : null) + '</td>' +
         '<td title="' + esc(f && f.pin ? 'Pinnacle: ' + side[f.pin] + ' ' + p100(f.pin_p) : '') + '">' + ok(f ? f.pin_hit : null) + '</td>' +
         '<td class="small">' + pickTxt + '</td><td class="hide-sm small">' + (bets || '<span class="muted">·</span>') + '</td></tr>' +
         '<tr id="pr' + i + '" style="display:none"><td colspan="10"><div id="pd' + i + '"></div></td></tr>';
}
function filteredPast(){
  var lg = el('pleague').value, q = el('pteam').value.toLowerCase(), out = [];
  for (var i = 0; i < _past.length; i++) {
    var r = _past[i];
    if (lg && r.league !== lg) continue;
    if (q && (r.home + ' ' + r.away).toLowerCase().indexOf(q) < 0) continue;
    if (_pastOnly && !r.fav && !r.pick && !r.bets.length) continue;
    out.push(i);
  }
  return out;
}
function renderPast(){
  var idx = filteredPast(), html = '', last = '';
  for (var k = 0; k < idx.length && k < _pastShown; k++) {
    var r = _past[idx[k]];
    if (r.date !== last) { html += '<tr class="dh"><td colspan="10">' + dayLabel(r.date) + '</td></tr>'; last = r.date; }
    html += pastRow(r, idx[k]);
  }
  el('pastbody').innerHTML = html || '<tr><td colspan="10" class="muted">Sin partidos.</td></tr>';
  el('pmore').style.display = idx.length > _pastShown ? 'block' : 'none';
}
function openPast(i){
  var row = el('pr' + i), r = _past[i];
  var show = row.style.display === 'none';
  row.style.display = show ? '' : 'none';
  if (!show) return;
  var box = el('pd' + i), ck = r.key + '|' + r.date;
  if (_detCache[ck]) { box.innerHTML = _detCache[ck]; return; }
  box.innerHTML = '<div class="muted small">Cargando…</div>';
  var p = r.key.split(' vs ');
  getJSON('/api/match?home=' + encodeURIComponent(p[0]) + '&away=' + encodeURIComponent(p[1]) + '&date=' + encodeURIComponent(r.date + 'T00:00:00Z'),
    function(d){ _detCache[ck] = detailHtml(d); box.innerHTML = _detCache[ck]; },
    function(msg){ box.innerHTML = '<div class="muted small">' + esc(msg) + '</div>'; });
}
function kpi(v, l, cls){ return '<div class="k"><div class="v ' + (cls || '') + '">' + v + '</div><div class="l">' + l + '</div></div>'; }
function loadPast(fresh){
  getJSON('/api/board/past' + (fresh ? '?fresh=1' : ''), function(d){
    _past = d.rows;
    var s = d.summary;
    el('pkpis').innerHTML = kpi(s.matches, 'Partidos desde el ' + shortDate(d.since)) +
      kpi(p100(s.model_hit), '🤖 Veces que el programa atinó el resultado (de ' + s.analyzed + ' partidos)') +
      kpi(p100(s.pin_hit), '🏦 Veces que Pinnacle atinó el resultado') +
      kpi(s.picks_fixed.n ? s.picks_fixed.won + ' / ' + s.picks_fixed.n : '—', 'Picks ganados / jugados, desde el 22 oct' +
          (s.picks_fixed.priced ? ' · ' + money(s.picks_fixed.profit) : ''), s.picks_fixed.profit >= 0 ? 'pos' : 'neg') +
      kpi(s.picks_rule.n ? s.picks_rule.won + ' / ' + s.picks_rule.n : '—', '🎯 Recomendados ganados / jugados' +
          (s.picks_rule.priced ? ' · ' + money(s.picks_rule.profit) : ''), s.picks_rule.profit >= 0 ? 'pos' : 'neg') +
      kpi(s.picks_old.n ? s.picks_old.won + ' / ' + s.picks_old.n : '—', '↺ Picks antes del 22 oct (versión con error)' +
          (s.picks_old.priced ? ' · ' + money(s.picks_old.profit) : ''), s.picks_old.profit >= 0 ? 'pos' : 'neg') +
      kpi(s.bets ? money(s.bets_profit) : '—', '💰 Apuestas reales (' + s.bets + ')', s.bets_profit >= 0 ? 'pos' : 'neg');
    var lg = {}, list = [];
    for (var i = 0; i < _past.length; i++) if (!lg[_past[i].league]) { lg[_past[i].league] = 1; list.push(_past[i].league); }
    fillSelect('pleague', list.sort());
    renderPast();
  }, function(msg){ el('pastbody').innerHTML = '<tr><td colspan="10" class="muted">' + esc(msg) + '</td></tr>'; });
}
el('pleague').onchange = function(){ _pastShown = 150; renderPast(); };
el('pteam').onkeyup = function(){ _pastShown = 150; renderPast(); };
el('ponly').onclick = function(){ _pastOnly = !_pastOnly; this.className = 'pill' + (_pastOnly ? ' on' : ''); _pastShown = 150; renderPast(); };
el('pmore').onclick = function(){ _pastShown += 150; renderPast(); };

// ── Ganadas y perdidas (v2.5.3, 29-oct-26) ──
// El dueño: "necesito gráficas y datos de ganadas y perdidas, ver los picks más
// ganadores y perdedores, y las ligas, cómo van en las apuestas, si se ganan
// bien ahí o no". Tres grupos: todos los picks, los 🎯 y las apuestas reales.
var _res = null, _resGroup = _storeGet('rgroup', 'picks');
function _units(v){ return '<span class="' + (v >= 0 ? 'pos' : 'neg') + '">' + money(v) + '</span>'; }
// ROI: lo que se gana por cada unidad apostada, en % (0.002 → +0.2%)
function _roi(v){ return v === null || v === undefined ? '—' :
  '<span class="' + (v >= 0 ? 'pos' : 'neg') + '">' + (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '%</span>'; }
function resRow(g, minN){
  return '<tr' + (g.few ? ' class="few" title="Pocos datos: menos de ' + minN + ' picks, todavía no dice mucho"' : '') + '>' +
    '<td>' + esc(g.name || '—') + '</td><td class="sc">' + g.won + '-' + g.lost + '</td><td>' + p100(g.hit) + '</td>' +
    '<td>' + _units(g.units) + '</td><td class="hide-sm">' + _roi(g.roi) + '</td></tr>';
}
function renderResults(){
  if (!_res) return;
  var g = _res.groups[_resGroup], t = g.total, i, h;
  var pills = el('rgroups').getElementsByTagName('span');
  for (i = 0; i < pills.length; i++) if (pills[i].getAttribute('data-g'))
    pills[i].className = 'pill' + (pills[i].getAttribute('data-g') === _resGroup ? ' on' : '');
  el('rkpis').innerHTML = kpi(t.won, '✅ Ganadas', 'pos') + kpi(t.lost, '❌ Perdidas', 'neg') +
    kpi(t.push, '↔️ Devueltas / anuladas') + kpi(p100(t.hit), '% de acierto') +
    kpi(money(t.units), 'Ganancia (1 unidad por apuesta)', t.units >= 0 ? 'pos' : 'neg') +
    kpi(t.roi === null ? '—' : (t.roi >= 0 ? '+' : '') + (t.roi * 100).toFixed(1) + '%', 'Rendimiento: lo que se gana o pierde de cada 100 apostados', t.roi >= 0 ? 'pos' : 'neg');
  var labels = [], lost = [];
  for (i = 0; i < g.daily.dates.length; i++) { labels.push(shortDate(g.daily.dates[i])); lost.push(-g.daily.lost[i]); }
  var tick = { fontColor: '#94a3b8' };
  chart('cdaily', { type: 'bar', data: { labels: labels, datasets: [
      { label: '✅ Ganadas', data: g.daily.won, backgroundColor: '#22c55e', stack: 'gp', yAxisID: 'n' },
      { label: '❌ Perdidas', data: lost, backgroundColor: '#ef4444', stack: 'gp', yAxisID: 'n' },
      { label: 'Unidades acumuladas', data: g.daily.units, type: 'line', borderColor: '#60a5fa', fill: false, yAxisID: 'u' }] },
    options: { legend: { labels: { fontColor: '#94a3b8', boxWidth: 12 } },
      scales: { xAxes: [{ stacked: true, ticks: { maxTicksLimit: 12, fontColor: '#94a3b8' } }],
                yAxes: [{ id: 'n', stacked: true, position: 'left', ticks: { fontColor: '#94a3b8', callback: function(v){ return Math.abs(v); } } },
                        { id: 'u', position: 'right', gridLines: { drawOnChartArea: false },
                          ticks: { fontColor: '#60a5fa', callback: function(v){ return v + 'u'; } } }] },
      tooltips: { callbacks: { label: function(it, data){
        var ds = data.datasets[it.datasetIndex];
        return ds.label + ': ' + (ds.yAxisID === 'u' ? money(it.yLabel) : Math.abs(it.yLabel)); } } } } });
  h = '';
  for (i = 0; i < g.by_type.length; i++) h += resRow(g.by_type[i], _res.min_n);
  el('rtypes').innerHTML = h || '<tr><td colspan="5" class="muted">Todavía no hay apuestas terminadas.</td></tr>';
  h = '';
  var ll = [], lu = [], lc = [];
  for (i = 0; i < g.by_league.length; i++) {
    var lg = g.by_league[i];
    h += resRow(lg, _res.min_n);
    ll.push(lg.name + ' (' + lg.won + '-' + lg.lost + ')'); lu.push(lg.units);
    lc.push(lg.few ? (lg.units >= 0 ? 'rgba(34,197,94,.35)' : 'rgba(239,68,68,.35)') : (lg.units >= 0 ? '#22c55e' : '#ef4444'));
  }
  el('rleagues').innerHTML = h || '<tr><td colspan="5" class="muted">Todavía no hay apuestas terminadas.</td></tr>';
  el('cleaguesbox').style.height = Math.max(160, 40 + 22 * ll.length) + 'px';   // una barra por liga
  chart('cleagues', { type: 'horizontalBar', data: { labels: ll, datasets: [{ data: lu, backgroundColor: lc }] },
    options: { legend: { display: false }, scales: { xAxes: [{ ticks: tick }], yAxes: [{ ticks: tick }] },
               tooltips: { callbacks: { label: function(it){ var x = g.by_league[it.index];
                 return money(x.units) + ' · acierto ' + p100(x.hit) + (x.few ? ' · pocos datos' : ''); } } } } });
  h = '';
  for (i = 0; i < g.best.length; i++) {
    var b = g.best[i];
    h += '<tr><td class="nw">' + shortDate(b.date) + '</td><td>' + esc(b.match) + '<div class="small muted">' + esc(b.league) + '</div></td>' +
         '<td>' + esc(b.type) + (b.odds ? ' <span class="muted">' + b.odds + '</span>' : '') + '</td><td>' + _units(b.profit) + '</td></tr>';
  }
  el('rbest').innerHTML = h || '<tr><td colspan="4" class="muted">Sin aciertos todavía.</td></tr>';
  h = '';
  for (i = 0; i < g.worst.length; i++) {
    var w = g.worst[i];
    h += '<tr><td class="nw">' + shortDate(w.date) + '</td><td>' + esc(w.match) + '<div class="small muted">' + esc(w.league) + '</div></td>' +
         '<td>' + esc(w.type) + '</td><td class="nw">' + (w.model !== null && w.model !== undefined ? '🤖 ' + p100(w.model) : _units(w.profit || 0)) + '</td></tr>';
  }
  el('rworst').innerHTML = h || '<tr><td colspan="4" class="muted">Sin fallos todavía.</td></tr>';
}
function loadResults(fresh){
  getJSON('/api/board/results' + (fresh ? '?fresh=1' : ''), function(d){ _res = d; renderResults(); },
    function(msg){ el('rkpis').innerHTML = '<div class="muted">' + esc(msg) + '</div>'; });
}
(function(){
  var pills = el('rgroups').getElementsByTagName('span');
  for (var i = 0; i < pills.length; i++) if (pills[i].getAttribute('data-g'))
    pills[i].onclick = function(){ _resGroup = this.getAttribute('data-g'); _storeSet('rgroup', _resGroup); renderResults(); };
})();

// ── Por liga (v2.10.0, 7-nov-26) ──
// El dueño: "necesito que me des un resumen de los partidos que tenemos hasta
// hoy, pero me lo ordenes por ligas, dime cuántas se ganaron y cuántas se
// perdieron". Cada liga (en orden alfabético, o por la columna que toque) con
// sus ganados y perdidos; al tocarla se ven sus partidos.
var _lg = null, _lgGroup = _storeGet('lgroup', 'picks'), _lgSort = _storeGet('lsort', 'name'), _lgOpen = {};
var LG_SORTS = ['name', 'n', 'won', 'lost', 'hit', 'units'];
// la ganancia a 1 unidad dicha apostando 100 (0.64 → +64)
function cien(v){ var n = Math.round((v || 0) * 100); return (n > 0 ? '+' : '') + n; }
function _cien(v){ return '<span class="' + ((v || 0) >= 0 ? 'pos' : 'neg') + '">' + cien(v) + '</span>'; }
function wlBar(won, lost){
  var n = won + lost;
  return n ? '<span class="wl"><span class="w" style="width:' + (won / n * 100) + '%"></span>' +
             '<span class="l" style="width:' + (lost / n * 100) + '%"></span></span>' : '';
}
function lgIcon(r){
  if (r === 'win' || r === 'half_win') return '✅';
  if (r === 'loss' || r === 'half_loss') return '❌';
  return '↔️';
}
function lgSorted(list, key){
  var out = list.slice();
  if (key === 'name') return out;                          // ya viene en orden alfabético
  out.sort(function(a, b){
    var x = a[key] === null ? -1 : a[key], y = b[key] === null ? -1 : b[key];
    return (y - x) || (a.name < b.name ? -1 : 1);
  });
  return out;
}
function lgCells(x){
  var unpriced = x.n - x.priced;
  return '<td class="sc">' + x.n + (x.push ? ' <span class="small muted">(' + x.push + (x.push === 1 ? ' anulado' : ' anulados') + ')</span>' : '') + '</td>' +
    '<td class="sc pos">' + x.won + '</td><td class="sc neg">' + x.lost + '</td>' +
    '<td class="hide-sm nw">' + wlBar(x.won, x.lost) + (x.hit === null ? '—' : Math.round(x.hit * 100)) + '</td>' +
    '<td class="sc">' + _cien(x.units) + (unpriced > 0 ? ' <span class="small muted" title="' + unpriced +
      (unpriced === 1 ? ' partido sin momio de PlayDoit: cuenta en ganados y perdidos, no en la ganancia'
                      : ' partidos sin momio de PlayDoit: cuentan en ganados y perdidos, no en la ganancia') + '">*</span>' : '') + '</td>';
}
function lgRow(lg, open, minN){
  return '<tr class="r' + (lg.few ? ' few' : '') + '" data-lg="' + esc(lg.name) + '" title="' +
    (lg.few ? 'Pocos partidos: menos de ' + minN + ', todavía no dice mucho. ' : '') + 'Toca para ver sus partidos">' +
    '<td>' + (open ? '▾ ' : '▸ ') + esc(lg.name) + '</td>' + lgCells(lg) + '</tr>';
}
function lgMatch(m, showStar){
  var gain = m.profit === null || m.profit === undefined ? '<span class="muted" title="Sin momio de PlayDoit">—</span>' : _cien(m.profit);
  return '<tr class="sub"><td colspan="6">' + lgIcon(m.result) + ' <span class="muted">' + shortDate(m.date) + '</span> · ' +
    esc(m.match) + ' · ' + (showStar && m.rule ? '🎯 ' : '') + esc(m.type) + ' · ' + gain + '</td></tr>';
}
function renderLeagues(){
  if (!_lg) return;
  if (!_lg.groups[_lgGroup]) _lgGroup = 'picks';
  if (LG_SORTS.indexOf(_lgSort) < 0) _lgSort = 'name';
  var g = _lg.groups[_lgGroup], t = g.total, i, j, h = '';
  var pills = el('lgroups').getElementsByTagName('span');
  for (i = 0; i < pills.length; i++) if (pills[i].getAttribute('data-g'))
    pills[i].className = 'pill' + (pills[i].getAttribute('data-g') === _lgGroup ? ' on' : '');
  var ths = el('lhead').getElementsByTagName('th');
  for (i = 0; i < ths.length; i++)
    ths[i].className = ths[i].className.replace(/(^|\s)on(?=\s|$)/g, '') + (ths[i].getAttribute('data-s') === _lgSort ? ' on' : '');
  el('lkpis').innerHTML = kpi(t.n, t.n === 1 ? 'Partido terminado' : 'Partidos terminados') +
    kpi(t.won, '✅ Ganados', 'pos') + kpi(t.lost, '❌ Perdidos', 'neg') +
    (t.push ? kpi(t.push, '↔️ Anulados') : '') +
    kpi(t.hit === null ? '—' : Math.round(t.hit * 100), 'Ganados de cada 100') +
    kpi(cien(t.units), 'Apostando 100 a cada uno', t.units >= 0 ? 'pos' : 'neg') +
    kpi(g.leagues.length, g.leagues.length === 1 ? 'Liga' : 'Ligas');
  var ls = lgSorted(g.leagues, _lgSort);
  for (i = 0; i < ls.length; i++) {
    var open = !!_lgOpen[ls[i].name];
    h += lgRow(ls[i], open, _lg.min_n);
    if (open) for (j = 0; j < ls[i].matches.length; j++) h += lgMatch(ls[i].matches[j], _lgGroup === 'picks');
  }
  h = ls.length ? h + '<tr class="tot"><td>Total</td>' + lgCells(t) + '</tr>'
                : '<tr><td colspan="6" class="muted">Todavía no hay partidos terminados.</td></tr>';
  el('lbody').innerHTML = h;
  var trs = el('lbody').getElementsByTagName('tr');
  for (i = 0; i < trs.length; i++) if (trs[i].getAttribute('data-lg') !== null)
    trs[i].onclick = function(){ var n = this.getAttribute('data-lg'); _lgOpen[n] = !_lgOpen[n]; renderLeagues(); };
}
function loadLeagues(fresh){
  getJSON('/api/board/leagues' + (fresh ? '?fresh=1' : ''), function(d){ _lg = d; renderLeagues(); },
    function(msg){ el('lbody').innerHTML = '<tr><td colspan="6" class="muted">' + esc(msg) + '</td></tr>'; });
}
(function(){
  var pills = el('lgroups').getElementsByTagName('span');
  for (var i = 0; i < pills.length; i++) if (pills[i].getAttribute('data-g'))
    pills[i].onclick = function(){ _lgGroup = this.getAttribute('data-g'); _storeSet('lgroup', _lgGroup); renderLeagues(); };
  var ths = el('lhead').getElementsByTagName('th');
  for (var j = 0; j < ths.length; j++)
    ths[j].onclick = function(){ _lgSort = this.getAttribute('data-s'); _storeSet('lsort', _lgSort); renderLeagues(); };
})();

// ── Parlay (v2.6.0, 3-nov-26; con bono desde v2.9.0, 6-nov-26) ──
// El dueño: "quiero agregar la función parley…". v2.9.0: favoritos de 65%+ con
// pago anticipado; el bono de PlayDoit se paga sobre la ganancia (3 = 5%,
// 4 = 7.5%, 5 = 10%, 6 = 15%). El del día (4 a 6) y el de un horario (3 o más
// que empiezan juntos, con las alineaciones de todos).
var _px = null;
function pxIcon(x){
  if (x.pa_paid && (x.result === 'win')) return '<span title="Pago anticipado: fue 2 goles arriba y PlayDoit la pagó como ganada">⚡✅</span>';
  if (x.result === 'win' || x.result === 'half_win') return '✅';
  if (x.result === 'loss' || x.result === 'half_loss') return '❌';
  if (x.result === 'push' || x.result === 'stale') return '<span title="Anulada: el parlay sigue con las demás">↔</span>';
  if (x.live && x.live.status === 'live') return '<span class="small muted">● ' + esc(x.live.clock || 'en vivo') + '</span>';
  return x.started ? '<span class="muted" title="Ya empezó">⏳</span>' : '<span class="muted">·</span>';
}
function pxStatus(p){
  if (!p) return '<span class="muted">—</span>';
  var prov = p.settled ? '' : ' <span class="small muted" title="Con el marcador final; cuenta en el historial cuando se liquide">provisional</span>';
  if (p.status === 'win') return '<span class="pos">✅ ' + money(p.profit) + '</span>' + prov;
  if (p.status === 'loss') return '<span class="neg">❌ ' + money(-1) + '</span>' + prov;
  if (p.status === 'push') return '↔ anulado' + prov;
  return '<span class="muted">⏳ ' + p.won_legs + ' de ' + p.k + ' ganados hasta ahora</span>';
}
function pxLineup(x){
  if (x.started) return '';
  if (x.lineup) return ' <span class="small" title="' + esc('Ya se revisó con la alineación (' + x.lineup + ')') + '">· 📋 con alineación</span>';
  if (x.no_lineups) return ' <span class="muted small" title="ESPN no publica alineaciones de esta liga: revísala antes de apostar.">· ESPN no publica alineación</span>';
  return ' <span class="muted small" title="La alineación sale ~1 h antes del partido.">· ⏳ falta la alineación</span>';
}
function pxReady(d){
  var p = d.parlay;
  if (!p || p.status !== 'pending') return '';
  if (p.locked) return '<span class="small muted">🔒 ya empezó: así quedó</span>';
  var one = d.kind === 'dia', what = one ? 'la alineación del primer partido' : 'las alineaciones de todos';
  if (d.ready) return '<span class="small pos">📋 ya revisado con ' + what + '</span><span class="small muted">apuéstalo antes de las ' + mxtime(d.lock) + '</span>';
  return '<span class="small muted" title="Las alineaciones salen ~1 h antes; el parlay se revisa cada 30 min y Telegram avisa cuando está listo">⏳ ' +
    (one ? 'falta ' : 'faltan ') + what + (one ? ' (sale' : ' (salen') + ' ~1 h antes) · puede cambiar hasta las ' + mxtime(d.lock) + '</span>';
}
// "~14%" → "1 de cada 7": la probabilidad de ganarlo dicha en veces
function pxOdds(p){
  if (!p) return '—';
  var n = Math.round(1 / p);
  return n > 1 ? 'más o menos 1 de cada ' + n + ' veces' : p100(p);
}
function pxName(d){
  if (d.kind === 'dia') return 'Parlay del día' + (d.seq > 1 ? ' (' + d.seq + ')' : '');
  return 'Parlay de las ' + mxtime(d.slot);
}
function pxBonus(p){
  return p.bonus ? Math.round(p.bonus * 1000) / 10 + '%' : '';
}
function pxBlock(d){
  var p = d.parlay, h = '<div class="small" style="margin:6px 0 4px"><b>' + pxName(d) + '</b> · ' + p.k + ' partidos' +
    (d.kind === 'horario' ? ' <span class="muted">(empiezan a la misma hora: se arma ya con todas las alineaciones)</span>' : '') + '</div>';
  h += '<div class="tbl" style="margin-bottom:8px"><table><thead><tr><th>Hora</th><th>Partido</th><th>Apuesta</th><th>Momio</th>' +
       '<th class="hide-sm" title="De cada 100 partidos así, cuántos gana este equipo según Pinnacle">Gana</th><th></th></tr></thead><tbody>';
  for (var i = 0; i < d.legs.length; i++) {
    var x = d.legs[i];
    h += '<tr><td class="nw">' + mxtime(x.kickoff) + '</td><td>' + esc(x.match) + '<div class="small muted">' + esc(x.league) + '</div></td>' +
         '<td><b>' + esc(x.label) + '</b>' + (x.pa ? ' <span class="small" title="Pago anticipado: si va 2 goles arriba, PlayDoit la paga como ganada aunque luego le empaten">⚡</span>' : '') +
         pxLineup(x) + '</td><td class="nw">' + esc(x.odds_us || '') + '</td><td class="hide-sm nw" title="De cada 100 partidos así, cuántos gana según Pinnacle">' +
         (x.market === null || x.market === undefined ? '—' : Math.round(x.market * 100) + ' de 100') + '</td>' +
         '<td class="sc"' + (x.provisional ? ' title="Provisional: ya terminó; se confirma cuando se cobre"' : '') + '>' + pxIcon(x) + '</td></tr>';
  }
  h += '</tbody></table><div class="xsum"><span title="Momio del parlay en PlayDoit (el aproximado de cada partido, multiplicado)">paga <b>' + esc(p.odds_us || '—') + '</b></span>' +
       (p.bonus ? '<span title="PlayDoit suma este bono a la ganancia">con el bono de ' + pxBonus(p) + ': <b class="big">' + esc(p.odds_bonus_us || '—') + '</b></span>' : '') +
       '<span class="muted" title="Probabilidad de que salgan todos (según Pinnacle; el pago anticipado la sube un poco): ~' + p100(p.p_market) + '">se gana ' + pxOdds(p.p_market) + '</span>' +
       '<span>' + pxStatus(p) + '</span>' + pxReady(d) + '</div></div>';
  return h;
}
function renderParlay(){
  if (!_px) return;
  var i, h = '', today = mxday(), r = _px.rules;
  el('xrule').innerHTML = '<b>Cómo se arma:</b> solo equipos muy favoritos (según Pinnacle ganan ' + Math.round(r.strong * 100) +
    ' de cada 100 partidos así o más) que paguen ' + esc(r.min_odds_us) + ' o mejor, en ligas con pago anticipado ⚡ ' +
    '(si el equipo va 2 goles arriba, PlayDoit la paga como ganada aunque luego le empaten). ' +
    'PlayDoit suma un bono a la ganancia: 3 partidos +5%, 4 +7.5%, 5 +10%, 6 +15%. ' +
    '<b>Parlay del día:</b> de ' + r.day_min + ' a ' + r.max_legs + ' partidos. <b>Parlay de un horario:</b> ' + r.slot_min +
    ' o más que empiezan a la misma hora, ya con las alineaciones. Ligas: ' + esc(r.leagues.join(', ')) + '.';
  var next = [], lastDay = '';
  for (i = _px.parlays.length - 1; i >= 0; i--) if (_px.parlays[i].day >= today) next.push(_px.parlays[i]);   // del más próximo
  for (i = 0; i < next.length; i++) {
    if (next[i].day !== lastDay) { h += '<div class="day">' + dayLabel(next[i].day) + '</div>'; lastDay = next[i].day; }
    h += pxBlock(next[i]);
  }
  el('xnext').innerHTML = h || '<div class="muted">Por ahora no hay parlay: ningún día tiene ' + r.day_min + ' equipos muy favoritos en las ligas con pago anticipado ' +
    '(ni ' + r.slot_min + ' a la misma hora). Se revisa cada 30 min y Telegram avisa.</div>';
  var t = _px.record.all, td = _px.record.dia, th = _px.record.horario;
  el('xkpis').innerHTML = kpi(t.n, 'Parlays terminados') + kpi(t.won, '✅ Ganados', 'pos') + kpi(t.lost, '❌ Perdidos', 'neg') +
    kpi(money(t.units), 'Ganancia (1 unidad por parlay, con bono)', t.units >= 0 ? 'pos' : 'neg') +
    kpi(_roi(t.roi), 'Rendimiento: lo que se gana o pierde de cada 100 apostados') +
    kpi(t.legs ? t.legs_won + ' / ' + t.legs : '—', 'Partidos de los parlays que salieron (' + p100(t.legs_hit) + ')') +
    kpi(t.pa, '⚡ Cobrados por ir 2 goles arriba') +
    kpi(td.n ? money(td.units) + ' en ' + td.n : '—', 'Del día') + kpi(th.n ? money(th.units) + ' en ' + th.n : '—', 'De un horario');
  h = '';
  for (i = 0; i < _px.parlays.length; i++) {
    var d = _px.parlays[i], p = d.parlay;
    if (d.day >= today && p.status === 'pending') continue;
    var legs = [];
    for (var j = 0; j < d.legs.length; j++)
      legs.push(pxIcon(d.legs[j]) + ' ' + esc(d.legs[j].label) + ' <span class="muted">· ' + esc(d.legs[j].match) + '</span>');
    h += '<tr><td class="nw">' + shortDate(d.day) + '</td><td class="nw">' + pxName(d) + '</td><td class="small">' + legs.join('<br>') +
         '</td><td class="nw">' + esc(p.odds_bonus_us || p.odds_us || '') + (p.bonus ? ' <span class="small muted">(' + pxBonus(p) + ')</span>' : '') +
         '</td><td class="nw">' + pxStatus(p) + '</td></tr>';
  }
  el('xhist').innerHTML = h || '<tr><td colspan="5" class="muted">Todavía no hay parlays con bono terminados: se guardan desde el 6 de noviembre.</td></tr>';
}
function loadParlay(){
  getJSON('/api/board/parlay', function(d){ _px = d; renderParlay(); },
    function(msg){ el('xnext').innerHTML = '<div class="muted">' + esc(msg) + '</div>'; });
}

// ── Equipos ──
var _teams = [], _charts = {};
function chart(id, cfg){
  if (_charts[id]) _charts[id].destroy();
  _charts[id] = new Chart(el(id).getContext('2d'), cfg);
}
function renderTeams(){
  var lg = el('tleague').value, q = el('tsearch').value.toLowerCase(), h = '';
  for (var i = 0; i < _teams.length; i++) {
    var t = _teams[i];
    if (lg && t.league !== lg) continue;
    if (q && t.name.toLowerCase().indexOf(q) < 0) continue;
    h += '<span class="pill" onclick="loadTeam(_teams[' + i + '].key)">' + esc(t.name) + '</span>';
  }
  el('teamlist').innerHTML = h || '<span class="muted">Sin equipos.</span>';
}
function loadTeams(){
  getJSON('/api/board/teams', function(d){
    _teams = d.teams;
    var lg = {}, list = [];
    for (var i = 0; i < _teams.length; i++) if (!lg[_teams[i].league]) { lg[_teams[i].league] = 1; list.push(_teams[i].league); }
    fillSelect('tleague', list.sort());
    renderTeams();
    var last = _storeGet('team', '');
    if (last) loadTeam(last);
  }, function(msg){ el('teamlist').innerHTML = '<span class="muted">' + esc(msg) + '</span>'; });
}
el('tleague').onchange = renderTeams;
el('tsearch').onkeyup = renderTeams;
function loadTeam(key){
  _storeSet('team', key);
  el('teamview').innerHTML = '<div class="muted">Cargando…</div>';
  getJSON('/api/board/team?name=' + encodeURIComponent(key), function(d){
    var r = d.record, a = d.avg || {}, h = '<div class="bar" style="margin-top:4px"><span style="font-size:1.2rem;font-weight:700;margin-right:8px">' + esc(d.team) + '</span>' +
      '<span class="chip"><b class="pos">' + r.w + '</b> G · <b>' + r.d + '</b> E · <b class="neg">' + r.l + '</b> P</span>' +
      (a.gf !== null && a.gf !== undefined ? '<span class="chip" title="Goles a favor - en contra por partido">⚽ ' + a.gf + ' - ' + a.ga + '</span>' : '') +
      (a.cf ? '<span class="chip" title="Córners a favor - en contra por partido">⛳ ' + a.cf + ' - ' + a.ca + '</span>' : '') +
      (a.cards ? '<span class="chip" title="Tarjetas por partido (roja = 1)">🟨 ' + a.cards + '</span>' : '') + '</div>';
    h += '<div class="grid2"><div class="cb"><h3>Goles por partido</h3><canvas id="tgoals"></canvas></div>' +
         '<div class="cb"><h3>Córners por partido</h3><canvas id="tcorners"></canvas></div></div>';
    if (d.upcoming.length) {
      h += '<div class="day">Próximos</div>';
      for (var u = 0; u < d.upcoming.length; u++) {
        var x = d.upcoming[u];
        h += '<div class="m"><div class="mr" style="cursor:default"><div class="time">' + mxtime(x.date) + '<span class="small muted">' + shortDate(mxday(x.date)) +
             '</span></div><div class="t h">' + esc(x.home) + '</div><div class="pbw">' + probBar(x.probs) + '</div><div class="t">' + esc(x.away) +
             '</div><div class="pk muted small">' + esc(x.league) + '</div><div></div></div></div>';
      }
    }
    h += '<div class="day">Desde septiembre</div><div class="tbl"><table><thead><tr><th>Fecha</th><th>Rival</th><th></th><th>Res.</th><th>Córners</th><th>Tarjetas</th><th class="hide-sm">Competición</th></tr></thead><tbody>';
    for (var i = d.matches.length - 1; i >= 0; i--) {
      var m = d.matches[i], cls = m.result === 'G' ? 'pos' : m.result === 'P' ? 'neg' : '';
      h += '<tr' + (m.friendly ? ' style="opacity:.55" title="Amistoso: no cuenta en el récord ni en los promedios"' : '') + '><td>' + shortDate(m.date) + '</td><td>' + esc(m.opponent) + '</td><td class="muted">' + (m.venue === 'L' ? 'Local' : 'Visita') +
           '</td><td class="' + cls + '"><b>' + m.result + ' ' + m.gf + '-' + m.ga + '</b></td><td>' + (m.cf === null ? '—' : m.cf + '-' + m.ca) +
           '</td><td>' + (m.cards === null ? '—' : m.cards) + '</td><td class="hide-sm muted small">' + esc(m.league) + '</td></tr>';
    }
    h += '</tbody></table></div>';
    el('teamview').innerHTML = h;
    var labels = [], gf = [], ga = [], cf = [], ca = [];
    for (var j = 0; j < d.matches.length; j++) {
      var mm = d.matches[j];
      if (mm.friendly) continue;                // las gráficas, sin amistosos
      labels.push(shortDate(mm.date) + ' ' + mm.opponent);
      gf.push(mm.gf); ga.push(mm.ga); cf.push(mm.cf); ca.push(mm.ca);
    }
    var xa = [{ ticks: { display: false }, gridLines: { display: false } }];
    chart('tgoals', { type: 'bar', data: { labels: labels, datasets: [
      { label: 'A favor', data: gf, backgroundColor: '#22c55e' }, { label: 'En contra', data: ga, backgroundColor: '#ef4444' }] },
      options: { legend: { labels: { fontColor: '#94a3b8', boxWidth: 12 } }, scales: { xAxes: xa, yAxes: [{ ticks: { beginAtZero: true, stepSize: 1, fontColor: '#94a3b8' } }] } } });
    chart('tcorners', { type: 'line', data: { labels: labels, datasets: [
      { label: 'A favor', data: cf, borderColor: '#60a5fa', fill: false, spanGaps: true },
      { label: 'En contra', data: ca, borderColor: '#f97316', fill: false, spanGaps: true }] },
      options: { legend: { labels: { fontColor: '#94a3b8', boxWidth: 12 } }, scales: { xAxes: xa, yAxes: [{ ticks: { beginAtZero: true, fontColor: '#94a3b8' } }] } } });
  }, function(msg){ el('teamview').innerHTML = '<div class="muted">' + esc(msg) + '</div>'; });
}

// ── Gráficas ──
function loadCharts(fresh){
  getJSON('/api/board/charts' + (fresh ? '?fresh=1' : ''), function(d){
    var axes = { xAxes: [{ ticks: { maxTicksLimit: 8, fontColor: '#94a3b8' } }], yAxes: [{ ticks: { fontColor: '#94a3b8' } }] };
    var nolegend = { display: false }, legend = { labels: { fontColor: '#94a3b8', boxWidth: 12 } };
    var bl = [];
    for (var b = 0; b < d.bankroll.dates.length; b++) bl.push(shortDate(d.bankroll.dates[b]));
    var pkl = [];
    for (var c = 0; c < d.picks.dates.length; c++) pkl.push(shortDate(d.picks.dates[c]));
    d.picks.dates = pkl;
    chart('cbank', { type: 'line', data: { labels: bl, datasets: [{ data: d.bankroll.values, borderColor: '#eab308', pointRadius: 0, borderWidth: 2, fill: false }] },
      options: { legend: nolegend, scales: axes } });
    var wl = [], wm = [], wp = [];
    for (var i = 0; i < d.accuracy.length; i++) { wl.push(shortDate(d.accuracy[i].week)); wm.push(Math.round(d.accuracy[i].model * 100)); wp.push(Math.round(d.accuracy[i].pin * 100)); }
    chart('cacc', { type: 'line', data: { labels: wl, datasets: [
      { label: '🤖 Programa', data: wm, borderColor: '#60a5fa', fill: false }, { label: '🏦 Pinnacle', data: wp, borderColor: '#94a3b8', fill: false }] },
      options: { legend: legend, scales: { xAxes: axes.xAxes, yAxes: [{ ticks: { fontColor: '#94a3b8', callback: function(v){ return v + '%'; } } }] } } });
    var fl = [], fw = [], fu = [], fc = [];
    for (var f in d.families) {
      var x = d.families[f];
      fl.push(famIcon(f) + ' ' + f + ' (' + x.won + '/' + x.n + ')');
      fu.push(x.profit); fc.push(x.profit >= 0 ? '#22c55e' : '#ef4444');
      fw.push(x.n ? Math.round(x.won / x.n * 100) : 0);
    }
    chart('cfam', { type: 'horizontalBar', data: { labels: fl, datasets: [{ label: 'Unidades', data: fu, backgroundColor: fc }] },
      options: { legend: nolegend, scales: { xAxes: [{ ticks: { fontColor: '#94a3b8' } }], yAxes: [{ ticks: { fontColor: '#94a3b8' } }] },
                 tooltips: { callbacks: { afterLabel: function(c){ return 'Acierto: ' + fw[c.index] + '%'; } } } } });
    chart('cpicks', { type: 'line', data: { labels: d.picks.dates, datasets: [
      { label: '↺ Antes del 22 oct (versión con error)', data: d.picks.old, borderColor: '#94a3b8', pointRadius: 0, fill: false, spanGaps: false },
      { label: 'Todos los picks', data: d.picks.fixed, borderColor: '#22c55e', fill: false, spanGaps: false },
      { label: '🎯 Recomendados', data: d.picks.rule, borderColor: '#eab308', fill: false, spanGaps: false }] },
      options: { legend: legend, scales: axes } });
    var pl = [], pv = [], pc = [];
    for (var j = 0; j < d.proposals.length; j++) {
      var w = d.proposals[j];
      pl.push(shortDate(w.week) + (w.old_model ? ' *' : ''));
      pv.push(w.profit_1u);
      pc.push(w.old_model ? (w.profit_1u >= 0 ? 'rgba(34,197,94,.35)' : 'rgba(239,68,68,.35)') : (w.profit_1u >= 0 ? '#22c55e' : '#ef4444'));
    }
    chart('cprops', { type: 'bar', data: { labels: pl, datasets: [{ data: pv, backgroundColor: pc }] },
      options: { legend: nolegend, scales: axes, tooltips: { callbacks: { footer: function(it){ return d.proposals[it[0].index].old_model ? 'Versión del programa con error (antes del 22 oct)' : ''; } } } } });
  }, function(msg){ err('Gráficas: ' + msg); });
}

// ── Encabezado ──
function loadHeader(){
  getJSON('/api/version', function(d){ el('ver').textContent = 'v' + d.current; }, function(){});
  getJSON('/api/kpis', function(d){
    el('chipbank').innerHTML = '💰 Bankroll <b>' + (d.bankroll === null ? '—' : d.bankroll.toFixed(1) + 'u') + '</b>';
  }, function(){});
}

// ── Arranque ──
if (window.Chart && Chart.defaults && Chart.defaults.global) {
  Chart.defaults.global.maintainAspectRatio = false;
  Chart.defaults.global.responsive = true;
  Chart.defaults.global.elements.line.tension = 0;      // rectas: la curva inventa valores entre puntos
}
if (el('refresh')) el('refresh').onclick = function(){ refreshAll(true); };
_refreshing = true;                                    // la primera carga también deja su hora
loadHeader();
loadUpcoming();
showTab((location.hash || '').replace('#', '') || _storeGet('tab2', 'proximos'));
refreshDone();
setInterval(function(){ refreshAll(false); }, 5 * 60000);   // datos cada 5 min
