LOGIN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Elisa</title>
<style>
body{font-family:Arial,sans-serif;background:#fff}
.card{width:420px;margin:80px auto;padding:24px;border:1px solid #ccc;background:#f4f4f4}
.form-control{display:block;width:100%;margin:6px 0 14px;padding:6px;box-sizing:border-box}
.btn{padding:6px 12px;border:0;cursor:pointer}
.btn-primary{background:#337ab7;color:#fff;width:100%}
button[disabled]{opacity:.65;cursor:not-allowed}
.text-danger{color:#a94442;min-height:20px}
</style></head>
<body>
<div class="card">
  <form name="loginForm" novalidate id="login-form">
    <label for="email">Email</label>
    <input id="email" type="email" name="email" class="form-control" placeholder="Your e-mail or user name" required autofocus>
    <label>Password</label>
    <input type="password" class="form-control auth-pass-control" placeholder="Your password" aria-label="Password" required>
    <button type="button" class="btn btn-password" id="eye" aria-label="Show password">&#128065;</button>
    <a href="#" class="forgot">Forgot Password?</a>
    <button type="submit" class="btn btn-primary w-100" id="login-btn" disabled>Login</button>
    <div id="login-message" class="text-danger"></div>
  </form>
  <p>Need help logging in or creating a new user account? Contact support for assistance.</p>
</div>
<script>
const LOGIN_DELAY_MS = __LOGIN_DELAY_MS__;
const email = document.getElementById('email');
const pwd = document.querySelector('input[type=password]');
const btn = document.getElementById('login-btn');
const msg = document.getElementById('login-message');
function sync() { btn.disabled = !(email.value.trim() && pwd.value); }
email.addEventListener('input', sync);
pwd.addEventListener('input', sync);
document.getElementById('eye').addEventListener('click', () => { pwd.type = pwd.type === 'password' ? 'text' : 'password'; });
document.getElementById('login-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  btn.disabled = true;
  msg.textContent = '';
  const res = await fetch('/api/login', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                         body: JSON.stringify({email: email.value, password: pwd.value})});
  let data = {};
  try { data = await res.json(); } catch (e) {}
  if (res.ok && data.ok) { setTimeout(() => { location.href = '/my/dashboard/main-dashboard'; }, LOGIN_DELAY_MS); return; }
  msg.textContent = data.message || 'Something went wrong.';
  sync();
});
</script>
</body></html>
"""

DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Elisa</title>
<style>
body{margin:0;font-family:Arial,sans-serif;padding-top:101px;padding-bottom:57px}
.navbar-fixed-top{position:fixed;top:0;left:0;right:0;height:101px;background:#fff;border-bottom:1px solid #ddd;z-index:10}
.footer{position:fixed;bottom:0;left:0;right:0;height:57px;background:#e6e6e6;z-index:10;text-align:center}
.nav-tabs{list-style:none;display:flex;gap:18px;padding:8px 24px;margin:0}
.nav-tabs a{cursor:pointer;color:#333}
.status-filters{padding:8px 24px}
.btn{padding:4px 10px;border:1px solid #888;background:#f5f5f5;cursor:pointer}
.btn[disabled]{opacity:.5;cursor:not-allowed}
.btn-primary{background:#2c4a7a;color:#fff}
.loading-dots{text-align:center;height:25px}
.header{display:flex;align-items:center;justify-content:center;gap:24px;background:#6a9bd8;color:#fff;padding:6px 24px}
.fa{display:inline-block;width:24px;height:24px;line-height:24px;cursor:pointer;text-align:center;font-style:normal}
.fa-angle-left::before{content:'\\2039'} .fa-angle-right::before{content:'\\203A'}
table.calendar{border-collapse:collapse;width:100%}
th.day-name{border:1px solid #333}
td.calendar-day{border:1px solid #333;vertical-align:top;height:122px;width:14%}
td.past-date{background:#f8f8f8} td.today{background:#e0ebf7}
.date-cell{cursor:pointer;display:flex;flex-direction:column;height:100%}
.d-flex{display:flex} .flex-1{flex:1}
.badge{display:inline-block;padding:2px 6px;border-radius:4px;font-size:11px;color:#fff;background:#777;cursor:pointer}
.badge-success{background:#5cb85c} .badge-warning{background:#f0ad4e} .badge-yellow{background:#e8e83a;color:#333} .badge-primary{background:#337ab7}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:50;display:none}
.modal.show{display:block}
.modal-dialog{background:#fff;width:520px;margin:140px auto;padding:16px}
.toast{position:fixed;top:110px;right:20px;background:#333;color:#fff;padding:10px;z-index:60}
</style></head>
<body>
<div class="header-accessibility navbar navbar-default-white navbar-fixed-top">
  <div class="brand">elisa</div>
  <div class="dropdown-menu" style="display:none"><a href="" tabindex="200" role="menuitem" alt="Open Work Orders">Open Work Orders </a></div>
  <span class="dropdown-toggle" id="user-menu">Test Interpreter &#9662;</span>
  <ul class="nav nav-tabs">
    <li><a data-tab="home">Home</a></li><li><a>Past Due</a></li><li><a>My Work Orders</a></li>
    <li><a data-tab="owo">Open Work Orders</a></li><li><a>Profile</a></li><li><a>Knowledge Library</a></li>
    <li><a>My Resources</a></li><li><a>Training</a></li>
  </ul>
</div>
<main>
  <section id="home-view"><h3>Welcome</h3><p>Home dashboard.</p></section>
  <section id="owo-view" style="display:none">
    <div class="status-filters">
      <button class="btn btn-sm btn-primary active status-btn" uib-tooltip="New"><span class="small-view-btn">Available</span></button>
      <button class="btn btn-sm status-btn"><span class="small-view-btn">Applied</span> <span class="count" data-count="applied">0</span></button>
      <button class="btn btn-sm status-btn"><span class="small-view-btn">Assigned</span> <span class="count" data-count="assigned">0</span></button>
      <button class="btn btn-sm status-btn"><span class="small-view-btn">Confirm</span> <span class="count" data-count="confirmed">0</span></button>
    </div>
    <div class="filters">Service Type: 3 Selected 3 / 3 &middot; Reporting Court: 75 Selected 75 / 75 &middot; Language: 1 Selected 1 / 1</div>
    <div class="loading-dots" id="loader" style="display:none">&bull; &bull;</div>
    <div class="calendar-panel">
      <div class="col-sm-12 d-flex justify-content-center"><div class="header">
        <i class="fa fa-angle-left" id="prev"></i>
        <span class="font-quicksand calender-monyh-header" id="month-title"></span>
        <i class="fa fa-angle-right" id="next"></i>
      </div></div>
      <table class="calendar"><thead><tr>
        <th class="day-name">Sun</th><th class="day-name">Mon</th><th class="day-name">Tue</th><th class="day-name">Wed</th>
        <th class="day-name">Thu</th><th class="day-name">Fri</th><th class="day-name">Sat</th>
      </tr></thead><tbody id="cal-body"></tbody></table>
    </div>
  </section>
</main>
<div class="footer">Proprietary and Confidential</div>
<script>
const CFG = __CONFIG__;
const MONTHS = ['January','February','March','April','May','June','July','August','September','October','November','December'];
const WORDS = {available: ['Available', 'badge-primary'], applied: ['Applied', 'badge-yellow'],
               assigned: ['Assigned', 'badge-warning'], confirmed: ['Confirmed', 'badge-success']};
const view = {year: parseInt(CFG.today.slice(0, 4), 10), month: parseInt(CFG.today.slice(5, 7), 10)};
const body = document.getElementById('cal-body');
const statusButtons = () => Array.from(document.querySelectorAll('.status-btn'));
const busy = () => statusButtons()[0].disabled;

function setLoading(on) {
  statusButtons().forEach(b => { b.disabled = on; });
  document.getElementById('loader').style.display = on ? 'block' : 'none';
}
async function api(url, opts) {
  const res = await fetch(url, opts);
  if (res.status === 401) { location.href = '/login'; throw new Error('signed out'); }
  return res;
}
async function load() {
  setLoading(true);
  try {
    const res = await api(`/api/mock/calendar?year=${view.year}&month=${view.month}`);
    render(await res.json());
  } finally { setLoading(false); }
}
function tags(iso, half, data) {
  const counts = (data.cells[iso] || {})[half] || {};
  return ['available', 'applied', 'assigned', 'confirmed'].filter(s => counts[s]).map(s =>
    `<div class="ng-scope"><span class="badge ${WORDS[s][1]} ng-binding" data-slot="${iso} ${half}" data-status="${s}">${counts[s]} ${WORDS[s][0]}</span></div>`
  ).join('');
}
function cell(iso, day, data) {
  const holiday = data.holidays[iso] ? `<div class="holiday-name">${data.holidays[iso]}</div>` : '';
  return `<div class="date-cell ng-scope">
    <div class="d-flex block-am flex-1">
      <div class="calendar-date"><div class="date ng-binding">${day}</div><div class="mt-2"><span class="badge badge-default time-slab-badge">AM</span></div></div>
      <div class="w-100 calender-data"><div class="block _block-am w-100">${holiday}${tags(iso, 'AM', data)}</div></div>
    </div>
    <div class="d-flex align-items-center_ flex-1">
      <div class="calendar-date"><div class="mt-2"><span class="badge badge-default time-slab-badge">PM</span></div></div>
      <div class="w-100 calender-data"><div class="block _block-am w-100">${tags(iso, 'PM', data)}</div></div>
    </div>
  </div>`;
}
function render(data) {
  document.getElementById('month-title').textContent = `${MONTHS[data.month - 1]} ${data.year}`;
  for (const [key, value] of Object.entries(data.counts)) {
    const el = document.querySelector(`[data-count="${key}"]`);
    if (el) el.textContent = value;
  }
  if (data.blank) { body.innerHTML = ''; return; }
  const first = new Date(Date.UTC(data.year, data.month - 1, 1)).getUTCDay();
  const days = new Date(Date.UTC(data.year, data.month, 0)).getUTCDate();
  const pad = n => String(n).padStart(2, '0');
  let html = '', row = '', col = 0;
  for (let i = 0; i < first; i++) { row += '<td class="calendar-day ng-scope past-date"></td>'; col++; }
  for (let d = 1; d <= days; d++) {
    const iso = `${data.year}-${pad(data.month)}-${pad(d)}`;
    const extra = iso < data.today ? ' past-date' : (iso === data.today ? ' today' : '');
    row += `<td class="calendar-day ng-scope${extra}">${cell(iso, d, data)}</td>`;
    if (++col === 7) { html += `<tr class="ng-scope">${row}</tr>`; row = ''; col = 0; }
  }
  if (col > 0) { while (col++ < 7) row += '<td class="calendar-day ng-scope"></td>'; html += `<tr class="ng-scope">${row}</tr>`; }
  body.innerHTML = html;
}
function show(tab) {
  document.getElementById('home-view').style.display = tab === 'home' ? '' : 'none';
  document.getElementById('owo-view').style.display = tab === 'owo' ? '' : 'none';
  if (tab === 'owo') load();
}
document.querySelectorAll('[data-tab]').forEach(a => a.addEventListener('click', e => { e.preventDefault(); show(a.dataset.tab); }));
statusButtons().forEach(b => b.addEventListener('click', () => { if (!busy()) load(); }));
document.getElementById('next').addEventListener('click', () => {
  if (busy()) return;
  if (++view.month > 12) { view.month = 1; view.year++; }
  load();
});
document.getElementById('prev').addEventListener('click', () => {
  if (busy()) return;
  if (--view.month < 1) { view.month = 12; view.year--; }
  load();
});

body.addEventListener('click', e => {
  const tag = e.target.closest('span.badge[data-status="available"]');
  if (tag) openWorkOrders(tag.dataset.slot);
});
function modal(id, inner) {
  const m = document.createElement('div');
  m.className = 'modal show';
  m.setAttribute('role', 'dialog');
  m.id = id;
  m.innerHTML = `<div class="modal-dialog"><div class="modal-content">${inner}</div></div>`;
  document.body.appendChild(m);
  return m;
}
async function openWorkOrders(slot) {
  const res = await api(`/api/mock/details?slot=${encodeURIComponent(slot)}`);
  const data = await res.json();
  const cards = data.work_orders.map(w => `<div class="wo-card">
      <div>${w.number}</div><div>${w.court}</div><div>${w.language}</div><div>${w.judge}</div><div>${w.time}</div>
      ${data.button_label ? `<button class="btn btn-success accept-btn" data-id="${w.id}">${data.button_label}</button>` : ''}
    </div>`).join('');
  const m = modal('details-modal', `<div class="modal-header"><h4>Work Order Details</h4>
      <button type="button" class="close" aria-label="Close">&times;</button></div>
      <div class="modal-body">${cards || '<p>No work orders.</p>'}</div>`);
  m.querySelector('.close').addEventListener('click', () => m.remove());
  m.querySelectorAll('.accept-btn').forEach(b => b.addEventListener('click', () => accept(b.dataset.id, m, data.confirm)));
}
function askSure() {
  return new Promise(resolve => {
    const m = modal('confirm-modal', `<p>Are you sure you want to accept this work order?</p>
        <button class="btn btn-primary" id="yes">Yes</button> <button class="btn" id="no">No</button>`);
    m._resolve = resolve;
    m.querySelector('#yes').addEventListener('click', () => { m.remove(); resolve(true); });
    m.querySelector('#no').addEventListener('click', () => { m.remove(); resolve(false); });
  });
}
async function accept(id, details, needConfirm) {
  if (needConfirm && !(await askSure())) return;
  const res = await api(`/api/mock/accept/${id}`, {method: 'POST'});
  let data = {};
  try { data = await res.json(); } catch (e) {}
  details.remove();
  if (data.message) toast(data.message);
  load();
}
function toast(text) {
  const t = document.createElement('div');
  t.className = 'toast show';
  t.setAttribute('role', 'alert');
  t.textContent = text;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3000);
}
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  const open = document.querySelectorAll('.modal.show');
  if (!open.length) return;
  const top = open[open.length - 1];
  if (top._resolve) top._resolve(false);
  top.remove();
});
</script>
</body></html>
"""
