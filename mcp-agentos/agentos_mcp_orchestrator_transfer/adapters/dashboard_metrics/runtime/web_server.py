import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .storage import MetricStore


DEFAULT_DB_PATH = Path(__file__).resolve().parents[3] / 'storage' / 'dashboard_metrics.db'

HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>iWeaver Growth Intelligence</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f7fb;
      --panel: #ffffff;
      --panel-soft: #f8fafc;
      --text: #0f172a;
      --muted: #64748b;
      --line: #e2e8f0;
      --line-strong: #cbd5e1;
      --brand: #2563eb;
      --brand-dark: #1d4ed8;
      --brand-soft: #eff6ff;
      --good: #16a34a;
      --good-soft: #ecfdf5;
      --bad: #dc2626;
      --bad-soft: #fef2f2;
      --warn: #d97706;
      --warn-soft: #fffbeb;
      --shadow: 0 22px 60px rgba(15, 23, 42, .08);
      --radius: 20px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at 12% 0%, rgba(37, 99, 235, .12), transparent 28%),
        linear-gradient(180deg, #f8fbff 0%, var(--bg) 52%, #eef3fb 100%);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .shell { max-width: 1480px; margin: 0 auto; padding: 28px; }
    .topbar { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; margin-bottom: 20px; }
    .eyebrow { color: var(--brand); font-weight: 800; letter-spacing: .12em; text-transform: uppercase; font-size: 12px; }
    h1 { margin: 8px 0 8px; font-size: clamp(28px, 4vw, 44px); line-height: 1.04; letter-spacing: -.04em; }
    .subtitle { max-width: 820px; color: var(--muted); line-height: 1.7; font-size: 15px; }
    .actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }
    .status-badge { display: inline-flex; align-items: center; gap: 8px; border: 1px solid var(--line); background: rgba(255, 255, 255, .82); border-radius: 999px; padding: 9px 13px; color: var(--muted); font-size: 13px; box-shadow: 0 10px 30px rgba(15, 23, 42, .05); }
    .dot { width: 8px; height: 8px; border-radius: 999px; background: var(--good); box-shadow: 0 0 0 4px rgba(22, 163, 74, .12); }
    .toolbar {
      position: sticky; top: 12px; z-index: 3;
      display: grid; grid-template-columns: minmax(220px, 1.3fr) 160px 160px auto auto; gap: 12px;
      align-items: end; padding: 14px; margin-bottom: 18px;
      background: rgba(255,255,255,.86); backdrop-filter: blur(18px);
      border: 1px solid rgba(203, 213, 225, .8); border-radius: var(--radius); box-shadow: var(--shadow);
    }
    label { display: grid; gap: 6px; color: var(--muted); font-size: 12px; font-weight: 700; }
    select, input, button {
      height: 42px; border: 1px solid var(--line-strong); border-radius: 12px; background: white; color: var(--text); padding: 0 12px; font: inherit;
    }
    button { border: none; background: linear-gradient(135deg, var(--brand), var(--brand-dark)); color: white; font-weight: 800; cursor: pointer; box-shadow: 0 12px 24px rgba(37, 99, 235, .22); }
    button.secondary { color: var(--brand); background: var(--brand-soft); box-shadow: none; border: 1px solid #bfdbfe; }
    .cadence { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }
    .cadence-card { padding: 16px; border: 1px solid var(--line); background: rgba(255,255,255,.78); border-radius: 18px; }
    .cadence-card strong { display: block; font-size: 14px; margin-bottom: 6px; }
    .cadence-card span { color: var(--muted); font-size: 12px; line-height: 1.5; }
    .kpis { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 14px; margin-bottom: 18px; }
    .kpi { min-height: 150px; padding: 18px; background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: var(--shadow); position: relative; overflow: hidden; }
    .kpi::after { content: ""; position: absolute; inset: auto -24px -48px auto; width: 120px; height: 120px; border-radius: 999px; background: var(--brand-soft); }
    .kpi-title { color: var(--muted); font-size: 13px; font-weight: 800; }
    .kpi-value { margin-top: 14px; font-size: 30px; font-weight: 900; letter-spacing: -.03em; }
    .kpi-meta { display: flex; align-items: center; gap: 8px; margin-top: 10px; color: var(--muted); font-size: 12px; }
    .delta { font-weight: 900; }
    .delta.up { color: var(--good); }
    .delta.down { color: var(--bad); }
    .layout { display: grid; grid-template-columns: minmax(0, 2fr) 420px; gap: 18px; align-items: start; }
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden; }
    .panel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 18px 20px; border-bottom: 1px solid var(--line); }
    .panel-head h2 { margin: 0; font-size: 18px; letter-spacing: -.02em; }
    .panel-body { padding: 20px; }
    .chart-wrap { height: 360px; }
    svg.chart { width: 100%; height: 100%; display: block; overflow: visible; }
    .metric-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 18px; }
    .mini { padding: 16px; border: 1px solid var(--line); border-radius: 16px; background: var(--panel-soft); }
    .mini-top { display: flex; justify-content: space-between; gap: 10px; align-items: start; }
    .mini h3 { margin: 0; font-size: 14px; }
    .mini .value { font-size: 22px; font-weight: 900; margin-top: 10px; }
    .spark { width: 100%; height: 74px; margin-top: 10px; }
    .side { display: grid; gap: 18px; }
    .source-list { display: grid; gap: 10px; }
    .source-item { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px; border: 1px solid var(--line); border-radius: 14px; background: var(--panel-soft); }
    .source-name { display: grid; gap: 3px; }
    .source-name strong { font-size: 13px; }
    .source-name span { color: var(--muted); font-size: 12px; }
    .pill { display:inline-flex; align-items:center; justify-content:center; min-width: 54px; border-radius:999px; padding:5px 9px; font-size:12px; font-weight:900; background:var(--good-soft); color:var(--good); }
    .pill.warn { background: var(--warn-soft); color: var(--warn); }
    .pill.bad { background: var(--bad-soft); color: var(--bad); }
    table { width:100%; border-collapse:collapse; font-size:13px; }
    th, td { text-align:left; border-bottom:1px solid var(--line); padding:12px 14px; white-space: nowrap; }
    th { color: var(--muted); font-size: 12px; background: #f8fafc; }
    .table-scroll { overflow: auto; max-height: 480px; }
    .empty { padding: 28px; color: var(--muted); text-align: center; border: 1px dashed var(--line-strong); border-radius: 16px; background: var(--panel-soft); }
    @media (max-width: 1180px) { .kpis { grid-template-columns: repeat(3, minmax(0, 1fr)); } .layout { grid-template-columns: 1fr; } .cadence { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    @media (max-width: 760px) { .shell { padding: 18px; } .topbar { display: grid; } .toolbar { position: static; grid-template-columns: 1fr; } .kpis, .metric-grid, .cadence { grid-template-columns: 1fr; } th, td { padding: 10px; } }
  </style>
</head>
<body>
  <div class="shell">
    <header class="topbar">
      <div>
        <div class="eyebrow">Growth Intelligence Dashboard</div>
        <h1>iWeaver 核心增长指标</h1>
        <div class="subtitle">统一展示 GSC、GA4 与内部数据库指标，支持 MCP 定时取数、SQLite 持久化、趋势对比和 6 小时预警检查。</div>
      </div>
      <div class="actions">
        <span class="status-badge"><i class="dot"></i><span id="statusText">等待加载</span></span>
        <span class="status-badge">数据源：GSC · GA4 · Superset</span>
      </div>
    </header>

    <section class="toolbar">
      <label>趋势指标<select id="metricSelect"></select></label>
      <label>开始日期<input id="startDate" type="date" /></label>
      <label>结束日期<input id="endDate" type="date" /></label>
      <button id="refreshBtn">刷新数据</button>
      <button id="resetBtn" class="secondary">近 30 天</button>
    </section>

    <section class="cadence">
      <div class="cadence-card"><strong>GSC 搜索表现</strong><span>每日更新 T-3 / T-2：曝光、点击、CTR。</span></div>
      <div class="cadence-card"><strong>GA4 官网 UV</strong><span>每日更新 T-2：newUsers 作为官网新 UV。</span></div>
      <div class="cadence-card"><strong>内部增长指标</strong><span>每日回填 T-1 至 T-15：注册、激活、DAU、留存。</span></div>
      <div class="cadence-card"><strong>6 小时预警</strong><span>注册类 10%，付费类 20%，对比上个周期。</span></div>
    </section>

    <section id="kpiGrid" class="kpis"></section>

    <section class="layout">
      <div>
        <section class="panel">
          <div class="panel-head">
            <div><h2 id="mainChartTitle">核心趋势</h2><div class="muted" id="mainChartMeta"></div></div>
            <span class="pill" id="pointCount">0 点</span>
          </div>
          <div class="panel-body"><div id="mainChart" class="chart-wrap"></div></div>
        </section>
        <section id="metricGrid" class="metric-grid"></section>
      </div>
      <aside class="side">
        <section class="panel">
          <div class="panel-head"><h2>数据源状态</h2><span class="pill">Live</span></div>
          <div class="panel-body"><div id="sourceList" class="source-list"></div></div>
        </section>
        <section class="panel">
          <div class="panel-head"><h2>最近预警</h2><span class="pill warn" id="alertCount">0</span></div>
          <div class="table-scroll"><table><thead><tr><th>指标</th><th>变化</th><th>状态</th></tr></thead><tbody id="alertsBody"></tbody></table></div>
        </section>
      </aside>
    </section>

    <section class="panel" style="margin-top:18px">
      <div class="panel-head"><h2>最近指标明细</h2><span class="muted" id="tableMeta"></span></div>
      <div class="table-scroll"><table><thead><tr><th>日期</th><th>指标</th><th>数值</th><th>来源</th><th>频率</th><th>状态</th></tr></thead><tbody id="dataBody"></tbody></table></div>
    </section>
  </div>

<script>
const zhNames = {
  gsc_impressions:'GSC 曝光量', gsc_clicks:'GSC 点击量', gsc_ctr:'GSC CTR', ga4_new_uv:'官网新 UV',
  registration_users:'注册用户数', registration_rate:'注册率', first_day_activation_users:'首日激活用户数',
  dau:'日活跃用户', cohort_users:'注册 Cohort', d1_retention_users:'次留用户', d1_retention_rate:'次留率',
  d7_retention_users:'周留用户', d7_retention_rate:'周留率', paid_users:'付费用户', paid_orders:'付费订单',
  renewal_orders:'续费订单', payment_amount:'付费金额'
};
const sourceNames = { gsc:'Google Search Console', ga4:'Google Analytics 4', sql:'Superset SQL', computed:'Computed' };
const kpiMetrics = ['gsc_impressions','gsc_clicks','ga4_new_uv','registration_users','registration_rate','payment_amount'];
const defaultMetrics = ['gsc_impressions','gsc_clicks','ga4_new_uv','registration_users','first_day_activation_users','dau','d1_retention_rate','d7_retention_rate','paid_users','renewal_orders','payment_amount'];
const fmt = v => v == null || Number.isNaN(Number(v)) ? '-' : Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
const pct = v => v == null ? '-' : (Number(v) * 100).toFixed(1) + '%';
const nameOf = metric => zhNames[metric] || metric;
function setDefaultDates(){ const end = new Date(); const start = new Date(Date.now() - 30*864e5); endDate.value = end.toISOString().slice(0,10); startDate.value = start.toISOString().slice(0,10); }
async function api(path){ const r = await fetch(path); if(!r.ok) throw new Error(await r.text()); return r.json(); }
async function init(){ setDefaultDates(); const meta = await api('/api/meta'); const options = (meta.metric_names.length ? meta.metric_names : defaultMetrics).sort(); metricSelect.innerHTML = '<option value="">总览核心指标</option>' + options.map(m => `<option value="${m}">${nameOf(m)}</option>`).join(''); await refresh(); }
async function refresh(){ statusText.textContent = '加载中'; const params = new URLSearchParams({start:startDate.value, end:endDate.value}); if(metricSelect.value) params.set('metric', metricSelect.value); const [series, alerts, latest] = await Promise.all([api('/api/metrics?' + params), api('/api/alerts?limit=30'), api('/api/latest?limit=500')]); const rows = series.metrics.filter(r => r.status !== 'unavailable'); const latestRows = latest.metrics.filter(r => r.status !== 'unavailable'); renderKpis(latestRows.length ? latestRows : rows); renderMain(rows); renderMiniGrid(rows); renderSources(rows); renderAlerts(alerts.alerts); renderTable(latestRows.length ? latestRows : rows); statusText.textContent = `已更新 ${new Date().toLocaleTimeString()}`; }
function groupRows(rows){ return rows.reduce((acc,row)=>{ (acc[row.metric_name] ||= []).push(row); return acc; }, {}); }
function sortedMetricRows(rows){ return [...rows].sort((a,b)=>a.metric_date.localeCompare(b.metric_date)); }
function latestFor(metric, grouped){ const rows = sortedMetricRows(grouped[metric] || []); return rows[rows.length - 1]; }
function previousFor(metric, grouped){ const rows = sortedMetricRows(grouped[metric] || []); return rows[rows.length - 2]; }
function deltaHtml(last, prev){ if(!last || !prev || !prev.metric_value) return '<span class="muted">无环比</span>'; const d = (Number(last.metric_value) - Number(prev.metric_value)) / Math.abs(Number(prev.metric_value)); return `<span class="delta ${d >= 0 ? 'up':'down'}">${d >= 0 ? '▲':'▼'} ${pct(Math.abs(d))}</span>`; }
function renderKpis(rows){ const grouped = groupRows(rows); kpiGrid.innerHTML = kpiMetrics.map(metric => { const last = latestFor(metric, grouped); const prev = previousFor(metric, grouped); return `<article class="kpi"><div class="kpi-title">${nameOf(metric)}</div><div class="kpi-value">${fmt(last?.metric_value)}</div><div class="kpi-meta"><span>${last?.metric_date || '-'}</span>${deltaHtml(last, prev)}</div></article>`; }).join(''); }
function renderMain(rows){ const grouped = groupRows(rows); const selected = metricSelect.value || defaultMetrics.find(m => grouped[m]?.length) || Object.keys(grouped)[0]; const data = sortedMetricRows(grouped[selected] || []); mainChartTitle.textContent = nameOf(selected || '核心趋势'); mainChartMeta.textContent = data.length ? `${data[0].metric_date} 至 ${data[data.length-1].metric_date}` : '暂无数据'; pointCount.textContent = `${data.length} 点`; mainChart.innerHTML = data.length ? lineChart(data, 920, 330, true) : '<div class="empty">暂无趋势数据。请先运行 dashboard_metrics_daily_update。</div>'; }
function renderMiniGrid(rows){ const grouped = groupRows(rows); const selected = metricSelect.value ? [metricSelect.value] : defaultMetrics; const metrics = selected.filter(m => grouped[m]?.length).concat(Object.keys(grouped).filter(m => !selected.includes(m))).slice(0, 12); metricGrid.innerHTML = metrics.map(metric => { const data = sortedMetricRows(grouped[metric]); const last = data[data.length-1]; const prev = data[data.length-2]; return `<article class="mini"><div class="mini-top"><h3>${nameOf(metric)}</h3>${deltaHtml(last, prev)}</div><div class="value">${fmt(last.metric_value)}</div><div class="muted">${last.metric_date} · ${sourceNames[last.source] || last.source}</div>${lineChart(data, 300, 80, false, 'spark')}</article>`; }).join('') || '<div class="empty">暂无指标数据</div>'; }
function lineChart(rows, w, h, axes, cls='chart'){ const p = axes ? 38 : 8; const values = rows.map(r => Number(r.metric_value || 0)); const min = Math.min(...values), max = Math.max(...values); const span = max === min ? 1 : max - min; const x = i => p + (rows.length === 1 ? 0 : i*(w-p*2)/(rows.length-1)); const y = v => h-p - ((v-min)/span)*(h-p*2); const pts = values.map((v,i)=>`${x(i)},${y(v)}`).join(' '); const area = `${p},${h-p} ${pts} ${w-p},${h-p}`; const labels = axes ? rows.map((r,i)=> i%Math.ceil(rows.length/6)===0 ? `<text x="${x(i)}" y="${h-8}" text-anchor="middle" fill="#64748b" font-size="11">${r.metric_date.slice(5)}</text>` : '').join('') : ''; const grid = axes ? [0,.25,.5,.75,1].map(t => `<line x1="${p}" x2="${w-p}" y1="${p+t*(h-p*2)}" y2="${p+t*(h-p*2)}" stroke="#e2e8f0"/>`).join('') : ''; return `<svg class="${cls}" viewBox="0 0 ${w} ${h}" role="img">${grid}<polygon points="${area}" fill="rgba(37,99,235,.10)"/><polyline fill="none" stroke="#2563eb" stroke-width="${axes ? 4 : 3}" stroke-linecap="round" stroke-linejoin="round" points="${pts}"/>${values.map((v,i)=>`<circle cx="${x(i)}" cy="${y(v)}" r="${axes ? 4 : 2}" fill="#2563eb"/>`).join('')}${labels}</svg>`; }
function renderSources(rows){ const counts = rows.reduce((acc,r)=>{ acc[r.source] = (acc[r.source] || 0) + 1; return acc; }, {}); sourceList.innerHTML = ['gsc','ga4','sql','computed'].map(src => `<div class="source-item"><div class="source-name"><strong>${sourceNames[src]}</strong><span>${counts[src] || 0} 条可用指标</span></div><span class="pill ${counts[src] ? '' : 'warn'}">${counts[src] ? '正常':'暂无'}</span></div>`).join(''); }
function renderAlerts(rows){ alertCount.textContent = rows.filter(r => r.triggered).length; alertsBody.innerHTML = rows.map(a => `<tr><td>${nameOf(a.metric_name)}</td><td>${a.change_ratio == null ? '-' : pct(a.change_ratio)}</td><td><span class="pill ${a.triggered ? 'bad':''}">${a.triggered ? '预警':'正常'}</span></td></tr>`).join('') || '<tr><td colspan="3" class="muted">暂无预警记录</td></tr>'; }
function renderTable(rows){ const sorted = [...rows].sort((a,b)=>(b.metric_date + b.collected_at).localeCompare(a.metric_date + a.collected_at)).slice(0, 120); tableMeta.textContent = `${sorted.length} 条`; dataBody.innerHTML = sorted.map(r => `<tr><td>${r.metric_date}</td><td>${nameOf(r.metric_name)}</td><td><strong>${fmt(r.metric_value)}</strong></td><td>${sourceNames[r.source] || r.source}</td><td>${r.frequency}</td><td><span class="pill">${r.status}</span></td></tr>`).join('') || '<tr><td colspan="6" class="muted">暂无数据</td></tr>'; }
refreshBtn.onclick = refresh; resetBtn.onclick = () => { setDefaultDates(); refresh(); }; metricSelect.onchange = refresh; init().catch(e => { statusText.textContent = e.message; });
</script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    store = None

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ('/', '/index.html'):
            self.send_text(HTML, 'text/html; charset=utf-8')
            return
        if parsed.path == '/api/meta':
            self.send_json({'metric_names': self.store.metric_names()})
            return
        if parsed.path == '/api/metrics':
            qs = parse_qs(parsed.query)
            rows = self.store.metric_series(
                metric_name=one(qs, 'metric'),
                start_date=one(qs, 'start'),
                end_date=one(qs, 'end'),
                source=one(qs, 'source'),
            )
            self.send_json({'success': True, 'metrics': rows})
            return
        if parsed.path == '/api/latest':
            limit = int(one(parse_qs(parsed.query), 'limit') or 200)
            self.send_json({'success': True, 'metrics': self.store.latest_metrics(limit=limit)})
            return
        if parsed.path == '/api/alerts':
            limit = int(one(parse_qs(parsed.query), 'limit') or 100)
            self.send_json({'success': True, 'alerts': self.store.recent_alerts(limit=limit)})
            return
        self.send_error(404)

    def log_message(self, fmt, *args):
        return

    def send_json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, content_type):
        body = text.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def one(qs, key):
    values = qs.get(key) or ['']
    return values[0]


def serve(host='0.0.0.0', port=8765, db_path=DEFAULT_DB_PATH):
    DashboardHandler.store = MetricStore(db_path)
    server = ThreadingHTTPServer((host, int(port)), DashboardHandler)
    print(f'Dashboard listening on http://{host}:{port}')
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description='Serve iWeaver growth dashboard')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--db', default=str(DEFAULT_DB_PATH))
    args = parser.parse_args()
    serve(host=args.host, port=args.port, db_path=args.db)


if __name__ == '__main__':
    main()
