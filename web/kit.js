// Shared demo runner + small rendering helpers for the product suite.
// The page is a real API client: when it is served by the app (/ui/) every step calls the live API;
// on a static host it replays demo.json, the transcript recorded from the same calls by scripts/demo.py.
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmt = {
  n: (x, d = 0) => x == null ? '–' : Number(x).toLocaleString('en-US', { maximumFractionDigits: d, minimumFractionDigits: d }),
  pct: (x, d = 0) => x == null ? '–' : (100 * x).toFixed(d) + '%',
  usd: (x, d = 0) => x == null ? '–' : (x < 0 ? '-$' : '$') + fmt.n(Math.abs(x), d),
  date: s => s ? String(s).slice(0, 10) : '–',
  time: s => s ? String(s).slice(0, 16).replace('T', ' ') : '–',
};
const badge = (text, tone = '') => `<span class="badge ${tone}">${esc(text)}</span>`;
const card = (title, html) => `<div class="card">${title ? `<h3>${esc(title)}</h3>` : ''}${html}</div>`;
const kpis = items => `<div class="kpis">${items.map(k => `<div class="kpi"><div class="v">${k.value}</div><div class="l">${esc(k.label)}</div>${k.sub ? `<div class="s">${esc(k.sub)}</div>` : ''}</div>`).join('')}</div>`;
// cols: [{k, label, num, f(value,row) -> html}]
const table = (cols, rows) => `<table><thead><tr>${cols.map(c => `<th class="${c.num ? 'num' : ''}">${esc(c.label)}</th>`).join('')}</tr></thead><tbody>${
  rows.map(r => `<tr>${cols.map(c => `<td class="${c.num ? 'num' : ''}">${c.f ? c.f(r[c.k], r) : esc(r[c.k])}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
const PALETTE = ['#7aa2ff', '#3fb97a', '#e0a63c', '#ef6a6a', '#b78cf2', '#4fc3d9'];

// series: [{name, points:[[x,y]], color, dash, area}] ; x numeric (use Date.getTime() for time) ; opts: {w,h,xfmt,yfmt,ymin,ymax,marks:[{x,label}],bands:[{lo:[[x,y]],hi:[[x,y]],color}]}
function lineChart(series, o = {}) {
  const w = o.w || 680, h = o.h || 230, m = { l: 52, r: 14, t: 12, b: 26 };
  const all = series.flatMap(s => s.points).concat((o.bands || []).flatMap(b => b.lo.concat(b.hi)));
  if (!all.length) return '';
  const xs = all.map(p => p[0]), ys = all.map(p => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = o.ymin ?? Math.min(...ys), y1 = o.ymax ?? Math.max(...ys);
  const X = x => m.l + (w - m.l - m.r) * (x - x0) / (x1 - x0 || 1), Y = y => h - m.b - (h - m.t - m.b) * (y - y0) / (y1 - y0 || 1);
  const path = pts => pts.map((p, i) => (i ? 'L' : 'M') + X(p[0]).toFixed(1) + ' ' + Y(p[1]).toFixed(1)).join('');
  const yf = o.yfmt || (v => fmt.n(v, Math.abs(y1 - y0) < 10 ? 1 : 0)), xf = o.xfmt || (v => fmt.n(v));
  let g = '';
  for (let i = 0; i <= 4; i++) { const v = y0 + (y1 - y0) * i / 4; g += `<line x1="${m.l}" x2="${w - m.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="#25252b"/><text x="${m.l - 6}" y="${Y(v) + 4}" text-anchor="end">${esc(yf(v))}</text>`; }
  for (let i = 0; i <= 4; i++) { const v = x0 + (x1 - x0) * i / 4; g += `<text x="${X(v)}" y="${h - 8}" text-anchor="middle">${esc(xf(v))}</text>`; }
  for (const b of o.bands || []) g += `<path d="${path(b.hi)}${path(b.lo.slice().reverse()).replace('M', 'L')}Z" fill="${b.color || '#7aa2ff'}" opacity=".16"/>`;
  for (const mk of o.marks || []) g += `<line x1="${X(mk.x)}" x2="${X(mk.x)}" y1="${m.t}" y2="${h - m.b}" stroke="${mk.color || '#e0a63c'}" stroke-dasharray="4 3"/><text x="${X(mk.x) + 4}" y="${m.t + 10}">${esc(mk.label)}</text>`;
  series.forEach((s, i) => { const c = s.color || PALETTE[i % 6];
    if (s.area) g += `<path d="${path(s.points)}L${X(s.points.at(-1)[0])} ${Y(y0)}L${X(s.points[0][0])} ${Y(y0)}Z" fill="${c}" opacity=".12"/>`;
    g += s.dots ? s.points.map(p => `<circle cx="${X(p[0])}" cy="${Y(p[1])}" r="2.6" fill="${c}"/>`).join('')
                : `<path d="${path(s.points)}" fill="none" stroke="${c}" stroke-width="${s.width || 1.8}" ${s.dash ? 'stroke-dasharray="5 4"' : ''}/>`; });
  const legend = series.filter(s => s.name).map((s, i) => `<span><i style="background:${s.color || PALETTE[i % 6]}"></i>${esc(s.name)}</span>`).join('');
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" role="img">${g}</svg>${legend ? `<div class="legend">${legend}</div>` : ''}`;
}
// items: [{label, value, color, note}]
function barChart(items, o = {}) {
  const max = o.max || Math.max(...items.map(i => Math.abs(i.value)), 1e-9), f = o.fmt || (v => fmt.n(v));
  return `<table>${items.map(i => `<tr><td style="width:34%">${esc(i.label)}</td><td><div style="height:14px;width:${Math.max(1, 100 * Math.abs(i.value) / max)}%;background:${i.color || PALETTE[0]};border-radius:3px"></div></td><td class="num" style="width:22%">${esc(f(i.value))}${i.note ? ` <span class="mute small">${esc(i.note)}</span>` : ''}</td></tr>`).join('')}</table>`;
}

class Demo {
  constructor(renderers) { this.renderers = renderers; this.r = {}; this.done = -1; }
  async init() {
    this.scenario = await (await fetch('scenario.json')).json();
    try { const h = await fetch('/healthz'); this.live = h.ok && (await h.json()).service === this.scenario.service; } catch { this.live = false; }
    if (!this.live) this.recorded = await (await fetch('demo.json')).json();
    document.querySelector('.mode').textContent = this.live ? 'LIVE API' : 'RECORDED RUN';
    document.querySelector('.mode').classList.toggle('live', this.live);
    document.querySelector('.mode').title = this.live ? 'Each step calls this server' : 'Replaying responses recorded from the real API by scripts/demo.py';
    const nav = document.querySelector('nav.steps'), main = document.querySelector('#steps');
    this.scenario.steps.forEach((s, i) => {
      nav.insertAdjacentHTML('beforeend', `<a href="#s${i}" id="n${i}"><span class="n">${i + 1}</span><span class="t">${esc(s.title)}</span></a>`);
      main.insertAdjacentHTML('beforeend', `<section class="step" id="s${i}"><div class="head"><div class="grow"><h2>${i + 1}. ${esc(s.title)}</h2><p>${esc(s.say)}</p></div>
        <button data-i="${i}" ${i ? 'disabled' : ''}>Run step</button></div><div class="out"></div><details class="calls" hidden><summary></summary></details></section>`);
    });
    main.addEventListener('click', e => { if (e.target.dataset.i) this.run(+e.target.dataset.i); });
    document.querySelector('#runall').onclick = async () => { for (let i = this.done + 1; i < this.scenario.steps.length; i++) await this.run(i); };
    if (location.hash === '#all') document.querySelector('#runall').click();
  }
  fill(v) {   // "{{case.id}}" -> value from an earlier response
    if (typeof v === 'string') return v.replace(/\{\{([\w.]+)\}\}/g, (_, p) => p === 'now' ? new Date().toISOString() : p.startsWith('days_ago_') ? new Date(Date.now() - 864e5 * +p.slice(9)).toISOString() : p === 'loss_date' ? new Date(Date.now() - 2 * 864e5).toISOString().slice(0, 10) : p.split('.').reduce((o, k) => o?.[k], this.r));
    if (Array.isArray(v)) return v.map(x => this.fill(x));
    if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, this.fill(x)]));
    return v;
  }
  async call(c) {
    if (!this.live) return this.recorded[c.key];
    const headers = { Authorization: 'Bearer ' + this.scenario.tokens[c.as] };
    if (c.method !== 'GET') { headers['Content-Type'] = 'application/json'; headers['Idempotency-Key'] = this.scenario.run + ':' + c.key; }
    let res = await fetch(this.fill(c.path), { method: c.method, headers, body: c.method === 'GET' ? undefined : JSON.stringify(this.fill(c.body ?? {})) });
    let body = await res.json();
    while (res.status === 202 || body.status === 'queued' || body.status === 'running') {      // long-running work: poll the job
      await new Promise(r => setTimeout(r, 400));
      res = await fetch(body.status_url || '/v1/jobs/' + body.job_id, { headers }); body = await res.json();
    }
    return { status: res.status, body };
  }
  async run(i) {
    if (i !== this.done + 1) return;
    const step = this.scenario.steps[i], sec = document.querySelector('#s' + i), out = sec.querySelector('.out'), log = sec.querySelector('details');
    sec.querySelector('button').disabled = true; document.querySelector('#n' + i).className = 'busy';
    let calls = '';
    for (const c of step.calls) {
      const res = await this.call(c); this.r[c.key] = res.body;
      calls += `<div class="call">${badge(res.status, res.status < 300 ? 'good' : 'bad')} ${esc(c.method)} ${esc(this.fill(c.path))} <span class="mute">as ${esc(c.as)}</span><details><summary>response</summary><pre>${esc(JSON.stringify(res.body, null, 1).slice(0, 6000))}</pre></details></div>`;
      if (res.status >= 400 && !c.expect_error) { out.innerHTML += card('Error', `<pre class="doc">${esc(JSON.stringify(res.body, null, 1))}</pre>`); break; }
      if (this.renderers[c.key]) out.insertAdjacentHTML('beforeend', this.renderers[c.key](res.body, this.r));
    }
    if (this.renderers['_step' + i]) out.insertAdjacentHTML(this.renderers['_step' + i].first ? 'afterbegin' : 'beforeend', this.renderers['_step' + i](this.r));
    log.hidden = false; log.querySelector('summary').textContent = `${step.calls.length} API call${step.calls.length > 1 ? 's' : ''} in this step`; log.insertAdjacentHTML('beforeend', calls);
    this.done = i; document.querySelector('#n' + i).className = 'done';
    const next = document.querySelector(`#s${i + 1} button`); if (next) next.disabled = false;
    if (this.renderers._after) this.renderers._after(i, this.r);
  }
}
