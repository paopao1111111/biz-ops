const PRODUCT_LABELS = {
  All: '总计',
  iWeaver: 'iWeaver / 其他',
  Palmly: 'Palmly',
  LearningCoach: '学习教练',
};

const PRODUCT_COLORS = {
  All: '#172B4D',
  iWeaver: '#0055FF',
  Palmly: '#8B5CF6',
  LearningCoach: '#0F9D76',
};

const STATUS_LABELS = {
  available: '可用',
  source_unavailable: '源数据未接入',
  pre_launch: '尚未上线',
  immature: '待成熟',
  partial_maturity: '部分成熟',
  insufficient_sample: '小样本',
  linkage_incomplete: '关联不完整',
  left_censored: '历史左截断',
  not_applicable: '不适用',
  partial: '部分可用',
};

const QUALITY_LABELS = {
  domain_not_populated: 'domain 未填充',
  domain_coverage_incomplete: 'domain 覆盖不完整',
  palm_message_linkage_incomplete: '消息关联不完整',
  palm_chat_linkage_incomplete: '聊天关联不完整',
  tiny_sample: '样本少于 10',
  small_sample: '样本少于 30',
  no_sample: '暂无样本',
  no_linkable_reports: '暂无可关联报告',
  maturity_incomplete: '观察窗口未成熟',
  incomplete_week: '当前周未结束',
  chat_source_left_censored: '聊天源历史不完整',
  not_collected: '尚未采集',
  launch_week: '上线首周',
};

const VALID_VIEWS = new Set(['overview', 'iWeaver', 'Palmly', 'LearningCoach', 'quality', 'definitions']);
const FALLBACK_GRAIN_OPTIONS = {
  day: { label: '日', ranges: [7, 14, 30, 90], default_range: 30 },
  week: { label: '周', ranges: [4, 8, 12, 26, 52], default_range: 12 },
  month: { label: '月', ranges: [3, 6, 12], default_range: 6 },
};
const cache = new Map();
let catalog = null;
let weeks = [];
let overviewData = null;
let viewController = null;
let explorerController = null;

const state = {
  view: 'overview',
  week: null,
  metric: 'active_users',
  products: ['All'],
  grain: 'week',
  range: 12,
  chart: 'line',
  partial: true,
};

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  })[character]);
}

function apiUrl(path, params = {}) {
  const url = new URL(path, window.location.origin);
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') url.searchParams.set(key, value);
  });
  return url;
}

async function api(path, params = {}, options = {}) {
  const url = apiUrl(path, params);
  const cacheKey = options.cache ? url.toString() : null;
  if (cacheKey && cache.has(cacheKey)) return cache.get(cacheKey);
  const response = await fetch(url, {
    credentials: 'same-origin',
    signal: options.signal,
  });
  if (response.status === 401) {
    window.location.href = '/login';
    return null;
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  if (cacheKey) cache.set(cacheKey, data);
  return data;
}

function metricDefinition(key = state.metric) {
  return catalog?.metrics.find((item) => item.key === key);
}

function grainOptions() {
  return catalog?.grain_options || FALLBACK_GRAIN_OPTIONS;
}

function grainConfig(grain = state.grain) {
  return grainOptions()[grain] || FALLBACK_GRAIN_OPTIONS.week;
}

function grainLabel(grain = state.grain) {
  return grainConfig(grain).label;
}

function pointPeriod(point) {
  return point?.period_start || point?.week_start || '';
}

function parseUrlState() {
  const params = new URLSearchParams(window.location.search);
  const view = params.get('view');
  if (view && VALID_VIEWS.has(view)) state.view = view;
  state.week = params.get('week') || state.week;
  state.metric = params.get('metric') || state.metric;
  const products = (params.get('products') || '').split(',').filter(Boolean);
  if (products.length) state.products = [...new Set(products)].slice(0, 4);
  const grain = params.get('grain');
  if (grain && FALLBACK_GRAIN_OPTIONS[grain]) state.grain = grain;
  const range = Number(params.get('range'));
  if (grainConfig().ranges.includes(range)) state.range = range;
  const chart = params.get('chart');
  if (chart === 'line' || chart === 'bar') state.chart = chart;
  const partial = params.get('partial');
  if (partial !== null) state.partial = partial !== '0';
}

function normalizeState() {
  if (!VALID_VIEWS.has(state.view)) state.view = 'overview';
  if (!weeks.includes(state.week)) state.week = weeks[0] || null;
  if (!grainOptions()[state.grain]) state.grain = 'week';
  const config = grainConfig();
  if (!config.ranges.includes(state.range)) state.range = config.default_range;
  let definition = metricDefinition();
  if (!definition || !(definition.grains || ['week']).includes(state.grain)) {
    state.metric = 'active_users';
    definition = metricDefinition();
  }
  state.products = state.products.filter((product) => definition.products.includes(product)).slice(0, 4);
  if (!state.products.length) state.products = [definition.products[0]];
  if (!definition.charts.includes(state.chart)) state.chart = definition.default_chart;
  if (!definition.partial_allowed) state.partial = false;
}

function syncUrl() {
  const params = new URLSearchParams();
  params.set('view', state.view);
  if (state.week) params.set('week', state.week);
  params.set('metric', state.metric);
  params.set('products', state.products.join(','));
  params.set('grain', state.grain);
  params.set('range', String(state.range));
  params.set('chart', state.chart);
  params.set('partial', state.partial ? '1' : '0');
  window.history.replaceState(null, '', `${window.location.pathname}?${params.toString()}`);
}

function setLoading(container, message = '载入数据') {
  container.innerHTML = `<div class="loading">${escapeHtml(message)}</div>`;
}

function showInlineError(container, error, retry) {
  container.innerHTML = `
    <div class="inline-error">
      <strong>数据暂时不可用</strong>
      <span>${escapeHtml(error.message || error)}</span>
      <button type="button" class="secondary-button">重试</button>
    </div>`;
  container.querySelector('button').addEventListener('click', retry);
}

function formatValue(value, unit = 'count', compact = false) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  const number = Number(value);
  if (unit === 'percent') return `${number.toFixed(1)}%`;
  if (unit === 'ratio' || unit === 'median') return number.toFixed(number % 1 === 0 ? 1 : 2);
  if (compact && Math.abs(number) >= 10000) return `${(number / 10000).toFixed(1)}万`;
  return number.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
}

