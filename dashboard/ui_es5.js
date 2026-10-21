// ============================================================
// Dashboard JS — ES5 universal (compatible con cualquier navegador)
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

// Fechas UTC → hora Mexico (UTC-6 fijo desde 2022, cálculo manual ES5)
var MX_MESES = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'];
function mxdate(s){
  try {
    var t = new Date(s);
    if (isNaN(t.getTime())) return String(s).slice(0, 16);
    var mx = new Date(t.getTime() - 6 * 3600000);
    function p2(n){ return (n < 10 ? '0' : '') + n; }
    return p2(mx.getUTCDate()) + ' ' + MX_MESES[mx.getUTCMonth()] + ', ' +
           p2(mx.getUTCHours()) + ':' + p2(mx.getUTCMinutes());
  } catch(e) { return String(s).slice(0, 16); }
}

function err(msg){
  var e = document.getElementById('err');
  e.style.display = 'block';
  e.textContent = '⚠️ ' + msg;
}

// ── Sin conexión con el programa (v1.6.0) ──
// status 0 = el navegador no llegó al programa: cerrado o reiniciándose.
// Antes cada sección mostraba "HTTP 0". El supervisor lo vuelve a abrir
// solo; la página conserva lo último que mostró, avisa arriba y se recarga
// sola en cuanto el programa responde.
var OFFLINE_MSG = 'El programa del dashboard no responde';
var _offlineSince = null, _offlineTimer = null;
function _offlineBanner(){
  var el = document.getElementById('offline');
  if (!el) {
    el = document.createElement('div');
    el.id = 'offline';
    el.style.cssText = 'position:sticky;top:0;z-index:10;background:#3a2a14;color:#fbbf24;' +
                       'padding:10px 14px;border-radius:8px;margin:10px 0;font-size:.9rem';
    document.body.insertBefore(el, document.body.firstChild);
  }
  return el;
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

// ── KPIs ──
function loadKpis(){
  getJSON('/api/kpis', function(d){
    var cards = [
      ['Bankroll', d.bankroll === null ? '—' : d.bankroll.toFixed(1) + 'u', ''],
      ['Profit 90d', money(d.profit), d.profit >= 0 ? 'pos' : 'neg'],
      ['ROI 90d', pct(d.roi), d.roi >= 0 ? 'pos' : 'neg'],
      ['Win rate', d.win_rate === null ? '—' : (d.win_rate * 100).toFixed(1) + '%', ''],
      ['Bets 90d', d.bets_90d, ''],
      ['Resueltas', d.resolved, ''],
      ['Pendientes', d.pending, ''],
      ['Brier', d.brier === null ? '—' : d.brier.toFixed(3), ''],
      ['CLV medio (n=' + d.clv_n + ')', d.clv_avg === null ? '—' : (d.clv_avg >= 0 ? '+' : '') + (d.clv_avg * 100).toFixed(2) + '%', d.clv_avg >= 0 ? 'pos' : 'neg']
    ];
    var html = '';
    for (var i = 0; i < cards.length; i++) {
      html += '<div class="card"><div class="lbl">' + cards[i][0] + '</div><div class="val ' + cards[i][2] + '">' + cards[i][1] + '</div></div>';
    }
    document.getElementById('kpis').innerHTML = html;
    document.getElementById('sub').textContent = 'Datos: ' + (d.window || '90d') + ' · solo lectura · ' + new Date().toLocaleString('es-MX');
  }, function(m){ err('KPIs: ' + m); });
}

function cctx(id){ return document.getElementById(id).getContext('2d'); }

// ── Gráficas ──
function loadEquity(){
  getJSON('/api/equity', function(d){
    new Chart(cctx('equity'), { type: 'line',
      data: { labels: d.dates, datasets: [{ data: d.cumulative, borderWidth: 2, pointRadius: 0, borderColor: '#3b82f6', fill: true, backgroundColor: 'rgba(59,130,246,.08)' }] },
      options: { plugins: { legend: { display: false } }, scales: { xAxes: [{ ticks: { maxTicksLimit: 8 } }] } } });
    var colors = [];
    for (var i = 0; i < d.daily.length; i++) colors.push(d.daily[i] >= 0 ? '#22c55e' : '#ef4444');
    new Chart(cctx('daily'), { type: 'bar',
      data: { labels: d.daily_dates, datasets: [{ data: d.daily, backgroundColor: colors }] },
      options: { plugins: { legend: { display: false } }, scales: { xAxes: [{ ticks: { maxTicksLimit: 10 } }] } } });
  }, function(m){ err('Curva: ' + m); });
}

function loadBy(){
  var dims = [['market', 'bymarket'], ['league', 'byleague']];
  for (var k = 0; k < dims.length; k++) {
    (function(dim, id){
      getJSON('/api/by/' + dim, function(d){
        var colors = [];
        for (var i = 0; i < d.roi.length; i++) colors.push(d.roi[i] >= 0 ? '#22c55e' : '#ef4444');
        new Chart(cctx(id), { type: 'bar',
          data: { labels: d.labels, datasets: [{ label: 'ROI %', data: d.roi, backgroundColor: colors }] },
          options: { indexAxis: 'y', plugins: { legend: { display: false },
            tooltip: { callbacks: { afterLabel: function(c){ return 'n=' + d.n[c.dataIndex] + ' · wr ' + d.wr[c.dataIndex] + '%'; } } } },
            scales: { xAxes: [{ ticks: { callback: function(v){ return v + '%'; } } }] } } });
        // valor = clave cruda ("under_3.5"), texto = nombre visible: la API
        // filtra por la clave (con el nombre, la tabla quedaba vacía)
        var keys = d.keys || d.labels;
        var sel = document.getElementById(dim === 'market' ? 'fmarket' : 'fleague');
        for (var j = 0; j < d.labels.length; j++) sel.add(new Option(d.labels[j], keys[j]));
      }, function(m){ err(dim + ': ' + m); });
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

function loadBank(){
  getJSON('/api/equity', function(d){
    var bank = [];
    for (var i = 0; i < d.cumulative.length; i++) bank.push(100 + d.cumulative[i]);
    new Chart(cctx('bankchart'), { type: 'line',
      data: { labels: d.dates, datasets: [{ data: bank, borderWidth: 2, pointRadius: 0, borderColor: '#eab308' }] },
      options: { plugins: { legend: { display: false } }, scales: { xAxes: [{ ticks: { maxTicksLimit: 6 } }] } } });
  }, function(){ /* sin datos */ });
}

// ── Tabla de apuestas ──
var _betsRaw = [];
function loadBets(){
  var q = 'status=' + encodeURIComponent(document.getElementById('fstatus').value) +
          '&market=' + encodeURIComponent(document.getElementById('fmarket').value) +
          '&league=' + encodeURIComponent(document.getElementById('fleague').value) +
          '&limit=150';
  var body = document.getElementById('betsbody');
  getJSON('/api/bets?' + q, function(d){
    _betsRaw = d.bets;
    var html = '';
    for (var i = 0; i < d.bets.length; i++) {
      var b = d.bets[i];
      var rk = String(b.result_key || b.result || 'pending').toLowerCase();
      var off = rk === 'cancelled';   // cancelada antes del partido: no se apuesta
      var prof = (rk === 'pending' || off) ? '' : money(b.profit || 0);
      var cls = rk === 'win' ? 'win' : rk === 'loss' ? 'loss' : rk === 'pending' ? 'pending' :
                off ? 'cancelled' : 'push';
      var clv = b.clv === '' ? '—' : (Number(b.clv) * 100).toFixed(1) + '%';
      // Formato americano, como PlayDoit (+128 / -140). La mejor cuota
      // europea es solo referencia; lo que decide es el mínimo de PlayDoit.
      var minUs = off ? '<span title="Cancelada antes del partido: ya no tiene valor">no apostar</span>' :
                  b.min_us ? '<span title="' + (b.min_hint || '') + '">' + b.min_us + ' o mejor</span>' : '—';
      html += '<tr><td>' + mxdate(b.match_date) + '</td><td>' + b.match + '</td><td>' +
              (b.league || '') + '</td><td>' + b.market + '</td><td>' +
              (b.probability * 100).toFixed(0) + '%</td><td title="decimal ' + b.odds + '">' +
              (b.odds_us || '—') + '</td><td>' + minUs + '</td><td>' +
              b.stake + 'u</td><td><span class="pill ' + cls + '">' + b.result + '</span></td><td>' +
              prof + '</td><td>' + clv + '</td></tr>';
    }
    body.innerHTML = html || '<tr><td colspan="11" style="color:var(--muted)">Sin apuestas</td></tr>';
  }, function(m){ body.innerHTML = '<tr><td colspan="11" style="color:var(--muted)">' + m + '</td></tr>'; });
}

// ── Goleadores ──
function loadScorers(){
  var body = document.getElementById('scorersbody');
  getJSON('/api/scorers', function(d){
    var html = '';
    for (var i = 0; i < d.picks.length; i++) {
      var p = d.picks[i];
      var rk = String(p.result_key || p.result || 'pending').toLowerCase();
      var cls = rk === 'win' ? 'win' : rk === 'loss' ? 'loss' : 'pending';
      html += '<tr><td>' + mxdate(p.match_date) + '</td><td>' + p.match + '</td><td>' + p.player +
              '</td><td>' + p.team + '</td><td>' + (p.probability * 100).toFixed(0) + '%</td><td>' +
              (p.fair_us || '—') + '</td><td><span class="pill ' + cls + '">' +
              p.result + '</span></td></tr>';
    }
    body.innerHTML = html || '<tr><td colspan="7" style="color:var(--muted)">Sin goleadores aún</td></tr>';
  }, function(){ body.innerHTML = '<tr><td colspan="7" style="color:var(--muted)">Sin goleadores aún</td></tr>'; });
}

// ── Pick del programa (v1.7.2): lo que jugaría, con o sin valor ──
var _picks = {};
function pickHtml(pk){
  if (!pk || !pk.label) return '—';
  var head = '<b>' + esc(pk.label) + '</b> · programa ' + Math.round(pk.model * 100) + '% vs mercado ' + Math.round(pk.market * 100) + '%';
  var tail = pk.min_us ? ' · PlayDoit ' + esc(pk.min_us) + ' o mejor' : '';
  var tag = pk.value ? ' <span class="pill win">con valor</span>' :
            pk.lean ? ' <span class="pill pending" title="El programa ve más que el mercado, pero el mejor momio todavía no paga el mínimo">sin valor aún</span>' :
            ' <span class="pill cancelled" style="text-decoration:none" title="No ve nada mejor que el mercado: es lo más probable, no una ventaja">lo más probable</span>';
  return head + tail + tag;
}

// ── Lo que dice el programa (v1.7.0) ──
function p3(arr){
  if (!arr) return '—';
  var out = [];
  for (var i = 0; i < arr.length; i++) out.push(arr[i] === null || arr[i] === undefined ? '—' : Math.round(arr[i] * 100));
  return out.join(' / ');
}
function _sidesArr(sides, key){
  var a = [];
  for (var i = 0; i < sides.length; i++) a.push(sides[i][key]);
  return a;
}
function esc(s){ return String(s === null || s === undefined ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function loadPredictions(){
  var body = document.getElementById('predbody');
  if (!body) return;
  getJSON('/api/predictions', function(d){
    var html = '';
    for (var i = 0; i < d.matches.length; i++) {
      var m = d.matches[i];
      var mins = [], anyValue = false;
      for (var j = 0; j < m.sides.length; j++) {
        var s = m.sides[j];
        mins.push(s.min_us ? s.min_us : '—');
        if (s.value_at_best) anyValue = true;
      }
      var lam = m.lam ? m.lam[0].toFixed(2) + ' - ' + m.lam[1].toFixed(2) : '—';
      var likely = m.likely ? m.likely.score + ' (' + Math.round(m.likely.prob * 100) + '%)' : '—';
      var extra = [];
      if (m.p_over25 !== null && m.p_over25 !== undefined) extra.push('Más de 2.5: ' + Math.round(m.p_over25 * 100) + '%');
      if (m.p_btts !== null && m.p_btts !== undefined) extra.push('Ambos anotan: ' + Math.round(m.p_btts * 100) + '%');
      var state = m.real_bet ? '<span class="pill win">Apuesta real</span>' :
                  anyValue ? '<span class="pill pending" title="El mejor momio de Europa paga el mínimo; en PlayDoit revisa si llega">Valor al mejor precio</span>' :
                  '<span class="pill cancelled" style="text-decoration:none">Sin valor</span>';
      html += '<tr><td>' + mxdate(m.match_date) + '</td><td>' + esc(m.match) + '</td><td>' + esc(m.league) +
              '</td><td title="' + esc(extra.join(' · ')) + '">' + lam + '</td><td>' + likely + '</td><td>' +
              p3(_sidesArr(m.sides, 'model')) + '</td><td>' + p3(_sidesArr(m.sides, 'pin')) + '</td><td>' +
              (m.old_model ? '<span style="color:var(--muted)" title="Analizado antes de la corrección del modelo (21-oct); se actualiza en la siguiente corrida">modelo anterior</span>' : esc(m.reading)) +
              '</td><td>' + (m.old_model ? '<span style="color:var(--muted)">modelo anterior</span>' : pickHtml(m.pick)) +
              '</td><td>' + mins.join(' / ') + '</td><td>' + state + '</td></tr>';
      _picks[m.match] = m.old_model ? null : m.pick;
    }
    body.innerHTML = html || '<tr><td colspan="11" style="color:var(--muted)">Sin partidos en los próximos 3 días</td></tr>';
    loadLineups();
  }, function(msg){ body.innerHTML = '<tr><td colspan="11" style="color:var(--muted)">' + esc(msg) + '</td></tr>'; });
}

function _luTeam(t){
  if (!t || !t.published) return '<span style="color:var(--muted)">sin publicar</span>';
  var out = esc(t.formation || '');
  if (t.known === false) return out + ' <span style="color:var(--muted)">(sin historial)</span>';
  var miss = t.missing || [];
  if (!miss.length) return out + ' · once habitual';
  var names = [];
  for (var i = 0; i < miss.length; i++) names.push(esc(miss[i].name) + (miss[i].bench ? ' <span style="color:var(--muted)">(banca)</span>' : ''));
  return out + ' · faltan ' + miss.length + ': ' + names.join(', ');
}
function loadLineups(){
  var body = document.getElementById('lubody');
  if (!body) return;
  getJSON('/api/lineups', function(d){
    var html = '';
    for (var i = 0; i < d.matches.length; i++) {
      var m = d.matches[i];
      var color = m.level === 'alert' || m.level === 'weak' ? '#fbbf24' : m.level === 'pending' ? 'var(--muted)' : 'var(--text)';
      html += '<tr><td>' + mxdate(m.match_date) + '</td><td>' + esc(m.match) + '<div style="color:var(--muted);font-size:.72rem">' +
              esc(m.league) + '</div></td><td>' + _luTeam(m.home) + '</td><td>' + _luTeam(m.away) +
              '</td><td style="color:' + color + '">' + esc(m.text) + '</td><td>' +
              (m.match in _picks ? (_picks[m.match] ? pickHtml(_picks[m.match]) : '<span style="color:var(--muted)">modelo anterior</span>') : '—') + '</td></tr>';
    }
    body.innerHTML = html || '<tr><td colspan="6" style="color:var(--muted)">Sin partidos hoy</td></tr>';
  }, function(msg){ body.innerHTML = '<tr><td colspan="6" style="color:var(--muted)">' + esc(msg) + '</td></tr>'; });
}

var SIDE_ES = { home: 'Local', draw: 'Empate', away: 'Visitante' };
function loadHistory(){
  var body = document.getElementById('histbody'), k = document.getElementById('histkpis');
  if (!body) return;
  getJSON('/api/predictions/history?days=14', function(d){
    function cards(sm, title){
      if (!sm) return '';
      return '<div class="card"><div class="lbl">' + title + ' · partidos</div><div class="val">' + sm.n + '</div></div>' +
             '<div class="card"><div class="lbl">' + title + ' · acertó el favorito</div><div class="val">Programa ' +
               Math.round(sm.model_hit * 100) + '% · Pinnacle ' + Math.round(sm.pin_hit * 100) + '%</div></div>' +
             '<div class="card"><div class="lbl">' + title + ' · prob. dada a lo que pasó</div><div class="val">Programa ' +
               Math.round(sm.model_p_actual * 100) + '% · Pinnacle ' + Math.round(sm.pin_p_actual * 100) + '%</div></div>';
    }
    k.innerHTML = cards(d.fixed, 'Desde la corrección') + cards(d.all, '14 días');
    var html = '';
    for (var i = 0; i < d.rows.length; i++) {
      var r = d.rows[i];
      html += '<tr><td>' + mxdate(r.match_date) + (r.fixed ? '' : ' <span style="color:var(--muted)" title="Antes de la corrección del modelo">·</span>') +
              '</td><td>' + esc(r.match) + '</td><td>' + esc(r.league) + '</td><td>' + r.score + ' (' + SIDE_ES[r.outcome] +
              ')</td><td>' + p3(r.model) + '</td><td>' + p3(r.pin) + '</td><td>' + (r.model_hit ? '✅' : '❌') +
              '</td><td>' + (r.pin_hit ? '✅' : '❌') + '</td></tr>';
    }
    body.innerHTML = html || '<tr><td colspan="8" style="color:var(--muted)">Sin partidos con resultado todavía</td></tr>';
  }, function(msg){ body.innerHTML = '<tr><td colspan="8" style="color:var(--muted)">' + esc(msg) + '</td></tr>'; k.innerHTML = ''; });
}

function loadProposals(){
  var body = document.getElementById('propbody'), sum = document.getElementById('propsum');
  if (!body) return;
  getJSON('/api/proposals?days=14', function(d){
    var sh = '';
    for (var i = 0; i < d.summary.length; i++) {
      var g = d.summary[i];
      sh += '<tr><td>' + esc(g.decision) + '</td><td>' + g.n + '</td><td>' + g.won + '</td><td class="' +
            (g.profit_1u >= 0 ? 'pos' : 'neg') + '">' + money(g.profit_1u) + '</td><td>' + pct(g.roi) + '</td></tr>';
    }
    sum.innerHTML = sh || '<tr><td colspan="5" style="color:var(--muted)">Todavía sin propuestas resueltas</td></tr>';
    var html = '';
    for (var j = 0; j < d.rows.length; j++) {
      var r = d.rows[j];
      var rk = String(r.result_key || 'pending').toLowerCase();
      var cls = rk === 'win' ? 'win' : rk === 'loss' ? 'loss' : rk === 'pending' ? 'pending' : 'push';
      var dec = esc(r.decision) + (r.fake_odds ? ' <span style="color:var(--muted)" title="Momio de prueba, no real">(momio de prueba)</span>' : '');
      html += '<tr><td>' + mxdate(r.match_date) + '</td><td>' + esc(r.match) + '</td><td>' + esc(r.market) + '</td><td>' +
              (r.probability === null ? '—' : Math.round(r.probability * 100) + '%') + '</td><td>' +
              (r.pin_prob === null ? '—' : Math.round(r.pin_prob * 100) + '%') + '</td><td>' + (r.odds_us || '—') +
              '</td><td>' + (r.min_us || '—') + '</td><td>' + dec + '</td><td><span class="pill ' + cls + '">' + esc(r.result) +
              '</span></td><td>' + (r.profit_1u === null ? '' : money(r.profit_1u)) + '</td></tr>';
    }
    body.innerHTML = html || '<tr><td colspan="10" style="color:var(--muted)">Sin propuestas</td></tr>';
  }, function(msg){ body.innerHTML = '<tr><td colspan="10" style="color:var(--muted)">' + esc(msg) + '</td></tr>'; sum.innerHTML = ''; });
}

// ── Panel de control GitHub ──
function loadGh(){
  var msg = document.getElementById('ghmsg'), body = document.getElementById('ghbody');
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
  var msg = document.getElementById('ghmsg');
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

// ── Narrador IA ──
function loadNarrative(){
  var sec = document.getElementById('narrsec');
  getJSON('/api/narrative', function(d){
    sec.style.display = 'block';
    document.getElementById('narrtext').textContent = d.narrative;
    document.getElementById('narreng').textContent = 'Generado por ' + d.engine + ' · ' + d.created;
  }, function(){ sec.style.display = 'none'; });
}

function genNarrative(){
  var b = document.getElementById('genbtn');
  b.textContent = '⏳ Generando (puede tardar ~1 min)…';
  b.disabled = true;
  var x = new XMLHttpRequest();
  x.open('POST', '/api/narrative/generate', true);
  x.onreadystatechange = function(){
    if (x.readyState !== 4) return;
    b.textContent = '🧠 Generar diagnóstico ahora';
    b.disabled = false;
    var d = null;
    try { d = JSON.parse(x.responseText); } catch(e2) {}
    if (d && d.ok) { loadNarrative(); }
    else { alert('⚠️ ' + ((d && d.msg) || 'No se pudo generar')); }
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
  var blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
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

// ── Auto-refresco (KPIs + tablas; las gráficas se redibujan con F5) ──
var _refreshTimer = null;
function setRefresh(mins){
  if (_refreshTimer) clearInterval(_refreshTimer);
  if (mins > 0) _refreshTimer = setInterval(function(){
    loadKpis(); loadBets(); loadScorers(); loadGh(); loadPredictions(); loadHistory(); loadProposals(); loadLineups();
  }, mins * 60000);
}

// ── Versión + banner de actualización ──
function loadVersion(){
  getJSON('/api/version', function(d){
    document.getElementById('ver').textContent = 'v' + d.current;
    if (d.update_available) {
      var u = document.getElementById('upd');
      u.style.display = 'block';
      u.innerHTML = '🔄 Nueva versión disponible: <b>v' + d.latest + '</b> (tienes v' + d.current + ') — <a href="' + d.release_url + '" target="_blank" style="color:var(--green)">descargar</a>';
    }
  }, function(){});
}

// ── Diagnóstico IA ──
function loadNarrativeSec(){
  var sec = document.getElementById('narrsec');
  getJSON('/api/narrative', function(d){
    sec.style.display = 'block';
    document.getElementById('narrtext').textContent = d.narrative;
    document.getElementById('narreng').textContent = 'Generado por ' + d.engine + ' · ' + d.created;
  }, function(){ sec.style.display = 'none'; });
}

function genNarrative(){
  var b = document.getElementById('genbtn');
  b.textContent = '⏳ Generando (~1 min)…';
  b.disabled = true;
  var x = new XMLHttpRequest();
  x.open('POST', '/api/narrative/generate', true);
  x.onreadystatechange = function(){
    if (x.readyState !== 4) return;
    b.textContent = '🧠 Generar diagnóstico ahora';
    b.disabled = false;
    if (x.status === 200) loadNarrativeSec();
  };
  x.send();
}

// ── ARRANQUE (fix 21-sep: el archivo definía todas las funciones pero nada
// las invocaba — la página se quedaba en "Conectando…" para siempre) ──
// ── Layout de gráficas (r14-fix): Chart.js v2 necesita contenedor con
// altura fija y position:relative — sin esto el canvas se desborda sobre
// las secciones de abajo (superposición reportada por el usuario) ──
if (window.Chart && Chart.defaults && Chart.defaults.global) {
  Chart.defaults.global.maintainAspectRatio = false;
  Chart.defaults.global.responsive = true;
}
(function(){
  var st = document.createElement('style');
  st.textContent = '.chartbox{position:relative;height:300px;overflow:hidden}' +
                   '.chartbox canvas{width:100%!important;height:280px!important}';
  document.head.appendChild(st);
})();

// Filtros: hasta el 24-sep-26 no tenían evento de cambio — elegir un estado,
// mercado o liga no hacía nada hasta el siguiente auto-refresco.
function _onChange(id, fn){ var el = document.getElementById(id); if (el) el.onchange = fn; }
_onChange('fstatus', loadBets);
_onChange('fmarket', loadBets);
_onChange('fleague', loadBets);
_onChange('frefresh', function(){
  var mins = parseInt(document.getElementById('frefresh').value, 10) || 0;
  _storeSet('refreshMins', String(mins));
  setRefresh(mins);
});
(function(){
  var saved = _storeGet('refreshMins', '5');
  var sel = document.getElementById('frefresh');
  if (sel) sel.value = saved;
})();

loadVersion();
loadKpis();
loadLineups();
loadPredictions();
loadHistory();
loadProposals();
loadEquity();
loadBy();
loadClv();
loadBank();
loadBets();
loadScorers();
loadGh();
loadNarrativeSec();
setRefresh(parseInt(_storeGet('refreshMins', '5'), 10) || 0);   // auto-refresco (5 min por defecto)
