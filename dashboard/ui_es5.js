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
    loadKpis(); loadBets(); loadScorers(); loadGh();
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
loadEquity();
loadBy();
loadClv();
loadBank();
loadBets();
loadScorers();
loadGh();
loadNarrativeSec();
setRefresh(parseInt(_storeGet('refreshMins', '5'), 10) || 0);   // auto-refresco (5 min por defecto)