function formatAxisValue(value, unit) {
  if (unit === 'percent') return `${Number(value).toFixed(0)}%`;
  if (unit === 'count') {
    const rounded = Math.round(Number(value));
    if (Math.abs(rounded) >= 10000) return `${(rounded / 10000).toFixed(1)}万`;
    return rounded.toLocaleString('zh-CN');
  }
  return Number(value).toFixed(Number(value) % 1 === 0 ? 0 : 1);
}

function formatDateTime(value) {
  if (!value) return '未知';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value).replace('T', ' ').slice(0, 16);
  return parsed.toLocaleString('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

function pointFor(data, product, key, comparison = false) {
  const source = comparison ? data?.comparison_metrics : data?.metrics;
  return source?.[product]?.[key] || null;
}

function pointStatus(point) {
  return point?.status_label || STATUS_LABELS[point?.status] || point?.status || '未采集';
}

function pointMainValue(point, unit) {
  if (!point) return '—';
  if (point.value !== null && point.value !== undefined) return formatValue(point.value, unit, true);
  if (point.numerator !== null && point.numerator !== undefined && point.denominator !== null && point.denominator !== undefined && point.denominator > 0) {
    return `${formatValue(point.numerator)} / ${formatValue(point.denominator)}`;
  }
  return '—';
}

function qualityText(point) {
  if (!point) return '尚未采集';
  const parts = [pointStatus(point)];
  if (point.quality_code && QUALITY_LABELS[point.quality_code]) parts.push(QUALITY_LABELS[point.quality_code]);
  if (point.numerator !== null && point.numerator !== undefined && point.denominator !== null && point.denominator !== undefined) {
    parts.push(`${formatValue(point.numerator)} / ${formatValue(point.denominator)}`);
  }
  return [...new Set(parts)].join(' · ');
}

function deltaValue(current, previous, unit) {
  if (!current || !previous || current.value === null || previous.value === null) return null;
  const delta = Number(current.value) - Number(previous.value);
  if (Math.abs(delta) < 1e-9) return { text: '持平', className: 'flat' };
  const prefix = delta > 0 ? '+' : '';
  const text = unit === 'percent'
    ? `${prefix}${delta.toFixed(1)} 个百分点`
    : `${prefix}${formatValue(delta, unit, true)}`;
  return { text, className: delta > 0 ? 'up' : 'down' };
}

function metricCard(label, point, previous, unit, subtitle = '') {
  const delta = deltaValue(point, previous, unit);
  const warning = point && point.status !== 'available';
  return `
    <article class="kpi-card ${warning ? 'has-warning' : ''}">
      <div class="kpi-label-row">
        <span class="kpi-label">${escapeHtml(label)}</span>
        ${warning ? `<span class="status-dot ${escapeHtml(point.status)}" title="${escapeHtml(qualityText(point))}"></span>` : ''}
      </div>
      <div class="kpi-value ${pointMainValue(point, unit) === '—' ? 'muted' : ''}">${escapeHtml(pointMainValue(point, unit))}</div>
      <div class="kpi-sub">
        <span title="${escapeHtml(qualityText(point))}">${escapeHtml(subtitle || qualityText(point))}</span>
        ${delta ? `<span class="kpi-change ${delta.className}">${escapeHtml(delta.text)}</span>` : ''}
      </div>
    </article>`;
}

function renderWindowMeta(data) {
  const badge = document.getElementById('windowBadge');
  if (data.window_kind === 'partial') {
    badge.textContent = '本周进行中';
    badge.className = 'badge badge-partial';
  } else {
    badge.textContent = '完整周';
    badge.className = 'badge badge-full';
  }
  const failed = data.collector?.status === 'failed' ? ' · 最近采集失败，展示上次成功快照' : '';
  document.getElementById('updateTime').textContent = `更新于 ${formatDateTime(data.collected_at)}${failed}`;
}

function renderKpis(data) {
  const product = ['iWeaver', 'Palmly', 'LearningCoach'].includes(state.view) ? state.view : null;
  const cards = [];
  if (!product) {
    const median = pointFor(data, 'All', 'median_user_turns');
    cards.push(
      metricCard('全站周活跃', pointFor(data, 'All', 'active_users'), pointFor(data, 'All', 'active_users', true), 'count', '去重用户；产品活跃不可相加'),
      metricCard('全站真实注册', pointFor(data, 'All', 'registration_total'), pointFor(data, 'All', 'registration_total', true), 'count', 'DB2 全部新增账号'),
      metricCard('回访用户占比', pointFor(data, 'All', 'returning_share'), pointFor(data, 'All', 'returning_share', true), 'percent', '回访活跃 / 周活跃'),
      metricCard('用户轮次', pointFor(data, 'All', 'user_turns'), pointFor(data, 'All', 'user_turns', true), 'count', `每活跃用户中位数 ${pointMainValue(median, 'median')}`),
    );
  } else if (product === 'iWeaver') {
    cards.push(
      metricCard('domain 归因注册', pointFor(data, product, 'registration_domain_attributed'), pointFor(data, product, 'registration_domain_attributed', true), 'count', '观察值，不外推缺失 domain'),
      metricCard('24h 激活率', pointFor(data, product, 'activation_24h'), pointFor(data, product, 'activation_24h', true), 'percent', '成熟 cohort 分子 / 分母'),
      metricCard('周活跃用户', pointFor(data, product, 'active_users'), pointFor(data, product, 'active_users', true), 'count', 'residual 产品分类'),
      metricCard('回访用户占比', pointFor(data, product, 'returning_share'), pointFor(data, product, 'returning_share', true), 'percent', '回访活跃 / 周活跃'),
    );
  } else if (product === 'Palmly') {
    cards.push(
      metricCard('首次报告用户', pointFor(data, product, 'first_use_users'), pointFor(data, product, 'first_use_users', true), 'count', '产品首次使用代理，不是注册'),
      metricCard('Lunara 报告', pointFor(data, product, 'reports'), pointFor(data, product, 'reports', true), 'count', 'lunara_reports 精确计数'),
      metricCard('报告用户', pointFor(data, product, 'active_users'), pointFor(data, product, 'active_users', true), 'count', '周内至少生成一份报告'),
      metricCard('报告后 7 日回访', pointFor(data, product, 'palm_followup_7d'), pointFor(data, product, 'palm_followup_7d', true), 'percent', '只使用成熟首次报告 cohort'),
    );
  } else {
    cards.push(
      metricCard('首次使用用户', pointFor(data, product, 'first_use_users'), pointFor(data, product, 'first_use_users', true), 'count', '学习教练家族归因'),
      metricCard('近 4 周深度达成', pointFor(data, product, 'learning_activation_4w'), pointFor(data, product, 'learning_activation_4w', true), 'percent', '连续 4 周分子 / 分母'),
      metricCard('周活跃用户', pointFor(data, product, 'active_users'), pointFor(data, product, 'active_users', true), 'count', '学习教练家族去重用户'),
      metricCard('回访用户占比', pointFor(data, product, 'returning_share'), pointFor(data, product, 'returning_share', true), 'percent', '小样本只显示分子 / 分母'),
    );
  }
  document.getElementById('kpiGrid').innerHTML = cards.join('');
}

function qualityChip(label, value, status, detail = '') {
  const tone = status === 'available' ? 'good' : status === 'source_unavailable' || status === 'pre_launch' ? 'muted' : 'warn';
  return `<div class="quality-chip ${tone}" title="${escapeHtml(detail)}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

function renderQualityStrip(data) {
  const domain = pointFor(data, 'All', 'domain_coverage');
  const palm = pointFor(data, 'Palmly', 'palm_linkage_coverage');
  const collectorStatus = data.collector?.status || 'unknown';
  document.getElementById('qualityStrip').innerHTML = [
    qualityChip('domain 覆盖', pointMainValue(domain, 'percent'), domain?.status, qualityText(domain)),
    qualityChip('Palmly 关联', pointMainValue(palm, 'percent'), palm?.status, qualityText(palm)),
    qualityChip('源数据最新', formatDateTime(data.source_freshness), data.source_freshness ? 'available' : 'source_unavailable', '所有源中的最新记录时间'),
    qualityChip('采集状态', collectorStatus === 'success' ? '成功' : '需关注', collectorStatus === 'success' ? 'available' : 'partial', data.collector?.error_summary || ''),
  ].join('');
}

function metricCell(point, unit) {
  if (!point) return '<span class="metric-cell unavailable">—<small>未采集</small></span>';
  const warning = point.status !== 'available';
  return `<span class="metric-cell ${warning ? 'warning' : ''}" title="${escapeHtml(qualityText(point))}">
    <strong>${escapeHtml(pointMainValue(point, unit))}</strong>
    <small>${escapeHtml(pointStatus(point))}</small>
  </span>`;
}

function renderProductTable(data) {
  const rows = ['iWeaver', 'Palmly', 'LearningCoach'].map((product) => {
    const firstKey = product === 'iWeaver' ? 'registration_domain_attributed' : 'first_use_users';
    const useKey = product === 'Palmly' ? 'reports' : 'user_turns';
    const outcomeKey = product === 'iWeaver' ? 'activation_24h' : product === 'Palmly' ? 'palm_followup_7d' : 'learning_activation_4w';
    return `<tr>
      <th scope="row" class="product-name"><span class="legend-swatch" style="background:${PRODUCT_COLORS[product]}"></span>${escapeHtml(PRODUCT_LABELS[product])}</th>
      <td>${metricCell(pointFor(data, product, firstKey), 'count')}</td>
      <td>${metricCell(pointFor(data, product, 'active_users'), 'count')}</td>
      <td>${metricCell(pointFor(data, product, 'returning_share'), 'percent')}</td>
      <td>${metricCell(pointFor(data, product, useKey), 'count')}</td>
      <td>${metricCell(pointFor(data, product, outcomeKey), 'percent')}</td>
    </tr>`;
  });
  document.getElementById('productTableBody').innerHTML = rows.join('');
}

function updatePageHeading() {
  const titleMap = {
    overview: '总览',
    iWeaver: 'iWeaver / 其他',
    Palmly: 'Palmly',
    LearningCoach: '学习教练',
    quality: '数据质量',
    definitions: '口径说明',
  };
  const subtitleMap = {
    overview: '三产品周度经营与数据质量',
    iWeaver: 'residual 使用与 domain 归因观察值',
    Palmly: 'Lunara 报告精确口径',
    LearningCoach: '学习教练家族归因口径',
    quality: '覆盖率、源范围与关联完整性',
    definitions: '指标分类、成熟窗口与解释边界',
  };
  document.getElementById('pageTitle').textContent = titleMap[state.view];
  document.getElementById('pageSubtitle').textContent = subtitleMap[state.view];
  document.querySelectorAll('.nav-item').forEach((item) => item.classList.toggle('active', item.dataset.view === state.view));
}

function setViewVisibility() {
  document.getElementById('view-dashboard').hidden = state.view === 'quality' || state.view === 'definitions';
  document.getElementById('view-quality').hidden = state.view !== 'quality';
  document.getElementById('view-definitions').hidden = state.view !== 'definitions';
  document.getElementById('windowBadge').hidden = state.view === 'definitions';
  document.getElementById('weekSelect').disabled = state.view === 'definitions';
}

function applyProductPreset(product) {
  if (product === 'Palmly') {
    state.metric = 'reports';
    state.products = ['Palmly'];
  } else if (product === 'LearningCoach') {
    state.metric = 'learning_activation_4w';
    state.products = ['LearningCoach'];
  } else if (product === 'iWeaver') {
    state.metric = 'active_users';
    state.products = ['iWeaver'];
  } else {
    state.metric = 'active_users';
    state.products = ['All'];
  }
  const definition = metricDefinition();
  state.chart = definition.default_chart;
  state.partial = definition.partial_allowed;
}

async function navigate(view, applyPreset = true) {
  state.view = view;
  if (applyPreset && ['overview', 'iWeaver', 'Palmly', 'LearningCoach'].includes(view)) applyProductPreset(view);
  normalizeState();
  syncUrl();
  updatePageHeading();
  setViewVisibility();
  closeMobileMenu();
  await loadCurrentView();
}

async function loadDashboard() {
  if (viewController) viewController.abort();
  viewController = new AbortController();
  setLoading(document.getElementById('kpiGrid'), '载入周度指标');
  try {
    overviewData = await api('/api/overview', { week: state.week }, { signal: viewController.signal });
    if (!overviewData?.available) throw new Error('所选周没有成功采集的数据');
    renderWindowMeta(overviewData);
    renderKpis(overviewData);
    renderQualityStrip(overviewData);
    const productMode = ['iWeaver', 'Palmly', 'LearningCoach'].includes(state.view);
    document.getElementById('productComparisonSection').hidden = productMode;
    if (!productMode) renderProductTable(overviewData);
    syncExplorerControls();
    await loadExplorer();
  } catch (error) {
    if (error.name === 'AbortError') return;
    showInlineError(document.getElementById('kpiGrid'), error, loadDashboard);
  }
}

function renderMetricOptions() {
  const select = document.getElementById('metricSelect');
  select.innerHTML = catalog.metrics
    .filter((metric) => (metric.grains || ['week']).includes(state.grain))
    .map((metric) => `<option value="${escapeHtml(metric.key)}">${escapeHtml(metric.label)}</option>`)
    .join('');
}

function renderRangeOptions() {
  const config = grainConfig();
  const select = document.getElementById('rangeSelect');
  const unit = state.grain === 'day' ? '天' : config.label;
  select.innerHTML = config.ranges
    .map((value) => `<option value="${value}">${value} ${escapeHtml(unit)}</option>`)
    .join('');
  select.value = String(state.range);
}

function renderProductOptions() {
  const definition = metricDefinition();
  const container = document.getElementById('productOptions');
  container.innerHTML = definition.products.map((product) => {
    const checked = state.products.includes(product);
    return `<label class="product-option ${checked ? 'selected' : ''}">
      <input type="checkbox" value="${escapeHtml(product)}" ${checked ? 'checked' : ''}>
      <span class="legend-swatch" style="background:${PRODUCT_COLORS[product]}"></span>
      <span>${escapeHtml(PRODUCT_LABELS[product])}</span>
    </label>`;
  }).join('');
  container.querySelectorAll('input').forEach((input) => {
    input.addEventListener('change', () => {
      const selected = [...container.querySelectorAll('input:checked')].map((node) => node.value);
      if (!selected.length) {
        input.checked = true;
        return;
      }
      if (selected.length > 4) {
        input.checked = false;
        return;
      }
      state.products = [...container.querySelectorAll('input:checked')].map((node) => node.value);
      syncUrl();
      renderProductOptions();
      loadExplorer();
    });
  });
}

function syncExplorerControls() {
  normalizeState();
  const definition = metricDefinition();
  renderMetricOptions();
  document.getElementById('metricSelect').value = state.metric;
  document.getElementById('grainSelect').value = state.grain;
  renderRangeOptions();
  const partial = document.getElementById('partialToggle');
  partial.checked = state.partial;
  partial.disabled = !definition.partial_allowed;
  document.getElementById('partialLabel').textContent = `包含当前${grainLabel()}`;
  document.getElementById('metricDescription').textContent = definition.description;
  const classification = document.getElementById('classificationBadge');
  classification.textContent = definition.classification;
  classification.className = `classification-badge ${definition.classification}`;
  document.querySelectorAll('#chartButtons button').forEach((button) => {
    const allowed = definition.charts.includes(button.dataset.chart);
    button.disabled = !allowed;
    button.classList.toggle('active', state.chart === button.dataset.chart);
  });
  renderProductOptions();
}

function chartTooltip(point, product, definition) {
  const parts = [
    `${PRODUCT_LABELS[product]} · ${pointPeriod(point)}`,
    `值：${formatValue(point.value, definition.unit)}`,
    `状态：${pointStatus(point)}`,
  ];
  if (point.numerator !== null && point.numerator !== undefined && point.denominator !== null && point.denominator !== undefined) {
    parts.push(`分子/分母：${formatValue(point.numerator)} / ${formatValue(point.denominator)}`);
  }
  if (point.quality_code) parts.push(QUALITY_LABELS[point.quality_code] || point.quality_code);
  if (point.window_kind === 'partial') parts.push(`当前${grainLabel()}尚未结束`);
  return parts.join('；');
}

function renderLegend(series) {
  document.getElementById('chartLegend').innerHTML = series.map((item) => `
    <span class="legend-item"><span class="legend-swatch" style="background:${PRODUCT_COLORS[item.product]}"></span>${escapeHtml(item.label)}</span>
  `).join('') + '<span class="legend-note"><span class="warning-point"></span>空心点表示需结合状态解释</span>';
}

function yScaleConfig(values, unit) {
  const finite = values.filter((value) => value !== null && Number.isFinite(Number(value))).map(Number);
  if (unit === 'percent') return { min: 0, max: 100 };
  const maximum = Math.max(...finite, 0);
  return { min: 0, max: maximum > 0 ? maximum * 1.12 : 1 };
}

function renderSvgChart(data) {
  const container = document.getElementById('trendChart');
  const definition = data.metric;
  const series = data.series;
  renderLegend(series);
  if (!series.length || !series[0].points.length) {
    container.innerHTML = '<div class="chart-empty">暂无趋势数据</div>';
    return;
  }
  const width = 940;
  const height = 350;
  const margin = { top: 24, right: 24, bottom: 52, left: 68 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const pointCount = series[0].points.length;
  const allValues = series.flatMap((item) => item.points.map((point) => point.value));
  const scale = yScaleConfig(allValues, definition.unit);
  const xAt = (index) => margin.left + (pointCount === 1 ? plotWidth / 2 : index / (pointCount - 1) * plotWidth);
  const yAt = (value) => margin.top + plotHeight - ((Number(value) - scale.min) / (scale.max - scale.min)) * plotHeight;
  const ticks = Array.from({ length: 5 }, (_, index) => scale.min + (scale.max - scale.min) * index / 4);
  const xLabelStep = pointCount > 60 ? 10 : pointCount > 30 ? 5 : pointCount > 16 ? 2 : 1;

  const grid = ticks.map((tick) => {
    const y = yAt(tick);
    return `<line x1="${margin.left}" y1="${y}" x2="${width - margin.right}" y2="${y}" class="grid-line"/>
      <text x="${margin.left - 10}" y="${y + 4}" text-anchor="end" class="axis-label">${escapeHtml(formatAxisValue(tick, definition.unit))}</text>`;
  }).join('');

  const labels = series[0].points.map((point, index) => {
    if (index % xLabelStep !== 0 && index !== pointCount - 1) return '';
    const period = pointPeriod(point);
    const label = state.grain === 'month' ? period.slice(0, 7) : period.slice(5);
    return `<text x="${xAt(index)}" y="${height - 18}" text-anchor="middle" class="axis-label ${point.window_kind === 'partial' ? 'partial-label' : ''}">${escapeHtml(label)}${point.window_kind === 'partial' ? '*' : ''}</text>`;
  }).join('');

  let marks = '';
  if (data.chart === 'line') {
    series.forEach((item) => {
      const color = PRODUCT_COLORS[item.product];
      for (let index = 1; index < item.points.length; index += 1) {
        const previous = item.points[index - 1];
        const current = item.points[index];
        if (previous.value === null || current.value === null) continue;
        const partial = previous.window_kind === 'partial' || current.window_kind === 'partial';
        marks += `<line x1="${xAt(index - 1)}" y1="${yAt(previous.value)}" x2="${xAt(index)}" y2="${yAt(current.value)}" stroke="${color}" class="series-line ${partial ? 'partial-mark' : ''}"/>`;
      }
      item.points.forEach((point, index) => {
        if (point.value === null) return;
        const warning = point.status !== 'available';
        const title = chartTooltip(point, item.product, definition);
        marks += `<g tabindex="0" role="img" aria-label="${escapeHtml(title)}" class="chart-focus">
          <circle cx="${xAt(index)}" cy="${yAt(point.value)}" r="${warning ? 5 : 4}" stroke="${color}" fill="${warning ? '#fff' : color}" stroke-width="${warning ? 2.5 : 1.5}" class="chart-point ${point.window_kind === 'partial' ? 'partial-mark' : ''}"><title>${escapeHtml(title)}</title></circle>
        </g>`;
      });
    });
  } else {
    const groupWidth = Math.min(62, plotWidth / Math.max(pointCount, 1) * 0.72);
    const barWidth = Math.max(3, groupWidth / Math.max(series.length, 1) - 3);
    series.forEach((item, seriesIndex) => {
      const color = PRODUCT_COLORS[item.product];
      item.points.forEach((point, index) => {
        if (point.value === null) return;
        const x = xAt(index) - groupWidth / 2 + seriesIndex * (barWidth + 3);
        const y = yAt(point.value);
        const barHeight = Math.max(1.5, margin.top + plotHeight - y);
        const warning = point.status !== 'available';
        const title = chartTooltip(point, item.product, definition);
        marks += `<g tabindex="0" role="img" aria-label="${escapeHtml(title)}" class="chart-focus">
          <rect x="${x}" y="${y}" width="${barWidth}" height="${barHeight}" rx="2" fill="${warning ? '#fff' : color}" stroke="${color}" stroke-width="${warning ? 2 : 0}" class="chart-bar ${point.window_kind === 'partial' ? 'partial-mark' : ''}"><title>${escapeHtml(title)}</title></rect>
        </g>`;
      });
    });
  }

  container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(definition.label)}趋势图">
    <g>${grid}</g>
    <line x1="${margin.left}" y1="${margin.top + plotHeight}" x2="${width - margin.right}" y2="${margin.top + plotHeight}" class="axis-line"/>
    ${marks}
    ${labels}
  </svg>`;
}

function renderExactTable(data) {
  const definition = data.metric;
  const head = document.getElementById('trendTableHead');
  const body = document.getElementById('trendTableBody');
  document.getElementById('trendCaption').textContent = `${definition.label} · ${data.range_requested} ${data.grain_label}精确值、分子/分母与状态`;
  head.innerHTML = `<tr><th scope="col">${escapeHtml(data.grain_label)}</th>${data.series.map((item) => `<th scope="col">${escapeHtml(item.label)}</th>`).join('')}</tr>`;
  const pointCount = data.series[0]?.points.length || 0;
  const rows = [];
  for (let index = pointCount - 1; index >= 0; index -= 1) {
    const first = data.series[0].points[index];
    rows.push(`<tr>
      <th scope="row">${escapeHtml(pointPeriod(first))}${first.window_kind === 'partial' ? '<span class="table-tag">进行中</span>' : ''}</th>
      ${data.series.map((item) => {
        const point = item.points[index];
        const fraction = point.numerator !== null && point.numerator !== undefined && point.denominator !== null && point.denominator !== undefined
          ? `<small>${formatValue(point.numerator)} / ${formatValue(point.denominator)}</small>` : '';
        return `<td><span class="exact-cell ${point.status !== 'available' ? 'warning' : ''}" title="${escapeHtml(chartTooltip(point, item.product, definition))}"><strong>${escapeHtml(formatValue(point.value, definition.unit))}</strong>${fraction}<em>${escapeHtml(pointStatus(point))}</em></span></td>`;
      }).join('')}
    </tr>`);
  }
  body.innerHTML = rows.join('');
}

async function loadExplorer() {
  if (explorerController) explorerController.abort();
  explorerController = new AbortController();
  const container = document.getElementById('trendChart');
  setLoading(container, '绘制趋势');
  document.getElementById('explorerStatus').textContent = '';
  try {
    const data = await api('/api/trends', {
      metric: state.metric,
      products: state.products.join(','),
      grain: state.grain,
      range: state.range,
      chart: state.chart,
      include_partial: state.partial ? 1 : 0,
    }, { signal: explorerController.signal });
    state.partial = data.include_partial;
    document.getElementById('partialToggle').checked = state.partial;
    renderSvgChart(data);
    renderExactTable(data);
    const nullPoints = data.series.flatMap((item) => item.points).filter((point) => point.value === null).length;
    const warningPoints = data.series.flatMap((item) => item.points).filter((point) => point.value !== null && point.status !== 'available').length;
    document.getElementById('explorerStatus').innerHTML = `显示 ${data.series[0]?.points.length || 0} 个${data.grain_label}周期 · ${data.series.length} 条系列${nullPoints ? ` · <span>${nullPoints} 个不可用点已断开</span>` : ''}${warningPoints ? ` · <span>${warningPoints} 个点需结合状态解释</span>` : ''}`;
  } catch (error) {
    if (error.name === 'AbortError') return;
    showInlineError(container, error, loadExplorer);
  }
}

function factByKey(week, scope, key) {
  return week?.facts.find((fact) => fact.scope === scope && fact.quality_key === key) || null;
}

function qualityValue(fact) {
  if (!fact || ['source_unavailable', 'pre_launch', 'not_applicable'].includes(fact.status)) return '—';
  if (fact.value_pct === null || fact.value_pct === undefined) return '—';
  return `${Number(fact.value_pct).toFixed(1)}%`;
}

function qualityStatus(fact) {
  return fact?.status_label || STATUS_LABELS[fact?.status] || fact?.status || '未采集';
}

function renderQuality(data) {
  const latest = data.weeks[data.weeks.length - 1];
  const domain = factByKey(latest, 'registrations', 'domain_coverage');
  const message = factByKey(latest, 'Palmly', 'message_id_coverage');
  const linkage = factByKey(latest, 'Palmly', 'chat_linkage_coverage');
  const overlap = factByKey(latest, 'All', 'multi_product_overlap');
  document.getElementById('qualitySummary').innerHTML = [
    metricCard('domain 覆盖率', { value: domain?.value_pct, status: domain?.status, status_label: qualityStatus(domain), numerator: domain?.numerator, denominator: domain?.denominator }, null, 'percent', '新增账号 domain 字段完整性'),
    metricCard('Palmly message_id', { value: message?.value_pct, status: message?.status, status_label: qualityStatus(message), numerator: message?.numerator, denominator: message?.denominator }, null, 'percent', '报告中 message_id 完整率'),
    metricCard('Palmly 聊天关联', { value: linkage?.value_pct, status: linkage?.status, status_label: qualityStatus(linkage), numerator: linkage?.numerator, denominator: linkage?.denominator }, null, 'percent', '有 message_id 的报告关联 chat_logs'),
    metricCard('多产品活跃重叠', { value: overlap?.value_pct, status: overlap?.status, status_label: qualityStatus(overlap), numerator: overlap?.numerator, denominator: overlap?.denominator }, null, 'percent', '解释为何产品活跃不可相加'),
  ].join('');

  document.getElementById('qualityTableBody').innerHTML = [...data.weeks].reverse().map((week) => {
    const facts = {
      domain: factByKey(week, 'registrations', 'domain_coverage'),
      message: factByKey(week, 'Palmly', 'message_id_coverage'),
      linkage: factByKey(week, 'Palmly', 'chat_linkage_coverage'),
      overlap: factByKey(week, 'All', 'multi_product_overlap'),
    };
    const statuses = [...new Set(Object.values(facts).filter(Boolean).map(qualityStatus))];
    return `<tr>
      <th scope="row">${escapeHtml(week.week_start)}${week.window_kind === 'partial' ? '<span class="table-tag">进行中</span>' : ''}</th>
      <td>${escapeHtml(qualityValue(facts.domain))}</td>
      <td>${escapeHtml(qualityValue(facts.message))}</td>
      <td>${escapeHtml(qualityValue(facts.linkage))}</td>
      <td>${escapeHtml(qualityValue(facts.overlap))}</td>
      <td><span class="status-list">${statuses.map((status) => `<span>${escapeHtml(status)}</span>`).join('')}</span></td>
    </tr>`;
  }).join('');

  const sourceLabels = {
    users: '用户注册源',
    chat_logs: '聊天记录源',
    lunara_reports: 'Lunara 报告源',
    learning_coach: '学习教练首次行为',
  };
  document.getElementById('sourceGrid').innerHTML = Object.entries(sourceLabels).map(([key, label]) => {
    const source = data.sources[key] || {};
    return `<article class="source-card">
      <h3>${escapeHtml(label)}</h3>
      <dl>
        <div><dt>起始</dt><dd>${escapeHtml(formatDateTime(source.first_at))}</dd></div>
        <div><dt>最新</dt><dd>${escapeHtml(formatDateTime(source.last_at || source.freshness))}</dd></div>
      </dl>
    </article>`;
  }).join('');
}

async function loadQuality() {
  if (viewController) viewController.abort();
  viewController = new AbortController();
  const container = document.getElementById('qualitySummary');
  setLoading(container, '载入数据质量');
  try {
    const data = await api('/api/quality', { weeks: 12 }, { signal: viewController.signal });
    renderQuality(data);
    document.getElementById('updateTime').textContent = `质量规则 ${data.rule_version}`;
    document.getElementById('windowBadge').hidden = true;
  } catch (error) {
    if (error.name === 'AbortError') return;
    showInlineError(container, error, loadQuality);
  }
}

function renderDefinitions() {
  const productCards = ['All', 'iWeaver', 'Palmly', 'LearningCoach'].map((key) => {
    const item = catalog.products.find((product) => product.key === key);
    const definition = overviewData?.definitions?.[key];
    return `<article class="definition-card"><h3>${escapeHtml(item.label)}</h3>${definition ? `<p>${escapeHtml(definition)}</p>` : ''}</article>`;
  }).join('');
  const metricCards = catalog.metrics.map((metric) => `<article class="metric-definition">
    <div><h3>${escapeHtml(metric.label)}</h3><span class="classification-badge ${escapeHtml(metric.classification)}">${escapeHtml(metric.classification)}</span></div>
    <p>${escapeHtml(metric.description)}</p>
    <dl>
      <div><dt>单位</dt><dd>${escapeHtml(metric.unit)}</dd></div>
      <div><dt>适用产品</dt><dd>${escapeHtml(metric.products.map((product) => PRODUCT_LABELS[product]).join('、'))}</dd></div>
      <div><dt>图表</dt><dd>${escapeHtml(metric.charts.map((chart) => chart === 'line' ? '折线' : '柱状').join(' / '))}</dd></div>
      <div><dt>粒度</dt><dd>${escapeHtml((metric.grains || ['week']).map((grain) => FALLBACK_GRAIN_OPTIONS[grain].label).join(' / '))}</dd></div>
    </dl>
  </article>`).join('');
  document.getElementById('definitionsContent').innerHTML = `
    <div class="definitions-intro">
      <h2>规则版本 ${escapeHtml(catalog.rule_version)}</h2>
      <p>数值与状态分开存储：真实 0 只在数据可用时显示；未接入、未上线、未成熟、关联不完整与小样本均单独标记。</p>
    </div>
    <div class="definition-callout">
      <strong>解释原则</strong>
      <span>精确数据不与归因数据混称；domain 缺失不外推；Palmly 仅使用 Lunara；产品活跃不可相加。</span>
    </div>
    <h2 class="definition-section-title">产品口径</h2>
    <div class="definition-product-grid">${productCards}</div>
    <h2 class="definition-section-title">指标目录</h2>
    <div class="metric-definition-grid">${metricCards}</div>`;
  document.getElementById('updateTime').textContent = `规则版本 ${catalog.rule_version} · ${catalog.metrics.length} 个指标`;
}

async function loadCurrentView() {
  updatePageHeading();
  setViewVisibility();
  if (state.view === 'quality') await loadQuality();
  else if (state.view === 'definitions') renderDefinitions();
  else await loadDashboard();
}

function openMobileMenu() {
  document.getElementById('sidebar').classList.add('open');
  document.getElementById('mobileOverlay').classList.add('open');
  document.getElementById('menuButton').setAttribute('aria-expanded', 'true');
}

function closeMobileMenu() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('mobileOverlay').classList.remove('open');
  document.getElementById('menuButton').setAttribute('aria-expanded', 'false');
}

