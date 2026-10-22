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

function getJSON(url, ok, fail){
  try {
    var x = new XMLHttpRequest();
    x.open('GET', url, true);
    x.onreadystatechange = function(){
      if (x.readyState !== 4) return;
      if (x.status === 0) { serverDown(); return; }
      if (x.status !== 200) { if (fail) fail('HTTP ' + x.status); return; }
      var d;
      try { d = JSON.parse(x.responseText); } catch(e2) { if (fail) fail('JSON inválido'); return; }
      if (d.ok === false) { if (fail) fail(d.msg || 'sin datos'); return; }
      if (ok) ok(d);
    };
    x.send();
  } catch(e) { if (fail) fail(e.message); }
}

// ── Pestañas ──
var TABS = ['proximos', 'anteriores', 'equipos', 'graficas'], _loaded = {};
function showTab(name){
  if (TABS.indexOf(name) < 0) name = 'proximos';
  var b = el('tabs').getElementsByTagName('button');
  for (var i = 0; i < b.length; i++) b[i].className = 'tab' + (b[i].getAttribute('data-tab') === name ? ' on' : '');
  for (var j = 0; j < TABS.length; j++) el('tab-' + TABS[j]).className = 'pane' + (TABS[j] === name ? ' on' : '');
  _storeSet('tab2', name);
  if (_loaded[name]) return;
  _loaded[name] = true;
  if (name === 'anteriores') loadPast();
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
    h += '<span class="f ' + m.result + '" title="' + esc(shortDate(m.date) + ' · ' + (m.venue === 'L' ? 'vs ' : 'en ') + m.opponent + ' · ' + m.score) + '">' + m.result + '</span>';
  }
  return h + '</div>';
}
function teamPanel(t){
  var h = '<div class="tp"><h3>' + esc(t.team) + '</h3>';
  if (!t.summary) return h + '<div class="muted small">Sin partidos anteriores</div></div>';
  var s = t.summary;
  h += formBadges(t.matches) + '<div class="small muted">Goles ' + s.goals_for + ' - ' + s.goals_against +
       (s.corners_for !== null ? ' · Córners ' + s.corners_for + ' - ' + s.corners_against : '') +
       (s.cards !== null ? ' · Tarjetas ' + s.cards : '') + '</div>';
  h += '<table><thead><tr><th>Fecha</th><th>Rival</th><th>Res.</th><th>Córn.</th><th>Tarj.</th></tr></thead><tbody>';
  for (var i = 0; i < t.matches.length; i++) {
    var m = t.matches[i], cls = m.result === 'G' ? 'pos' : m.result === 'P' ? 'neg' : '';
    h += '<tr title="' + esc(m.xi ? m.xi.formation + ': ' + m.xi.players.join(', ') : '') + '"><td>' + shortDate(m.date) + '</td><td>' +
         (m.venue === 'L' ? 'vs ' : '@ ') + esc(m.opponent) + '</td><td class="' + cls + '"><b>' + m.score + '</b></td><td>' +
         (m.corners ? m.corners[0] + '-' + m.corners[1] : '—') + '</td><td>' + (m.cards === null ? '—' : m.cards) + '</td></tr>';
  }
  h += '</tbody></table>';
  var lu = t.lineup;
  if (lu) h += '<div class="xi">' + (lu.confirmed ? '✅ ' : '👥 ') + '<b>' + esc(lu.formation) + '</b> ' + esc(lu.players.join(', ').replace(/ \(\d+ de \d+\)/g, '')) + '</div>';
  return h + '</div>';
}
function detailHtml(d){
  var h = '<div class="two">' + teamPanel(d.home) + teamPanel(d.away) + '</div>';
  if (d.h2h && d.h2h.length) {
    var x = [];
    for (var i = 0; i < d.h2h.length; i++) x.push('<span title="' + esc(d.h2h[i].match) + '">' + shortDate(d.h2h[i].date) + ' <b>' + d.h2h[i].score + '</b></span>');
    h += '<div class="h2h"><span class="muted">Entre ellos:</span> ' + x.join(' · ') + '</div>';
  }
  return h;
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
var _up = [], _upDay = '', _upValue = false, _alerts = {};
function upState(m){
  if (m.real_bet) return ['✅', 'Apuesta real'];
  if (m.old_model || !m.pick || !m.pick.label) return ['·', 'Sin análisis'];
  if (m.pick.pd_value) return ['✅', 'Vale la pena: PlayDoit suele pagar el mínimo'];
  if (m.pick.value) return ['🟡', 'Solo el mejor momio de Europa paga el mínimo'];
  return ['❌', 'No conviene'];
}
function upRow(m, i){
  var pk = m.pick || {}, st = upState(m), t = String(m.match).split(' vs ');
  var model = _sides(m, 'model'), pin = _sides(m, 'pin');
  var tip = 'Programa: ' + p100(model[0]) + ' / ' + p100(model[1]) + ' / ' + p100(model[2]) +
            '  ·  Pinnacle: ' + p100(pin[0]) + ' / ' + p100(pin[1]) + ' / ' + p100(pin[2]);
  var pick = pk.label ? '<b>' + esc(pk.label) + '</b> ' + p100(pk.model) + '<br><span class="muted">mín. ' + esc(pk.min_us || '—') +
             (pk.pd_est_us ? ' · paga ~' + esc(pk.pd_est_us) : '') + '</span>' : '<span class="muted">—</span>';
  var al = _alerts[m.match] ? ' <span title="' + esc(_alerts[m.match]) + '">⚠️</span>' : '';
  return '<div class="m" id="u' + i + '"><div class="mr" onclick="openUp(' + i + ')">' +
         '<div class="time">' + mxtime(m.match_date) + '<span class="small muted">' + esc(m.league) + '</span></div>' +
         '<div class="t h">' + esc(t[0]) + '</div>' +
         '<div class="pbw">' + probBar(model, false, tip) + probBar(pin, true, tip) + '</div>' +
         '<div class="t">' + esc(t[1] || '') + al + '</div>' +
         '<div class="pk" title="Lo que jugaría el programa · mínimo en PlayDoit · lo que paga aprox.">' + pick + '</div>' +
         '<div class="ic" title="' + esc(st[1]) + '">' + st[0] + '</div></div>' +
         '<div class="det" id="ud' + i + '"></div></div>';
}
function openUp(i){ var m = _up[i]; if (m && m.key) openDetail('ud' + i, m.key, m.match_date); }
function renderUp(){
  var lg = el('uleague').value, html = '', last = '', n = 0;
  for (var i = 0; i < _up.length; i++) {
    var m = _up[i], d = mxday(m.match_date);
    if (_upDay && d !== _upDay) continue;
    if (lg && m.league !== lg) continue;
    if (_upValue && upState(m)[0] === '❌') continue;
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
    // hoy: ¿hay algo que apostar?
    var today = mxday(), n = 0, good = 0;
    for (var j = 0; j < _up.length; j++) if (mxday(_up[j].match_date) === today) { n++; if (upState(_up[j])[0] === '✅') good++; }
    el('chiptoday').innerHTML = n ? (good ? '✅ Hoy: <b>' + good + '</b> con valor' : '❌ Hoy: nada que apostar') : 'Hoy: sin partidos';
    el('chiptoday').title = n + ' partidos hoy';
    getJSON('/api/lineups', function(l){
      _alerts = {};
      for (var k = 0; k < l.matches.length; k++) if (l.matches[k].level === 'alert') _alerts[l.matches[k].match] = l.matches[k].text;
      renderUp();
    }, function(){});
  }, function(msg){ el('uplist').innerHTML = '<div class="muted">' + esc(msg) + '</div>'; });
}
el('uleague').onchange = renderUp;
el('uvalue').onclick = function(){ _upValue = !_upValue; this.className = 'pill' + (_upValue ? ' on' : ''); renderUp(); };

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
  var pickTxt = '<span class="muted">·</span>';
  if (pk) {
    var played = pk.result === 'win' || pk.result === 'loss' || pk.result === 'half_win' || pk.result === 'half_loss';
    pickTxt = '<span' + (pk.old_model ? ' style="opacity:.6"' : '') + ' title="' + esc(pk.label + ' · programa ' + p100(pk.model) + ' vs mercado ' + p100(pk.market) +
              (pk.winner ? ' · tuvo razón el ' + pk.winner : '') + (pk.old_model ? ' · modelo con error (antes del 22 oct)' : '') +
              (pk.rebuilt ? ' · reconstruido con la predicción guardada antes del partido' : '')) + '">' + (pk.rebuilt ? '↺ ' : '') +
              (played ? ok(pk.result === 'win' || pk.result === 'half_win') : '⏳') + ' ' + esc(pk.label) +
              (pk.profit !== null && played ? ' <span class="' + (pk.profit >= 0 ? 'pos' : 'neg') + '">' + money(pk.profit) + '</span>' : '') + '</span>';
  }
  return '<tr class="r" onclick="openPast(' + i + ')"><td class="hide-sm muted small">' + esc(r.league) + '</td><td style="text-align:right">' + esc(r.home) +
         '</td><td class="sc">' + r.score[0] + ' - ' + r.score[1] + '</td><td>' + esc(r.away) + '</td><td>' +
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
function loadPast(){
  getJSON('/api/board/past', function(d){
    _past = d.rows;
    var s = d.summary;
    el('pkpis').innerHTML = kpi(s.matches, 'Partidos desde ' + shortDate(d.since)) +
      kpi(p100(s.model_hit), '🤖 Programa acertó el favorito (' + s.analyzed + ')') +
      kpi(p100(s.pin_hit), '🏦 Pinnacle acertó el favorito') +
      kpi(s.picks_fixed.n ? s.picks_fixed.won + ' / ' + s.picks_fixed.n : '—', '🎯 Picks · modelo corregido (desde 22 oct)' +
          (s.picks_fixed.priced ? ' · ' + money(s.picks_fixed.profit) : ''), s.picks_fixed.profit >= 0 ? 'pos' : 'neg') +
      kpi(s.picks_old.n ? s.picks_old.won + ' / ' + s.picks_old.n : '—', '↺ Picks · modelo con error (antes del 22 oct)' +
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
    h += '<div class="day">Desde septiembre</div><div class="tbl"><table><thead><tr><th>Fecha</th><th>Rival</th><th></th><th>Res.</th><th>Córners</th><th>Tarjetas</th><th class="hide-sm">Liga</th></tr></thead><tbody>';
    for (var i = d.matches.length - 1; i >= 0; i--) {
      var m = d.matches[i], cls = m.result === 'G' ? 'pos' : m.result === 'P' ? 'neg' : '';
      h += '<tr><td>' + shortDate(m.date) + '</td><td>' + esc(m.opponent) + '</td><td class="muted">' + (m.venue === 'L' ? 'Local' : 'Visita') +
           '</td><td class="' + cls + '"><b>' + m.result + ' ' + m.gf + '-' + m.ga + '</b></td><td>' + (m.cf === null ? '—' : m.cf + '-' + m.ca) +
           '</td><td>' + (m.cards === null ? '—' : m.cards) + '</td><td class="hide-sm muted small">' + esc(m.league) + '</td></tr>';
    }
    h += '</tbody></table></div>';
    el('teamview').innerHTML = h;
    var labels = [], gf = [], ga = [], cf = [], ca = [];
    for (var j = 0; j < d.matches.length; j++) {
      var mm = d.matches[j];
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
function loadCharts(){
  getJSON('/api/board/charts', function(d){
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
    chart('cpicks', { type: 'line', data: { labels: d.picks.dates, datasets: [
      { label: '↺ Modelo con error', data: d.picks.old, borderColor: '#94a3b8', pointRadius: 0, fill: false, spanGaps: false },
      { label: 'Modelo corregido', data: d.picks.fixed, borderColor: '#22c55e', fill: false, spanGaps: false }] },
      options: { legend: legend, scales: axes } });
    var pl = [], pv = [], pc = [];
    for (var j = 0; j < d.proposals.length; j++) {
      var w = d.proposals[j];
      pl.push(shortDate(w.week) + (w.old_model ? ' *' : ''));
      pv.push(w.profit_1u);
      pc.push(w.old_model ? (w.profit_1u >= 0 ? 'rgba(34,197,94,.35)' : 'rgba(239,68,68,.35)') : (w.profit_1u >= 0 ? '#22c55e' : '#ef4444'));
    }
    chart('cprops', { type: 'bar', data: { labels: pl, datasets: [{ data: pv, backgroundColor: pc }] },
      options: { legend: nolegend, scales: axes, tooltips: { callbacks: { footer: function(it){ return d.proposals[it[0].index].old_model ? 'Modelo con error (antes del 22-oct)' : ''; } } } } });
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
loadHeader();
loadUpcoming();
showTab((location.hash || '').replace('#', '') || _storeGet('tab2', 'proximos'));
setInterval(function(){ loadHeader(); loadUpcoming(); }, 5 * 60000);   // datos cada 5 min