function bindEvents() {
  document.querySelectorAll('.nav-item').forEach((item) => {
    item.addEventListener('click', (event) => {
      event.preventDefault();
      navigate(item.dataset.view, true);
    });
  });
  document.getElementById('weekSelect').addEventListener('change', (event) => {
    state.week = event.target.value;
    syncUrl();
    loadCurrentView();
  });
  document.getElementById('metricSelect').addEventListener('change', (event) => {
    state.metric = event.target.value;
    const definition = metricDefinition();
    state.products = state.products.filter((product) => definition.products.includes(product));
    if (!state.products.length) state.products = [definition.products[0]];
    state.chart = definition.default_chart;
    state.partial = definition.partial_allowed;
    syncExplorerControls();
    syncUrl();
    loadExplorer();
  });
  document.getElementById('grainSelect').addEventListener('change', (event) => {
    state.grain = event.target.value;
    state.range = grainConfig().default_range;
    const definition = metricDefinition();
    if (!(definition.grains || ['week']).includes(state.grain)) {
      state.metric = 'active_users';
      state.products = state.view === 'overview' ? ['All'] :
        ['iWeaver', 'Palmly', 'LearningCoach'].includes(state.view) ? [state.view] : ['All'];
      state.chart = metricDefinition().default_chart;
    }
    normalizeState();
    syncExplorerControls();
    syncUrl();
    loadExplorer();
  });
  document.getElementById('rangeSelect').addEventListener('change', (event) => {
    state.range = Number(event.target.value);
    syncUrl();
    loadExplorer();
  });
  document.querySelectorAll('#chartButtons button').forEach((button) => {
    button.addEventListener('click', () => {
      if (button.disabled) return;
      state.chart = button.dataset.chart;
      syncExplorerControls();
      syncUrl();
      loadExplorer();
    });
  });
  document.getElementById('partialToggle').addEventListener('change', (event) => {
    state.partial = event.target.checked;
    syncUrl();
    loadExplorer();
  });
  document.getElementById('logoutBtn').addEventListener('click', async (event) => {
    event.preventDefault();
    await fetch('/logout', { method: 'POST', credentials: 'same-origin' });
    window.location.href = '/login';
  });
  document.getElementById('menuButton').addEventListener('click', openMobileMenu);
  document.getElementById('sidebarClose').addEventListener('click', closeMobileMenu);
  document.getElementById('mobileOverlay').addEventListener('click', closeMobileMenu);
  window.addEventListener('popstate', async () => {
    parseUrlState();
    normalizeState();
    syncExplorerControls();
    document.getElementById('weekSelect').value = state.week;
    await loadCurrentView();
  });
}

async function init() {
  parseUrlState();
  bindEvents();
  try {
    [catalog, weeks] = await Promise.all([
      api('/api/metrics', {}, { cache: true }),
      api('/api/weeks', {}, { cache: true }),
    ]);
    if (!weeks?.length) throw new Error('暂无可用周度数据');
    normalizeState();
    renderMetricOptions();
    const weekSelect = document.getElementById('weekSelect');
    weekSelect.innerHTML = weeks.map((week, index) => `<option value="${escapeHtml(week)}">${escapeHtml(week)}${index === 0 ? '（最新）' : ''}</option>`).join('');
    weekSelect.value = state.week;
    document.getElementById('sidebarVersion').textContent = `规则 ${catalog.rule_version} · ${catalog.metrics.length} 个指标`;
    syncExplorerControls();
    syncUrl();
    await loadCurrentView();
  } catch (error) {
    showInlineError(document.getElementById('content'), error, () => window.location.reload());
  }
}

document.addEventListener('DOMContentLoaded', init);
