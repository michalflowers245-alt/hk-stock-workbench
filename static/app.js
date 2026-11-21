const view = document.querySelector('#view');
const dialog = document.querySelector('#dialog');
const toastElement = document.querySelector('#toast');

const routes = { overview: '今日总览', market: '港股行情', ipo: '打新日历', offers: '平台活动', operations: '任务与资金', ledger: '收益台账', profiles: '我的账户', settings: '本地状态' };
const indices = { 'HK.800000': '恒生指数', 'HK.800700': '恒生科技指数', 'HK.800100': '恒生中国企业指数' };
const platforms = { za: 'ZA Bank', welab: 'WeLab Bank', eddid: '艾德', fafa: 'FaFa 熊猫证券', phillip: '辉立' };
const quoteStates = {
  public_snapshot: ['公开快照', 'good'], pending_auth: ['待更新', ''], live: ['实时', 'good live'], delayed: ['延迟', 'warn'], closed: ['休市', ''],
  stale: ['已过时', 'warn'], unavailable: ['快照暂不可用', 'bad'],
};
const serviceStates = {
  ...quoteStates, pending: ['待配置', 'warn'], ready: ['已就绪', 'good'], running: ['运行中', 'good'], sent: ['已发送', 'good'],
  error: ['连接失败', 'bad'], degraded: ['部分异常', 'warn'],
};
const reviewStates = { pending: ['待核对', 'warn'], eligible: ['已核对 · 符合', 'good'], ineligible: ['已核对 · 不符合', ''] };
const coverageLabels = { public: '公开活动页', public_and_app_manual: '公开线索 · App 专属活动需录入', terms: '官方条款文件', offer_detail: '活动详情页' };
const ledgerKinds = {
  cash: ['现金到账', 'good'], fees: ['实际费用', ''], voucher: ['券已到账', 'good'],
  pending_cash: ['待到账现金', 'warn'], pending_voucher: ['待到账券', 'warn'],
  trading_pnl: ['交易盈亏', ''], fx_pnl: ['汇兑盈亏', ''],
};
const currencies = { HKD: '港币 HKD', CNY: '人民币 CNY', USD: '美元 USD' };
const accountSources = {
  za: { help: 'ZA Bank App → 月结单与通知书', url: 'https://bank.za.group/hk/faqs/Individuals/Transfer/Transfer_App_operation/67b5964cccffd730c885c6d2' },
  welab: { help: 'WeLab Bank App → 总览 → 电子月结单', url: 'https://www.welab.bank/zh-CN/support/bank-account/how-can-i-see-my-estatement/' },
  eddid: { help: 'Eddid ONE 或登记邮箱收到的电子结单', url: 'https://www.eddid.com.hk/sf/support/' },
};
const accountKinds = { balances: '余额', positions: '持仓', transactions: '流水' };
const accountColumns = {
  balances: [['observed_on', '日期'], ['label', '项目'], ['currency', '币种'], ['amount', '金额'], ['source', '来源']],
  positions: [['observed_on', '日期'], ['symbol', '标的代码'], ['label', '名称'], ['currency', '币种'], ['quantity', '数量'], ['market_value', '市值'], ['source', '来源']],
  transactions: [['observed_on', '日期'], ['reference', '流水编号'], ['description', '说明'], ['currency', '币种'], ['amount', '金额'], ['source', '来源']],
};
const accountStates = { unknown: '待填写', none: '未开户', active: '已有账户', closed: '已销户' };
const assessmentStates = { satisfied: ['条件已满足', 'good'], unknown: ['条件待补充', 'warn'], unmet: ['有条件不符', 'bad'] };
const taskStages = { registration: '开户 / 报名', deposit: '入金', qualify: '达标', hold: '留存', claim: '领奖', payout: '到账' };
const reviewFields = {
  eligibility: '参与资格', capital: '所需资金', holding: '持有/留存时间', fees: '费用', reward: '奖励', payout: '预计到账条件与时间', note: '备注',
};
const state = {
  route: 'overview', dashboard: {}, offers: [], ipos: [], ledger: {}, offerFilters: { query: '', platform: '', stage: 'all' },
  offerLimit: 12, symbol: 'HK.800000', chartDays: 1, chartObserver: null, pageLoad: 0, dialogLoad: 0, tutorial: null,
  toastTimer: null, profiles: [], reservations: {}, preferences: {}, alerts: [], tasks: [], currentOffer: null, notices: { events: [], unread: 0 },
  accountPreview: null,
};

const hongKongDate = new Intl.DateTimeFormat('sv-SE', {
  timeZone: 'Asia/Hong_Kong', year: 'numeric', month: '2-digit', day: '2-digit',
});
const hongKongTime = new Intl.DateTimeFormat('zh-HK', {
  timeZone: 'Asia/Hong_Kong', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});
const preciseTime = new Intl.DateTimeFormat('zh-HK', {
  timeZone: 'Asia/Hong_Kong', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
});
const numberFormat = new Intl.NumberFormat('zh-HK', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function escapeHtml(value = '') {
  return String(value ?? '').replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
}

function validDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date : null;
}

function formatDate(value, withTime = false) {
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const date = validDate(value);
  if (!date) return '—';
  const day = hongKongDate.format(date);
  return withTime ? `${day} ${hongKongTime.format(date)}` : day;
}

function timeMarkup(value, withTime = true) {
  return `<time datetime="${escapeHtml(value || '')}">${formatDate(value, withTime)}</time>`;
}

function unix(value) {
  return validDate(value)?.getTime() ?? NaN;
}

function num(value) {
  return value === null || value === undefined || value === '' || !Number.isFinite(Number(value)) ? '—' : numberFormat.format(value);
}

function money(value) {
  const display = num(value ?? 0);
  return display === '—' ? display : `HK$ ${display}`;
}

function changeMarkup(value) {
  if (num(value) === '—') return '<span class="numeric muted">—</span>';
  const change = Number(value);
  const color = change > 0 ? 'positive' : change < 0 ? 'negative' : '';
  return `<span class="numeric ${color}">${change > 0 ? '+' : ''}${num(change)}%</span>`;
}

function safeUrl(value) {
  if (!value) return '';
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) ? url.href : '';
  } catch {
    return '';
  }
}

function externalLink(value, label, cssClass = 'text-button') {
  const url = safeUrl(value);
  return url ? `<a class="${cssClass}" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)} ↗</a>` : '';
}

function action(label, name, value = '', cssClass = '') {
  return `<button type="button" class="button ${cssClass}" data-action="${escapeHtml(name)}" data-value="${escapeHtml(value)}">${escapeHtml(label)}</button>`;
}

function textAction(label, name, value = '') {
  return `<button type="button" class="text-button" data-action="${escapeHtml(name)}" data-value="${escapeHtml(value)}">${escapeHtml(label)}</button>`;
}

function chip(label, tone = '') {
  return `<span class="chip ${tone}">${escapeHtml(label)}</span>`;
}

function statusChip(status, options = quoteStates, fallback = ['待确认', '']) {
  return chip(...(options[status] || fallback));
}

function platformTag(value) {
  const platform = value.platform || '';
  return `<span class="platform-tag" data-platform="${escapeHtml(platform)}">${escapeHtml(platforms[platform] || value.platform_name || value.name || platform)}</span>`;
}

function heading(title, summary = '', buttons = '') {
  return `<header class="page-heading"><div><h1>${escapeHtml(title)}</h1>${summary ? `<p>${escapeHtml(summary)}</p>` : ''}</div>${buttons ? `<div class="actions">${buttons}</div>` : ''}</header>`;
}

function cardHeader(title, buttons = '', summary = '') {
  return `<header class="card-header"><div><h2>${escapeHtml(title)}</h2>${summary ? `<p>${escapeHtml(summary)}</p>` : ''}</div>${buttons ? `<div class="actions">${buttons}</div>` : ''}</header>`;
}

function empty(title, detail = '') {
  return `<div class="empty-state"><strong>${escapeHtml(title)}</strong>${detail ? `<p>${escapeHtml(detail)}</p>` : ''}</div>`;
}

function table(headingLabels, rows, emptyHeading = '暂无记录', emptyDetail = '') {
  if (!rows.length) return empty(emptyHeading, emptyDetail);
  return `<div class="table-wrap" tabindex="0" aria-label="可以横向滚动的表格"><table class="table"><thead><tr>${headingLabels.map((label) => `<th scope="col">${escapeHtml(label)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
}

function options(values, selected = '') {
  return Object.entries(values).map(([key, value]) => `<option value="${escapeHtml(key)}"${String(selected) === key ? ' selected' : ''}>${escapeHtml(value)}</option>`).join('');
}

function allQuotes() {
  return state.dashboard.quotes || [];
}

function findQuote(symbol) {
  return allQuotes().find((quote) => quote.symbol === symbol);
}

function quoteName(symbol) {
  return findQuote(symbol)?.name || indices[symbol] || symbol;
}

function offerStage(offer) {
  const deadline = unix(offer.deadline);
  if ((Number.isFinite(deadline) && deadline < Date.now()) || (offer.ends_on && offer.ends_on < formatDate(new Date()))) return 'past';
  if (offer.starts_at) {
    const futureStart = offer.starts_at.length === 10 ? offer.starts_at > formatDate(new Date()) : unix(offer.starts_at) > Date.now();
    if (futureStart) return 'planned';
  }
  if (Number.isFinite(deadline) || offer.ends_on) return 'open';
  return 'unknown';
}

function offerTime(offer) {
  if (offer.deadline) return `截止 ${timeMarkup(offer.deadline)}`;
  if (offer.ends_on) return `结束日 ${timeMarkup(offer.ends_on, false)}`;
  return '截止待核对';
}

function offerStageChip(offer) {
  return statusChip(offerStage(offer), { past: ['已结束', ''], planned: ['尚未开始', ''], open: ['期间内', 'good'], unknown: ['日期待核对', 'warn'] });
}

function metric(title, value, note, highlight = false) {
  return `<article class="card metric${highlight ? ' main-metric' : ''}"><h2>${escapeHtml(title)}</h2><p>${escapeHtml(value)}</p>${note ? `<small>${escapeHtml(note)}</small>` : ''}</article>`;
}

function marketHero() {
  const quotes = Object.entries(indices).map(([symbol, name]) => {
    const quote = findQuote(symbol) || {};
    return `<button type="button" class="hero-quote" data-action="show-market" data-value="${symbol}" data-quote-symbol="${symbol}"><span class="quote-name">${name}</span><span class="quote-value numeric" data-quote-field="price">${num(quote.last_price)}</span><span class="quote-caption"><span data-quote-field="change">${changeMarkup(quote.change_percent)}</span><span data-quote-field="short-status">${quoteStates[quote.status]?.[0] || '待数据'}</span></span></button>`;
  }).join('');
  return `<section class="hero" aria-labelledby="main-title"><div class="hero-head"><div><h1 id="main-title">港股总览</h1><p id="latest-capture">${escapeHtml(latestCaptureLabel())}</p></div><span class="hero-release" id="live-summary">${liveSummary()}</span></div><div class="hero-data">${quotes}</div></section>`;
}

function latestCaptureLabel() {
  const lastCapture = allQuotes().map((quote) => quote.captured_at).filter(Boolean).sort((a, b) => unix(b) - unix(a))[0];
  return lastCapture ? `最新记录 ${formatDate(lastCapture, true)}` : '还没有快照，可手动更新';
}

function liveSummary() {
  const snapshotCount = allQuotes().filter((quote) => quote.status === 'public_snapshot').length;
  return `<i class="status-dot ${snapshotCount ? 'good' : ''}" aria-hidden="true"></i>${snapshotCount ? `${snapshotCount} 个公开快照` : '按需更新 · 非实时'}`;
}

function localQuoteDetail() {
  const info = state.dashboard.service?.quotes;
  return info?.status === 'pending_auth' ? '本地版使用公开行情快照，无需授权。点击更新可尝试获取。' : info?.detail || '公开快照只作记录，时效以来源的报价时间为准。';
}

function chartCard(full = false) {
  const quotes = Object.fromEntries(allQuotes().map(({ symbol, name }) => [symbol, name || indices[symbol] || symbol]));
  if (!quotes[state.symbol]) state.symbol = Object.keys(quotes)[0] || 'HK.800000';
  const quote = findQuote(state.symbol);
  const title = full ? '历史走势' : '行情走势';
  const shortcut = full ? '' : '<a href="#market" class="text-button">全部行情 →</a>';
  const selector = Object.keys(quotes).length ? `<label>标的<select id="chart-symbol">${options(quotes, state.symbol)}</select></label>` : '';
  const exportUrl = `/api/quotes/export.csv?${new URLSearchParams({ symbol: state.symbol, days: state.chartDays })}`;
  return `<section class="card${full ? ' chart-page' : ''}" aria-label="${title}">${cardHeader(title, shortcut)}<div class="chart-controls"><div class="actions">${selector}<label>区间<select id="chart-days">${options({ 1: '最近 1 天', 7: '最近 7 天', 30: '最近 30 天' }, state.chartDays)}</select></label></div><a class="button" href="${escapeHtml(exportUrl)}">下载 CSV</a></div><div class="chart-current" data-quote-symbol="${escapeHtml(state.symbol)}"><strong data-quote-field="price">${num(quote?.last_price)}</strong><span data-quote-field="change">${changeMarkup(quote?.change_percent)}</span><span data-quote-field="status">${statusChip(quote?.status)}</span></div><div class="chart-area"><canvas id="quote-chart" aria-label="按香港时间显示的价格走势" role="img"></canvas><div class="empty-state" data-chart-status>正在读取历史记录…</div></div><p id="chart-caption">显示工作台采集的历史快照</p></section>`;
}

function compactOffer(offer) {
  return `<li><div class="row-tag">${platformTag(offer)}</div><div class="row-main row-grow"><h3>${textAction(offer.title, 'offer-detail', offer.id)}</h3><small>${offerTime(offer)}</small></div><div class="row-end">${offer.followed ? chip('已关注', 'good') : offerStageChip(offer)}</div></li>`;
}

function fastOffers() {
  const upcoming = state.offers.filter((offer) => offer.followed && unix(offer.deadline) >= Date.now())
    .sort((a, b) => unix(a.deadline) - unix(b.deadline)).slice(0, 4);
  const recent = state.offers.filter((offer) => !['past', 'planned'].includes(offerStage(offer)))
    .slice().sort((a, b) => (unix(b.updated_at || b.seen_at) || 0) - (unix(a.updated_at || a.seen_at) || 0)).slice(0, 4);
  const offers = upcoming.length ? upcoming : recent;
  const title = upcoming.length ? '关注的截止时间' : '近期活动';
  return `<section class="card">${cardHeader(title, '<a href="#offers" class="text-button">活动列表 →</a>')}${offers.length ? `<ul class="rows">${offers.map(compactOffer).join('')}</ul>` : empty('暂无待办活动', '监测到新活动后会出现在这里。')}<div class="card-footer"><span>已收录 ${state.dashboard.counts?.offers ?? state.offers.length} 条 · 已关注 ${state.dashboard.counts?.following ?? 0} 条</span>${textAction('录入活动', 'add-offer')}</div></section>`;
}

function compactIPO(ipo) {
  return `<li><div class="row-main"><strong>${escapeHtml(ipo.name || '待定')}</strong><small>${escapeHtml([ipo.code, boardName(ipo.board)].filter(Boolean).join(' · '))}</small></div><div class="row-end">${ipo.deadline ? timeMarkup(ipo.deadline) : '截止待核对'}</div></li>`;
}

function sourceStatus(source) {
  if (source.partial) return chip('部分来源可读', 'warn');
  if (source.ok === true || source.ok === 1) return chip('公开来源可读', 'good');
  if (source.checked_at) return chip('检查异常', 'bad');
  return chip('等待首次检查', 'warn');
}

function sourceLines(detail = false) {
  const available = state.dashboard.sources || [];
  const sources = Object.keys(platforms).map((platform) => available.filter((source) => source.platform === platform))
    .flatMap((records, index) => {
      if (!records.length) return [{ platform: Object.keys(platforms)[index] }];
      if (detail) return records;
      const working = records.find((record) => record.ok);
      return [{ ...(working || records[0]), partial: Boolean(working && records.some((record) => record.checked_at && !record.ok)) }];
    });
  return `<div class="sources">${sources.map((source) => {
    const coverage = coverageLabels[source.coverage] || source.coverage || '官方页面';
    return `<div class="source-line">${platformTag(source)}<div class="row-main"><strong>${detail ? (externalLink(source.url, coverage) || escapeHtml(coverage)) : '公开活动来源'}</strong><small>上次成功 ${timeMarkup(source.last_success)}</small>${detail && source.error ? `<p class="negative">${escapeHtml(source.error)}</p>` : ''}</div><div class="source-ending">${sourceStatus(source)}</div></div>`;
  }).join('')}</div>`;
}

function renderOverview() {
  const totals = state.dashboard.ledger || {};
  const activeIPOs = state.ipos.filter((ipo) => ipo.deadline && unix(ipo.deadline) >= Date.now())
    .sort((a, b) => unix(a.deadline) - unix(b.deadline));
  const ipoPreview = (activeIPOs.length ? activeIPOs : state.ipos).slice(0, 4);
  const pending = state.tasks.filter((task) => !task.completed)
    .sort((a, b) => (unix(a.due_on) || Infinity) - (unix(b.due_on) || Infinity));
  const overdue = pending.filter((task) => task.due_on && task.due_on < formatDate(new Date())).length;
  const tasks = `<section class="card">${cardHeader('接下来要做', '<a href="#operations" class="text-button">任务与资金 →</a>', pending.length ? `${pending.length} 项未完成${overdue ? `，其中 ${overdue} 项已过期` : ''}` : '挑一个活动，核对条款，再安排自己的参与步骤。')}${pending.length ? `<ul class="rows task-list">${taskRows(pending.slice(0, 5))}</ul>` : `<div class="local-start"><p>已有活动线索可以查看，也可以录入 App 内看到的新活动。</p><div class="actions"><a href="#offers" class="button button-primary">查看平台活动</a>${action('录入活动', 'add-offer')}${action('记一笔收益', 'add-ledger')}</div></div>`}</section>`;
  const quoteRows = Object.entries(indices).map(([symbol, name]) => {
    const quote = findQuote(symbol) || {};
    return `<li data-quote-symbol="${symbol}"><div class="row-main row-grow"><strong>${textAction(name, 'show-market', symbol)}</strong><small data-quote-field="quote_at">${timeMarkup(quote.quote_at)}</small></div><strong class="numeric" data-quote-field="price">${num(quote.last_price)}</strong><span data-quote-field="status">${statusChip(quote.status)}</span></li>`;
  }).join('');
  return `${heading('今天的港股工作台', '活动、任务和收益都留在这台 Mac，无需注册云服务。', action('更新公开资料', 'queue-refresh', 'all'))}${deploymentNotice()}<div class="grid grid-four">${metric('已到账现金', money(totals.cash))}${metric('实际费用', money(totals.fees))}${metric('净现金收益', money(totals.net), '已到账现金减实际费用', true)}${metric('待到账现金', money(totals.pending_cash), `待到账券 ${money(totals.pending_voucher)}`)}</div><div class="grid grid-main">${tasks}${fastOffers()}</div>${noticesSection(true)}<div class="grid grid-even"><section class="card">${cardHeader('行情快照', '<a href="#market" class="text-button">自选与历史 →</a>', '公开资料按需更新，报价时间由来源提供。')}<ul class="rows compact-quotes">${quoteRows}</ul></section><section class="card">${cardHeader('打新日历', '<a href="#ipo" class="text-button">查看日历 →</a>')}${ipoPreview.length ? `<ul class="rows">${ipoPreview.map(compactIPO).join('')}</ul>` : empty('暂无上市记录', '公开公告更新后会显示在这里。')}</section></div>`;
}

function deploymentNotice() {
  return '<p class="info-band local-notice"><strong>本地版 · 无需注册</strong>　Mac 开着且工作台服务运行时更新资料；关机后暂停，已有记录保留，重新打开继续更新。</p>';
}

function noticesSection(compact = false) {
  const notices = state.notices || {};
  const events = (notices.events || []).slice(0, compact ? 5 : 50);
  const unread = Number(notices.unread || 0);
  const links = `${unread ? action('全部标为已读', 'read-all-notices') : ''}${compact ? '<a href="#settings" class="text-button">全部提醒 →</a>' : ''}`;
  const rows = events.map((event) => `<li class="notice-row${event.read_at ? ' is-read' : ''}"><div class="row-main row-grow"><div class="actions">${!event.read_at ? chip('未读', 'good') : chip('已读')}${event.expired ? chip('已过期 · 历史记录') : event.level === 'urgent' || event.level === 'important' ? chip('优先处理', 'warn') : ''}</div><strong>${escapeHtml(event.subject || '工作台提醒')}</strong><small>${timeMarkup(event.created_at)}</small><p class="notice-body">${escapeHtml(event.body || '')}</p></div>${event.read_at ? '' : textAction('已读', 'read-notice', event.id)}</li>`).join('');
  return `<section class="card notice-card">${cardHeader('本地提醒', links, unread ? `${unread} 条未读。条款变化、截止任务和奖励跟进集中放在这里。` : '活动变化和待处理事项会留在工作台中，无需配置邮箱。')}${notices.unavailable ? empty('提醒暂时无法读取', '请稍后刷新。') : events.length ? `<ul class="rows">${rows}</ul>` : empty('目前没有新提醒', '发现活动变化或临近任务日期后，会在这里提示。')}</section>`;
}

function economicsSection(offer) {
  const economics = offer.economics;
  if (!economics) return '';
  return `<section class="card">${cardHeader('这次活动的实际回报', '', '金额折合港币。现金、券与预期奖励分别计算。')}<div class="grid grid-even compact-metrics">${metric('已实现净现金', money(economics.net), '已到账现金减实际费用', true)}${metric('已到账券面值', money(economics.voucher))}${metric('尚待到账现金', money(economics.pending_cash))}${metric('本人实记耗时', economics.actual_minutes == null ? '待记录' : `${economics.actual_minutes} 分钟`)}</div>${economics.simple_annualized_percent == null ? '' : `<p class="economics-rate">简单年化 ${num(economics.simple_annualized_percent)}%</p>`}<p class="detail-help spacing-top">${escapeHtml(economics.annualized_note || '资金占用与到账情况完整后再核算。')}</p></section>`;
}

function renderMarket() {
  const quoteRows = allQuotes().map((quote) => `<tr data-quote-symbol="${escapeHtml(quote.symbol)}"><td class="name strong">${textAction(quoteName(quote.symbol), 'pick-quote', quote.symbol)}<small class="item-date">${escapeHtml(quote.symbol)}</small></td><td class="measure mono" data-quote-field="price">${num(quote.last_price)}</td><td class="measure" data-quote-field="change">${changeMarkup(quote.change_percent)}</td><td data-quote-field="status">${statusChip(quote.status)}</td><td class="item-date-cell" data-quote-field="quote_at">${timeMarkup(quote.quote_at)}</td><td class="item-date-cell" data-quote-field="captured_at">${timeMarkup(quote.captured_at)}</td><td>${externalLink(quote.source_url, quote.source || '公开来源', 'evidence-link') || escapeHtml(quote.source || '—')}</td><td>${indices[quote.symbol] ? '' : textAction('移出', 'remove-symbol', quote.symbol)}</td></tr>`);
  const accountDetail = state.dashboard.service?.quotes;
  return `${heading('港股行情快照', '无需账户的公开快照，不作实时行情保证；报价与采集时间分别记录。', `${action('更新行情快照', 'queue-refresh', 'quotes')}${action('添加自选', 'add-symbol', '', 'button-primary')}<a class="button" href="/api/quotes/export.csv">导出全部</a>`)}<p id="quote-account-status" class="info-band${['live', 'public_snapshot'].includes(accountDetail?.status) ? '' : ' warn'}"${accountDetail?.detail ? '' : ' hidden'}>${escapeHtml(localQuoteDetail())}</p><section class="card">${cardHeader('自选行情')}${table(['标的', '最新价', '涨跌幅', '状态', '报价时间', '采集时间', '来源', '操作'], quoteRows)}</section>${chartCard(true)}<p class="detail-help">快照之间的价格变化不会补录；关闭 Mac 期间没有采集记录。CSV 保留来源与时间，供本人复盘。</p>`;
}

function boardName(value) {
  const name = String(value || '').toUpperCase();
  if (['MAIN BOARD', 'MAIN', '主板'].includes(name)) return '主板';
  return name === 'GEM' ? 'GEM' : value || '';
}

function renderIPO() {
  const ipoRows = state.ipos.map((ipo) => {
    const deadline = ipo.deadline ? timeMarkup(ipo.deadline) : chip('截止待核对', 'warn');
    const evidence = ipo.deadline_evidence ? `<small class="cell-evidence">${escapeHtml(ipo.deadline_evidence)} ${externalLink(ipo.deadline_source_url, '截止依据', 'evidence-link')}</small>` : '';
    const brokerCutoffs = (ipo.broker_cutoffs || ipo.cutoffs || []).map((cutoff) => `<div class="broker-cutoff"><strong>${escapeHtml(platforms[cutoff.platform] || cutoff.platform)}</strong> ${timeMarkup(cutoff.deadline)} ${chip(cutoff.verified ? '已核对' : '待核对', cutoff.verified ? 'good' : 'warn')}${externalLink(cutoff.source_url, '依据', 'evidence-link')}</div>`).join('');
    return `<tr><td class="name strong">${escapeHtml(ipo.name || '待定')}<small class="item-date">${escapeHtml(ipo.code || '—')}</small></td><td>${escapeHtml(boardName(ipo.board))}</td><td class="item-date-cell">${deadline}${evidence}</td><td class="item-date-cell">${brokerCutoffs || '<span class="muted">尚未录入</span>'}${textAction('登记券商截止', 'ipo-cutoff', ipo.identity || ipo.id || ipo.code)}</td><td class="item-date-cell">${timeMarkup(ipo.listing_date, false)}</td><td><div class="actions">${externalLink(ipo.prospectus_url, '招股书')}${externalLink(ipo.allotment_url, '配发结果')}${externalLink(ipo.announcement_url || ipo.source_url, '公告')}</div></td></tr>`;
  });
  const sourceErrors = (state.dashboard.ipo_sources || []).filter((source) => source.checked_at && !source.ok)
    .map((source) => `<p class="info-band warn">港交所来源检查异常。${externalLink(source.url, '查看公告页', 'evidence-link')} ${escapeHtml(source.error || '')}　上次成功 ${timeMarkup(source.last_success)}</p>`).join('');
  return `${heading('打新日历', '港交所公开申购时间与券商接单截止分别记录，申购前按自己的券商核对。', action('检查港交所公告', 'queue-refresh', 'ipos'))}${sourceErrors}<section class="card">${cardHeader('新上市记录')}${table(['发行人 / 代码', '板块', '公开申购截止', '券商截止（香港时间）', '上市日期', '官方文件'], ipoRows, '暂无上市记录', '首次检查港交所公告后会显示可读取的记录。')}</section>`;
}

function filterOffers() {
  const filter = state.offerFilters;
  const query = filter.query.trim().toLowerCase();
  return state.offers.filter((offer) => {
    if (filter.platform && offer.platform !== filter.platform) return false;
    if (query && ![offer.title, offer.platform_name, platforms[offer.platform]].some((value) => value?.toLowerCase().includes(query))) return false;
    if (filter.stage === 'following') return Boolean(offer.followed);
    if (filter.stage === 'review') return !offer.review_state || offer.review_state === 'pending';
    if (['satisfied', 'unmet'].includes(filter.stage)) return offer.assessment?.state === filter.stage;
    if (filter.stage !== 'all') return offerStage(offer) === filter.stage;
    return true;
  }).sort((a, b) => Number(Boolean(b.followed)) - Number(Boolean(a.followed)) || Number(b.assessment?.priority || 0) - Number(a.assessment?.priority || 0) || (unix(b.updated_at || b.seen_at) || 0) - (unix(a.updated_at || a.seen_at) || 0));
}

function promotionCard(offer) {
  const manual = offer.source_kind === 'manual';
  return `<article class="offer-card"><div class="actions">${platformTag(offer)}${manual ? chip('手动录入') : ''}${offer.followed ? chip('已关注', 'good') : ''}</div><h3>${escapeHtml(offer.title)}</h3><p>${offerTime(offer)}</p><div class="actions">${offerStageChip(offer)}${offer.review_stale ? chip('条款变更 · 需重核', 'warn') : statusChip(offer.review_state, reviewStates)}</div>${assessmentMarkup(offer.assessment)}${offer.assessment?.priority_reasons?.length ? `<p class="detail-help">优先级依据：${offer.assessment.priority_reasons.map(escapeHtml).join('；')}</p>` : ''}${textAction('看条款与教程 →', 'offer-detail', offer.id)}</article>`;
}

function offerResults() {
  const matches = filterOffers();
  if (!matches.length) return empty('暂无匹配活动', '可调整筛选条件，或录入平台 App 内看到的活动。');
  const more = matches.length > state.offerLimit ? action('显示更多', 'show-more-offers') : '';
  return `<div class="offer-grid">${matches.slice(0, state.offerLimit).map(promotionCard).join('')}</div><div class="card-footer"><span>显示 ${Math.min(state.offerLimit, matches.length)} / ${matches.length} 条</span>${more}</div>`;
}

function renderOffers() {
  const { query, platform, stage } = state.offerFilters;
  return `${heading('平台活动', '已关注活动置顶，其余根据资格匹配结果与活动类型排序。可在「我的账户」补齐匹配条件。', `${action('检查平台来源', 'queue-refresh', 'offers')}${action('录入活动', 'add-offer', '', 'button-primary')}`)}<section class="card"><div class="filter-bar" aria-label="筛选平台活动"><label class="grow">搜索标题<input id="offer-query" type="search" value="${escapeHtml(query)}" placeholder="输入标题关键词"></label><label>平台<select id="offer-platform">${options({ '': '全部平台', ...platforms }, platform)}</select></label><label>状态<select id="offer-stage">${options({ all: '全部状态', following: '已关注', satisfied: '已满足匹配条件', unmet: '有条件不符', review: '待核对', open: '活动期间', unknown: '日期待核对', past: '已结束', planned: '尚未开始' }, stage)}</select></label></div><div id="offer-results">${offerResults()}</div></section>`;
}

function ledgerEntryNote(entry) {
  return `${escapeHtml(entry.note || '—')}${entry.expected_on ? `<small class="cell-evidence">预计 ${timeMarkup(entry.expected_on, false)} ${entry.overdue ? chip('已逾期 · 待跟进', 'warn') : ''}</small>` : ''}${entry.voucher_scope ? `<small class="cell-evidence">券用途：${escapeHtml(entry.voucher_scope)}</small>` : ''}${entry.kind === 'voucher' ? `<small class="cell-evidence">实际抵扣 ${currencyMoney(entry.redeemed_value || 0, entry.currency)}</small>` : ''}`;
}

function renderLedger() {
  const totals = state.ledger.totals || {};
  const entries = state.ledger.entries || [];
  const ledgerRows = entries.map((entry) => `<tr><td class="item-date-cell">${timeMarkup(entry.occurred_on, false)}<small class="item-date">#${escapeHtml(entry.id)}</small></td><td class="name">${entry.offer_id ? textAction(entry.offer_title || '活动详情', 'offer-detail', entry.offer_id) : escapeHtml(entry.offer_title || '其他')}</td><td>${statusChip(entry.kind, ledgerKinds)}${entry.expected_entry_id ? `<small class="item-date">兑现预计 #${escapeHtml(entry.expected_entry_id)}</small>` : ''}</td><td class="measure mono">${currencyMoney(entry.amount, entry.currency)}${entry.currency !== 'HKD' ? `<small class="item-date">折合 ${money(entry.amount_hkd)}</small>` : ''}${entry.kind?.startsWith('pending_') ? `<small class="item-date">已兑现 ${currencyMoney(entry.settled_amount || 0, entry.currency)}<br>尚待 ${currencyMoney(entry.remaining_amount ?? entry.amount, entry.currency)}</small>` : ''}</td><td class="note">${ledgerEntryNote(entry)}</td><td><div class="actions">${entry.kind?.startsWith('pending_') && (entry.remaining_amount ?? entry.amount) > 0 ? textAction('登记本次到账', 'settle-ledger', entry.id) : ''}${entry.kind === 'voucher' ? textAction('记录使用', 'voucher-use', entry.id) : ''}${textAction('撤销', 'remove-ledger', entry.id)}</div></td></tr>`);
  const groups = new Map();
  for (const entry of entries) {
    const key = entry.offer_id || 'other';
    const group = groups.get(key) || { id: entry.offer_id, title: entry.offer_title || '其他', cash: 0, fees: 0, voucher: 0, trading_pnl: 0, fx_pnl: 0, pending_cash: 0 };
    if (Object.hasOwn(group, entry.kind)) group[entry.kind] += entry.kind === 'pending_cash' ? Number(entry.remaining_amount ?? entry.amount) * Number(entry.rate_hkd || 1) : Number(entry.amount_hkd ?? entry.amount);
    groups.set(key, group);
  }
  const activityRows = [...groups.values()].map((group) => `<tr><td class="name">${group.id ? textAction(group.title, 'offer-detail', group.id) : escapeHtml(group.title)}</td><td class="measure mono">${money(group.cash)}</td><td class="measure mono">${money(group.fees)}</td><td class="measure mono strong">${money(group.cash - group.fees)}</td><td class="measure mono">${money(group.trading_pnl)} / ${money(group.fx_pnl)}</td><td class="measure mono">${money(group.voucher)}</td><td class="measure mono">${money(group.pending_cash)}</td></tr>`);
  const performanceRows = (state.ledger.platform_performance || []).map((item) => `<tr><td>${platformTag({ platform: item.platform })}</td><td class="measure mono">${money(item.cash)}</td><td class="measure mono">${money(item.fees)}</td><td class="measure mono strong">${money(item.net)}</td><td>${item.timed_income_offer_count > 0 ? `${escapeHtml(item.actual_minutes)} 分钟` : '待记录'}${item.income_offer_count ? `<small class="item-date">已记耗时 ${item.timed_income_offer_count || 0} / ${item.income_offer_count} 个收益活动</small>` : ''}</td><td class="measure mono">${item.cash_per_hour == null ? '待补齐实操耗时' : `${money(item.cash_per_hour)} / 小时`}</td></tr>`);
  return `${heading('收益台账', '保留原预计奖励，每次到账另记一笔。汇总按录入汇率折合港币；券面值与待到账不计入净现金。', action('新增记录', 'add-ledger', '', 'button-primary'))}<div class="grid grid-four">${metric('已到账现金', money(totals.cash))}${metric('实际费用', money(totals.fees))}${metric('活动净现金', money(totals.net), '已到账现金减实际费用', true)}${metric('券面值（已到账）', money(totals.voucher))}${metric('尚待到账现金', money(totals.pending_cash), `原预计 ${money(totals.expected_cash)}`)}${metric('尚待到账券', money(totals.pending_voucher), `原预计 ${money(totals.expected_voucher)}`)}${metric('交易盈亏', money(totals.trading_pnl), '盈利填正数，亏损填负数')}${metric('汇兑盈亏', money(totals.fx_pnl), '与活动奖励分开核算')}</div><section class="card">${cardHeader('平台投入与回报', '', '按本人实记耗时核算。只有有收益或费用的活动都记录耗时后，才显示净现金时薪。')}${table(['平台', '到账现金', '费用', '活动净现金', '实际耗时', '净现金时薪'], performanceRows, '暂无复盘记录')}</section><section class="card">${cardHeader('按活动复盘', '', '金额均折合港币；活动净现金不含券、交易及汇兑盈亏。')}${table(['活动', '到账现金', '费用', '活动净现金', '交易 / 汇兑', '券面值', '尚待现金'], activityRows, '暂无活动收益')}</section><section class="card">${cardHeader('流水与奖励兑现')}${table(['日期 / 编号', '活动', '类型', '原币金额', '备注', '操作'], ledgerRows, '暂无收益记录', '先登记预期奖励，再通过「登记本次到账」分笔确认。')}</section>`;
}

function serviceLine(name, info) {
  return `<div class="service-line"><div class="actions"><strong>${escapeHtml(name)}</strong>${statusChip(info?.status, serviceStates)}</div>${info?.detail ? `<p>${escapeHtml(info.detail)}</p>` : ''}<small>最近状态 ${timeMarkup(info?.updated_at)}</small></div>`;
}

function renderSettings() {
  const { service = {}, runtime = {} } = state.dashboard;
  const quoteService = service.quotes?.status === 'pending_auth' ? { ...service.quotes, status: 'ready', detail: localQuoteDetail() } : service.quotes;
  return `${heading('本地状态与提醒', '查看这台 Mac 上的资料更新、来源检查和提醒记录。', action('检查全部来源', 'queue-refresh', 'all'))}${deploymentNotice()}<div class="grid grid-even"><section class="card">${cardHeader('本机服务')}${serviceLine('公开行情快照', quoteService)}${serviceLine('公开活动与公告更新', service.worker)}</section><section class="card">${cardHeader('我的本地资料')}<div class="local-data"><p>活动条款、任务、资金和收益台账保存在这台 Mac。页面关闭后，已有记录不会丢失。</p><p>关闭或休眠 Mac 时暂停更新，重新运行工作台后继续；关机期间的行情不会自动补齐。</p><p>教程可以下载为 Markdown，行情记录可以导出 CSV。活动 App 专属内容可手动录入。</p>${runtime.data_dir ? `<p><strong>数据位置</strong><br><code>${escapeHtml(runtime.data_dir)}</code></p>` : ''}<div class="actions"><a href="#offers" class="button">活动与教程</a><a href="#ledger" class="button">收益台账</a><a href="#market" class="button">导出行情</a></div></div></section></div>${noticesSection()}<section class="card">${cardHeader('五家平台来源', '', '显示每个公开来源最近一次成功更新时间；无法读取的来源会保留具体错误。')}${sourceLines(true)}</section>`;
}

function alertsSection() {
  const rules = state.alerts.rules || [];
  const rows = rules.map((rule) => `<tr><td class="name">${escapeHtml(quoteName(rule.symbol))}<small class="item-date">${escapeHtml(rule.symbol)}</small></td><td>${escapeHtml(rule.field === 'change_percent' ? '涨跌幅' : '最新价')} ${rule.operator === 'lte' ? '≤' : '≥'} <strong class="mono">${num(rule.threshold)}${rule.field === 'change_percent' ? '%' : ''}</strong></td><td>${chip(rule.enabled ? '已开启' : '已暂停', rule.enabled ? 'good' : '')}</td><td><div class="actions">${textAction('编辑', 'edit-alert', rule.id)}${textAction('删除', 'remove-alert', rule.id)}</div></td></tr>`);
  const events = (state.alerts.events || []).slice(0, 5).map((event) => `<li><div class="row-main"><strong>${escapeHtml(event.subject)}</strong><small>${timeMarkup(event.created_at)}${event.last_error ? ` · ${escapeHtml(event.last_error)}` : ''}</small></div>${chip(event.sent_at ? '已发送' : event.cancelled_at ? '已取消' : event.last_error ? '投递待重试' : '等待发送', event.sent_at ? 'good' : 'warn')}</li>`).join('');
  return `<section class="card">${cardHeader('我的行情预警', action('添加预警', 'add-alert'), '仅在有效实时行情达到阈值时提醒。首次触发后防抖，同一规则 24 小时内不重复发信。')}${table(['标的', '触发条件', '状态', '操作'], rows, '暂无行情预警', '为关注的股票或指数设置价格、涨跌幅阈值。')}${events ? `<details class="editor-details"><summary>近期预警投递</summary><ul class="rows">${events}</ul></details>` : ''}</section>`;
}

function formAlert(id = '') {
  const rule = (state.alerts.rules || []).find((item) => String(item.id) === String(id)) || {};
  const fields = selectField('symbol', '关注标的', Object.fromEntries(allQuotes().map((quote) => [quote.symbol, quoteName(quote.symbol)])), rule.symbol || state.symbol) + selectField('field', '观察数值', { price: '最新价', change_percent: '当日涨跌幅（%）' }, rule.field || 'price') + selectField('operator', '达到条件', { gte: '大于或等于', lte: '小于或等于' }, rule.operator || 'gte') + field('threshold', '阈值', rule.threshold, { inputType: 'number', required: true, attributes: 'step="0.0001"' }) + selectField('enabled', '提醒开关', { true: '开启', false: '暂停' }, String(rule.enabled !== false));
  showDialog(id ? '编辑行情预警' : '添加行情预警', formFrame('alert', fields, '保存预警', id));
}

function formIPOCutoff(identity) {
  const ipo = state.ipos.find((item) => String(item.identity || item.id || item.code) === String(identity));
  if (!ipo) throw new Error('上市记录不存在，请刷新。');
  const fields = selectField('platform', '申购券商', { eddid: platforms.eddid, fafa: platforms.fafa, phillip: platforms.phillip }, 'eddid') + field('deadline', '券商截止时间（香港时间）', '', { inputType: 'datetime-local', required: true }) + field('source_url', '券商公告 / 依据链接', '', { inputType: 'url', wide: true, required: true }) + selectField('verified', '已与券商原公告核对', { false: '尚未核对', true: '已核对' }, 'false') + textField('note', '核对说明', '', 'maxlength="2000"');
  showDialog(`${ipo.name} · 券商申购截止`, `<p class="info-band">登记同一券商会更新其截止记录。公开申购截止仍以港交所公告为准。</p>${formFrame('ipo-cutoff', fields, '保存券商截止', identity)}`);
}

function currencyMoney(amount, currency = 'HKD') {
  return amount === null || amount === undefined || amount === '' ? '待填写' : `${currency === 'HKD' ? 'HK$' : currency === 'CNY' ? '¥' : 'US$'} ${num(amount)}`;
}

function assessmentMarkup(assessment, detail = false) {
  if (!assessment) return chip('尚未匹配账户', 'warn');
  const reasons = detail ? assessment.reasons || [] : (assessment.reasons || []).filter((reason) => reason.state !== 'satisfied').slice(0, 2);
  return `<div class="assessment">${statusChip(assessment.state, assessmentStates)}${reasons.length ? `<ul class="reason-list">${reasons.map((reason) => `<li><span class="reason-dot ${reason.state === 'satisfied' ? 'good' : reason.state === 'unmet' ? 'bad' : 'warn'}"></span><span>${escapeHtml(reason.label)}${reason.detail ? `：${escapeHtml(reason.detail)}` : ''}</span></li>`).join('')}</ul>` : ''}${detail && assessment.priority_reasons?.length ? `<p class="detail-help">排序依据：${assessment.priority_reasons.map(escapeHtml).join('；')}</p>` : ''}</div>`;
}

function renderProfiles() {
  const budget = state.budget || {};
  const cards = Object.entries(platforms).map(([platform, name]) => {
    const profile = state.profiles.find((item) => item.platform === platform) || {};
    return `<section class="card profile-card">${cardHeader(name, action('编辑资料', 'edit-profile', platform))}<div class="actions">${chip(accountStates[profile.status || 'unknown'], !profile.status || profile.status === 'unknown' ? 'warn' : '')}${profile.opened_on ? chip(`开户 ${formatDate(profile.opened_on)}`) : ''}</div><dl class="facts-list"><div><dt>受邀资格</dt><dd>${profile.invited === true ? '已获邀请' : profile.invited === false ? '未获邀请' : '待核对'}</dd></div><div><dt>历史开户</dt><dd>${profile.prior_owner === true ? '曾开过户' : profile.prior_owner === false ? '从未开户' : '待核对'}</dd></div><div><dt>平台可用资金</dt><dd>${currencyMoney(profile.available_capital, profile.currency)}</dd></div></dl>${profile.notes ? `<p class="detail-help">${escapeHtml(profile.notes)}</p>` : ''}</section>`;
  }).join('');
  const accountCards = Object.entries(accountSources).map(([platform, source]) =>
    `<section class="card profile-card">${cardHeader(platforms[platform])}<p class="detail-help">${escapeHtml(source.help)}</p><div class="actions spacing-top">${action('余额、持仓与流水', 'view-account-data', platform, 'button-primary')}${textAction('公开活动', 'platform-offers', platform)}${textAction('录入专属活动', 'add-offer', platform)}</div></section>`).join('');
  return `${heading('我的账户', '查看本人账户记录和活动匹配资料。')}${state.dashboard.runtime?.mode === 'cloud' ? '' : `<div class="page-heading"><div><h2>本人账户记录</h2><p>从 App 或结单整理数据后导入本机，记录保留原币和资料日期。</p></div></div><div class="grid grid-even profile-grid">${accountCards}</div>`}<section class="card budget-card">${cardHeader('活动总预算', action('设置总预算', 'edit-budget'), '独立填写可用于活动的总资金，平台余额不重复计入。')}<strong class="budget-value mono">${currencyMoney(budget.amount, budget.currency)}</strong>${budget.available_hkd != null && budget.currency !== 'HKD' ? `<p class="detail-help">按所填汇率折合 ${money(budget.available_hkd)}</p>` : ''}</section><div class="grid grid-even profile-grid">${cards}</div>`;
}

function renderAccountTable(kind, rows, platform = '') {
  const columns = accountColumns[kind];
  const body = rows.map((row) => {
    const cells = columns.map(([key]) => {
      const value = row[key];
      return `<td>${key === 'observed_on' ? timeMarkup(value, false) : escapeHtml(value === '' || value == null ? '—' : value)}</td>`;
    }).join('');
    const remove = platform ? `<td>${textAction('移除', 'account-remove-record', `${platform}:${kind}:${row.id}`)}</td>` : '';
    return `<tr>${cells}${remove}</tr>`;
  });
  const headers = columns.map(([, title]) => title);
  if (platform) headers.push('操作');
  return table(headers, body, `暂无${accountKinds[kind]}记录`);
}

async function openAccountData(platform) {
  const source = accountSources[platform];
  if (!source) return;
  const serial = ++state.dialogLoad;
  const records = await api(`/api/account-data/${platform}`);
  if (state.dialogLoad !== serial) return;
  const sections = Object.entries(accountKinds).map(([kind, label]) => {
    const id = `${platform}:${kind}`;
    const buttons = action('录入一条', 'account-record', id) + action('导入 CSV', 'account-import', id);
    return `<section class="card">${cardHeader(label, buttons, '按资料日期显示最近 100 条。')}${renderAccountTable(kind, records[kind] || [], platform)}</section>`;
  }).join('');
  const updated = records.last_import_at ? `最近一次录入 ${formatDate(records.last_import_at, true)}。` : '等待首次录入。';
  showDialog(platforms[platform] + ' · 本人账户', `<p class="info-band">${updated} 当前通过本机录入更新。</p><p class="detail-help spacing-top">${escapeHtml(source.help)} ${externalLink(source.url, '官方说明')}</p><div class="stack spacing-top">${sections}</div>`);
}

function accountImportForm(id, content = '') {
  const [platform, kind] = id.split(':');
  const fields = `<label class="wide">选择按模板整理的 CSV<input type="file" id="account-csv-file" accept=".csv,text/csv"></label>` +
    textField('csv', '或直接编辑 CSV 内容', content, 'id="account-csv-text" spellcheck="false" maxlength="70000" required');
  const hints = kind === 'transactions' ? '流水编号请使用结单流水参考或自己取的稳定编号，避免使用账号。流入填正数，流出填负数。' : kind === 'positions' ? '持仓按日期记录，清仓时填写数量 0。市值不详可留空。' : '余额按日期、项目和币种记录。';
  showDialog(`${platforms[platform]} · ${accountKinds[kind]}`, `<p class="info-band">${hints} 来源写 App 页面或结单类型。</p><p class="detail-help spacing-top">按官方资料填写，去除姓名、账号、证件号和其他身份信息。</p><div class="actions spacing-top"><a class="button" href="/api/account-data/template/${kind}" download>下载 CSV 模板</a>${textAction('返回账户记录', 'view-account-data', platform)}</div>${formFrame('account-preview', fields, '预览资料', id)}`);
}

function openAccountImport(id) {
  const [, kind] = id.split(':');
  if (!accountColumns[kind]) return;
  accountImportForm(id, `${accountColumns[kind].map(([, title]) => title).join(',')}\n`);
}

function openAccountRecord(id) {
  const [platform, kind] = id.split(':');
  const columns = accountColumns[kind];
  if (!columns) return;
  const fields = columns.map(([key, title]) => {
    const initial = key === 'observed_on' ? formatDate(new Date()) : key === 'currency' ? 'HKD' : '';
    return field(key, title, initial, { inputType: key === 'observed_on' ? 'date' : 'text', required: key !== 'market_value' });
  }).join('');
  const hint = kind === 'transactions' ? '编号请写结单流水参考或自行编定的编号，流出填负数。' : '资料日期请以当时的 App 页面或结单为准。';
  showDialog(`${platforms[platform]} · 录入${accountKinds[kind]}`, `<p class="info-band">${hint} 来源写结单类型或 App 页面。</p>${formFrame('account-single', fields, '预览记录', id)}`);
}

function accountRecordCsv(kind, fields) {
  const columns = accountColumns[kind];
  const header = columns.map(([, title]) => title).join(',');
  const row = columns.map(([key]) => `"${(fields[key] || '').replaceAll('"', '""')}"`).join(',');
  return `${header}\n${row}\n`;
}

async function previewAccountImport(id, content) {
  const [platform, kind] = id.split(':');
  const serial = state.dialogLoad;
  const preview = await api(`/api/account-data/${platform}/${kind}/preview`, 'POST', { csv: content });
  if (state.dialogLoad !== serial) return;
  state.accountPreview = { id, content, digest: preview.digest };
  showDialog(`${platforms[platform]} · ${accountKinds[kind]}预览`, `<p class="info-band">共 ${preview.total} 行，新增 ${preview.new} 行，已录入 ${preview.existing} 行。${preview.rows.length < preview.total ? `下方显示前 ${preview.rows.length} 行。` : ''}</p><div class="spacing-top">${renderAccountTable(kind, preview.rows)}</div><div class="actions spacing-top">${action('返回修改', 'account-edit-preview', id)}${action('确认导入', 'account-confirm', id, 'button-primary')}</div>`);
}

function taskRows(tasks, showOffer = true) {
  return tasks.map((task) => {
    const overdue = !task.completed && task.due_on && task.due_on < formatDate(new Date());
    const prerequisites = task.prerequisite_ids || [];
    return `<li class="task-row"><span class="task-status ${task.completed ? 'done' : overdue ? 'overdue' : ''}" aria-hidden="true">${task.completed ? '✓' : '○'}</span><div class="row-main row-grow"><strong>${escapeHtml(task.title || taskStages[task.stage] || '任务')}</strong><small>${escapeHtml(taskStages[task.stage] || '')}${task.due_on ? ` · ${timeMarkup(task.due_on, false)}` : ' · 截止待填写'}${overdue ? ' · 已逾期' : ''}</small>${task.blocked_by?.length ? chip('等待前置步骤', 'warn') : ''}${showOffer ? textAction(task.offer_title || '查看活动', 'offer-detail', task.offer_id) : ''}${prerequisites.length ? `<small>前置任务：${prerequisites.map((id) => escapeHtml(tasks.find((item) => String(item.id) === String(id))?.title || state.tasks.find((item) => String(item.id) === String(id))?.title || `#${id}`)).join('、')}</small>` : ''}${task.evidence ? `<small class="task-evidence">凭证：${escapeHtml(task.evidence)}</small>` : ''}</div>${action('编辑', 'edit-task', `${task.offer_id}:${task.id}`)}</li>`;
  }).join('');
}

function renderOperations() {
  const capital = state.reservations || {};
  const reservations = capital.entries || [];
  const pending = state.tasks.filter((task) => !task.completed).sort((a, b) => (unix(a.due_on) || Infinity) - (unix(b.due_on) || Infinity));
  const calendar = (capital.calendar || []).map((period) => `<tr><td class="item-date-cell">${timeMarkup(period.starts_on, false)} — ${timeMarkup(period.ends_on, false)}</td><td class="measure mono">${money(period.reserved_hkd)}</td><td class="measure mono">${capital.budget?.available_hkd == null ? chip('待设置预算', 'warn') : period.shortfall_hkd > 0 ? `<span class="negative">缺 ${money(period.shortfall_hkd)}</span>` : chip('预算内', 'good')}</td></tr>`);
  const rows = reservations.map((entry) => `<tr><td class="name">${textAction(entry.offer_title || state.offers.find((offer) => offer.id === entry.offer_id)?.title || '关联活动', 'offer-detail', entry.offer_id)}</td><td class="measure mono">${currencyMoney(entry.amount, entry.currency)}${entry.currency !== 'HKD' ? `<small class="item-date">折合 ${money(Number(entry.amount) * Number(entry.rate_hkd))}</small>` : ''}</td><td class="item-date-cell">${timeMarkup(entry.starts_on, false)}<br>至 ${timeMarkup(entry.ends_on, false)}</td><td>${statusChip(entry.status, { planned: ['计划占用', 'warn'], active: ['正在占用', 'good'], released: ['已释放', ''] })}</td><td>${textAction('编辑', 'edit-reservation', entry.id)}</td></tr>`);
  return `${heading('任务与资金', '每一步有截止时间和凭证；资金按实际占用期间核算。', action('登记资金占用', 'add-reservation', '', 'button-primary'))}<div class="grid grid-four">${metric('活动总预算', capital.budget?.available_hkd == null ? '待设置' : money(capital.budget.available_hkd), '折合港币')}${metric('最高资金占用', money(capital.peak_reserved_hkd))}${metric('资金冲突时段', String(capital.conflicts?.length || 0), '超出总预算的日期区间')}${metric('未完成任务', String(pending.length))}</div>${capital.budget?.available_hkd == null ? `<p class="info-band warn spacing-top">尚未设置总预算，资金是否冲突无法确认。<a href="#profiles">设置预算</a></p>` : ''}${capital.conflicts?.length ? `<p class="info-band warn spacing-top">有 ${capital.conflicts.length} 个时段资金不足。请调整参与日期、金额，或释放已经结束的占用。</p>` : ''}<div class="grid grid-even"><section class="card">${cardHeader('下一步任务')}<ul class="rows task-list">${pending.length ? taskRows(pending.slice(0, 12)) : `<li>${empty('暂无未完成任务', '进入活动详情，建立六步实操任务。')}</li>`}</ul></section><section class="card">${cardHeader('资金日历', '', '同一天的占用区间合并检查，释放日以录入的最后占用日为准。')}${table(['占用期间', '合计占用', '预算检查'], calendar, '暂无资金安排', '登记活动的入金和预计释放日期。')}</section></div><section class="card">${cardHeader('资金占用明细')}${table(['活动', '金额', '期间', '状态', '操作'], rows, '暂无占用记录')}</section>`;
}

function formProfile(platform) {
  const profile = state.profiles.find((item) => item.platform === platform) || {};
  const tri = { '': '待核对', true: '是', false: '否' };
  const fields = selectField('status', '账户状态', accountStates, profile.status || 'unknown') + field('opened_on', '开户日期', profile.opened_on, { inputType: 'date' }) + selectField('invited', '已获活动邀请', tri, profile.invited == null ? '' : String(profile.invited)) + selectField('prior_owner', '过去是否开过该平台账户', tri, profile.prior_owner == null ? '' : String(profile.prior_owner)) + field('available_capital', '该平台可用资金', profile.available_capital, { inputType: 'number', attributes: 'min="0" step="0.01"' }) + selectField('currency', '资金币种', currencies, profile.currency || 'HKD') + field('rate_hkd', '1 单位原币折合港币', profile.rate_hkd || 1, { inputType: 'number', required: true, attributes: 'min="0.000001" step="0.000001"' }) + textField('notes', '账户补充说明', profile.notes, 'maxlength="2000"');
  showDialog(`${platforms[platform]} · 我的账户`, `<p class="info-band">仅填写判断活动资格需要的信息；无需账号、证件号或密码。</p>${formFrame('profile', fields, '保存账户资料', platform)}`);
}

function formBudget() {
  const budget = state.budget || state.reservations?.budget || {};
  showDialog('活动总预算', formFrame('budget', field('amount', '可用于活动的总金额', budget.amount, { inputType: 'number', required: true, attributes: 'min="0" step="0.01" autofocus' }) + selectField('currency', '预算币种', currencies, budget.currency || 'HKD') + field('rate_hkd', '1 单位原币折合港币', budget.rate_hkd || 1, { inputType: 'number', required: true, attributes: 'min="0.000001" step="0.000001"' }), '保存总预算'));
}

function offerOptions() {
  return Object.fromEntries(state.offers.map((offer) => [offer.id, `${platforms[offer.platform] || '平台'} · ${offer.title}`]));
}

function formReservation(id = '', offerId = '') {
  const entry = (state.reservations.entries || []).find((item) => String(item.id) === String(id)) || {};
  const fields = selectField('offer_id', '关联活动', offerOptions(), entry.offer_id || offerId, true) + field('amount', '占用金额', entry.amount, { inputType: 'number', required: true, attributes: 'min="0.01" step="0.01"' }) + selectField('currency', '币种', currencies, entry.currency || 'HKD') + field('rate_hkd', '1 单位原币折合港币', entry.rate_hkd || 1, { inputType: 'number', required: true, attributes: 'min="0.000001" step="0.000001"' }) + selectField('status', '占用状态', { planned: '计划占用', active: '正在占用', released: '已释放' }, entry.status || 'planned') + field('starts_on', '开始占用日', entry.starts_on, { inputType: 'date', required: true }) + field('ends_on', '最后占用日', entry.ends_on, { inputType: 'date', required: true }) + textField('note', '资金安排备注', entry.note, 'maxlength="2000"');
  showDialog(id ? '编辑资金占用' : '登记资金占用', formFrame('reservation', fields, '保存资金安排', id));
}

function requirementsSection(offer) {
  const requirement = offer.requirements || {}, eligibility = requirement.eligibility || {};
  const tri = { '': '条款未明确 / 待核对', true: '是', false: '否' };
  const fields = selectField('reviewed', '本组条件已与官方原文核对', { false: '尚未核对', true: '已核对' }, String(Boolean(requirement.reviewed))) + field('source_url', '条件依据链接', requirement.source_url || offer.url, { inputType: 'url' }) + selectField('new_customer', '是否仅限新客户', tri, eligibility.new_customer == null ? '' : String(eligibility.new_customer)) + selectField('invitation_required', '是否必须受邀', tri, eligibility.invitation_required == null ? '' : String(eligibility.invitation_required)) + field('opened_after', '开户日期须不早于', eligibility.opened_after, { inputType: 'date' }) + field('opened_before', '开户日期须不晚于', eligibility.opened_before, { inputType: 'date' }) + field('capital_hkd', '最低所需资金（港币）', requirement.capital_hkd, { inputType: 'number', attributes: 'min="0" step="0.01"' }) + field('holding_days', '最低留存天数', requirement.holding_days, { inputType: 'number', attributes: 'min="0" step="1"' }) + textField('notes', '其他限制与待核实事项', requirement.notes, 'maxlength="3000"');
  return `<section class="card"><h3>按我的账户匹配</h3>${assessmentMarkup(offer.assessment, true)}<details class="editor-details"><summary>填写结构化条件</summary>${formFrame('requirements', fields, '保存条件并重新匹配', offer.id)}</details></section>`;
}

function tasksSection(offer) {
  const tasks = offer.tasks || [];
  return `<section class="card">${cardHeader('六步实操', `${action(tasks.length ? '补齐六步任务' : '建立六步任务', 'initialize-tasks', offer.id)}${action('资金安排', 'offer-reservation', offer.id)}`)}<p class="detail-help">开户 / 报名 → 入金 → 达标 → 留存 → 领奖 → 到账。请按真实条款设置前置任务和截止时间。</p>${tasks.length ? `<ul class="rows task-list">${taskRows(tasks.map((task) => ({ ...task, offer_id: offer.id })), false)}</ul>` : empty('尚未建立任务', '生成后可逐项编辑日期、前置任务和完成凭证。')}</section>`;
}

async function formTask(value) {
  const [offerId, taskId] = value.split(':');
  const offer = state.currentOffer?.id === offerId ? state.currentOffer : await api(`/api/offers/${encodeURIComponent(offerId)}`);
  const task = (offer.tasks || []).find((item) => String(item.id) === String(taskId));
  if (!task) throw new Error('任务不存在，请刷新后重试。');
  state.currentOffer = offer;
  const prereqs = (offer.tasks || []).filter((item) => String(item.id) !== String(task.id)).map((item) => `<label class="check-label"><input type="checkbox" name="prerequisite_${escapeHtml(item.id)}" value="${escapeHtml(item.id)}"${(task.prerequisite_ids || []).map(String).includes(String(item.id)) ? ' checked' : ''}>${escapeHtml(item.title || taskStages[item.stage])}</label>`).join('');
  const fields = field('title', '任务名称', task.title || taskStages[task.stage], { required: true, wide: true, attributes: 'maxlength="200"' }) + `<input type="hidden" name="stage" value="${escapeHtml(task.stage)}">` + field('due_on', '截止日期（香港日期）', task.due_on, { inputType: 'date' }) + selectField('completed', '完成状态', { false: '未完成', true: '已完成' }, String(Boolean(task.completed))) + `<fieldset class="wide task-prerequisites"><legend>完成前必须先完成</legend>${prereqs || '<p class="detail-help">暂无其他任务</p>'}</fieldset>` + textField('evidence', '完成凭证 / 记录', task.evidence, 'maxlength="4000" placeholder="例如操作日期、订单编号、凭证存放位置或链接。"');
  showDialog('编辑实操任务', `<div class="actions">${action('返回活动', 'offer-detail', offer.id)}</div>${formFrame('task', fields, '保存任务', `${offer.id}:${task.id}`)}`);
}

function retrospectiveSection(offer) {
  const retro = offer.retrospective || {};
  const fields = field('actual_minutes', '本人实际耗时（分钟）', retro.actual_minutes, { inputType: 'number', required: true, attributes: 'min="0" step="1"' }) + selectField('shareable_approved', '下方步骤已由我核对可分享', { false: '未核对', true: '已核对可分享' }, String(Boolean(retro.shareable_approved))) + textField('problems', '实际遇到的问题与解决方法', retro.problems, 'maxlength="6000"') + textField('shareable_notes', '本人实操后确认可分享的步骤', retro.shareable_notes, 'maxlength="8000" placeholder="只写自己实际验证过的步骤、注意点和到账情况。"');
  return `<section class="card"><h3>本人实操复盘</h3><p class="detail-help">实际耗时和问题用于复盘；已核对的分享步骤会附入教程草稿。</p>${formFrame('retrospective', fields, '保存实操复盘', offer.id)}</section>`;
}

function reviewHistorySection(offer) {
  const reviews = offer.review_history || [];
  const previous = (offer.history || []).find((item) => Number(item.version) < Number(offer.version));
  let diff = '';
  if (previous) {
    const before = new Set(String(previous.text || '').split(/\n+/).map((line) => line.trim()).filter(Boolean));
    const after = new Set(String(offer.text || '').split(/\n+/).map((line) => line.trim()).filter(Boolean));
    const removed = [...before].filter((line) => !after.has(line));
    const added = [...after].filter((line) => !before.has(line));
    const deadlines = previous.deadline !== offer.deadline || previous.ends_on !== offer.ends_on ? `<p class="info-band warn">截止信息有变化：此前 ${escapeHtml(formatDate(previous.deadline || previous.ends_on, Boolean(previous.deadline)))} → 当前 ${escapeHtml(formatDate(offer.deadline || offer.ends_on, Boolean(offer.deadline)))}</p>` : '';
    if (removed.length || added.length || deadlines) diff = `<section class="card"><h3>与上一版的变化</h3>${deadlines}${removed.length ? `<p class="diff-label">已移除 / 修改前</p><pre class="terms diff-removed">${escapeHtml(removed.join('\n'))}</pre>` : ''}${added.length ? `<p class="diff-label">新增 / 修改后</p><pre class="terms diff-added">${escapeHtml(added.join('\n'))}</pre>` : ''}</section>`;
  }
  return `${offer.review_stale ? '<p class="info-band warn spacing-top">官方条款已更新。之前的核对结果已保留，请核对新版本后再确认资格。</p>' : ''}${diff}${reviews.length ? `<section class="card history"><h3>我的历次核对</h3>${reviews.map((review) => `<details><summary>${timeMarkup(review.created_at || review.reviewed_at)} · 条款第 ${escapeHtml(review.version || review.reviewed_version || '—')} 版 · ${escapeHtml(reviewStates[review.review_state]?.[0] || '已记录')}</summary><pre class="terms">${escapeHtml(formatEvidence(review.review || review))}</pre></details>`).join('')}</section>` : ''}`;
}

function chartPoints(records) {
  const points = new Map();
  for (const record of records) {
    const stamp = unix(record.captured_at || record.quote_at);
    if (num(record.last_price) !== '—' && Number.isFinite(stamp)) points.set(stamp, [stamp, Number(record.last_price)]);
  }
  return [...points.values()].sort(([a], [b]) => a - b);
}

function drawHistory(canvas, points) {
  const context = canvas.getContext('2d');
  if (!context) return;
  const { width, height } = canvas.getBoundingClientRect();
  const scale = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.round(width * scale);
  canvas.height = Math.round(height * scale);
  context.scale(scale, scale);
  if (width < 100 || height < 70) return;

  const digits = Math.max(...points.map(([, value]) => num(value).length));
  const left = Math.max(52, digits * 6 + 11);
  const right = width - 13;
  const top = 16;
  const bottom = height - 22;
  const minTime = points[0][0];
  const maxTime = points.at(-1)[0];
  const values = points.map((point) => point[1]);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const padding = Math.max((max - min) * .1, Math.abs(max) * .0003, .01);
  const x = (stamp) => minTime === maxTime ? (left + right) / 2 : left + (stamp - minTime) * (right - left) / (maxTime - minTime);
  const y = (value) => bottom - (value - min + padding) * (bottom - top) / (max - min + 2 * padding);

  context.font = '10px ui-monospace, Menlo, monospace';
  context.textAlign = 'right';
  context.textBaseline = 'middle';
  context.fillStyle = '#58717d';
  context.strokeStyle = '#dedfe5';
  for (let index = 0; index <= 4; index += 1) {
    const lineY = top + index * (bottom - top) / 4;
    const price = max + padding - index * (max - min + 2 * padding) / 4;
    context.beginPath();
    context.moveTo(left, lineY);
    context.lineTo(right, lineY);
    context.stroke();
    context.fillText(num(price), left - 7, lineY);
  }

  const labels = minTime === maxTime ? [minTime] : [minTime, minTime + (maxTime - minTime) / 2, maxTime];
  labels.forEach((stamp, index) => {
    const label = hongKongDate.format(minTime) === hongKongDate.format(maxTime) ? hongKongTime.format(stamp) : hongKongDate.format(stamp).slice(5);
    context.textAlign = labels.length === 1 || index === 1 ? 'center' : index === 0 ? 'left' : 'right';
    context.fillText(label, x(stamp), bottom + 12);
  });

  context.beginPath();
  context.strokeStyle = '#087f69';
  context.lineJoin = 'round';
  context.lineWidth = 1.7;
  const gaps = [];
  points.forEach(([stamp, value], index) => {
    const previous = points[index - 1];
    if (!previous || stamp - previous[0] > 120000) {
      context.moveTo(x(stamp), y(value));
      gaps.push([stamp, value]);
    } else {
      context.lineTo(x(stamp), y(value));
    }
  });
  context.stroke();

  context.fillStyle = '#087f69';
  for (const [stamp, value] of gaps) {
    context.beginPath();
    context.arc(x(stamp), y(value), 2, 0, Math.PI * 2);
    context.fill();
  }
}

async function loadHistory() {
  state.chartObserver?.disconnect();
  const canvas = view.querySelector('#quote-chart');
  if (!canvas) return;
  const status = view.querySelector('[data-chart-status]');
  const caption = view.querySelector('#chart-caption');
  const params = new URLSearchParams({ symbol: state.symbol, days: state.chartDays });
  try {
    const points = chartPoints(await api(`/api/quotes/history?${params}`));
    if (!canvas.isConnected) return;
    if (!points.length) {
      status.textContent = '暂无历史快照。点击“更新行情快照”后，有效报价会保存到本机。';
      return;
    }
    status.hidden = true;
    caption.textContent = `${points.length} 个快照 · ${formatDate(points[0][0], true)} 至 ${formatDate(points.at(-1)[0], true)} · 线段断开展示采集中断或休市`;
    canvas.setAttribute('aria-label', `${quoteName(state.symbol)}价格走势，${caption.textContent}，最新价 ${num(points.at(-1)[1])}`);
    const draw = () => drawHistory(canvas, points);
    draw();
    if ('ResizeObserver' in window) {
      state.chartObserver = new ResizeObserver(draw);
      state.chartObserver.observe(canvas.parentElement);
    }
  } catch (error) {
    if (canvas.isConnected) status.textContent = `历史记录读取失败。${error.message}`;
  }
}

async function refreshQuoteNumbers() {
  if (document.hidden || dialog.open || !['overview', 'market'].includes(state.route)) return;
  const serial = state.pageLoad;
  try {
    const dashboard = await api('/api/dashboard');
    if (state.pageLoad !== serial) return;
    state.dashboard.quotes = dashboard.quotes;
    state.dashboard.service = dashboard.service;
    setConnection(true);
  } catch {
    if (state.pageLoad !== serial) return;
    setConnection(false);
    state.dashboard.quotes = allQuotes().map((quote) => ({ ...quote, status: 'unavailable' }));
  }

  const quotes = Object.fromEntries(allQuotes().map((quote) => [quote.symbol, quote]));
  for (const cell of view.querySelectorAll('[data-quote-field]')) {
    const quote = quotes[cell.closest('[data-quote-symbol]').dataset.quoteSymbol];
    if (!quote) continue;
    switch (cell.dataset.quoteField) {
      case 'price': cell.textContent = num(quote.last_price); break;
      case 'change': cell.innerHTML = changeMarkup(quote.change_percent); break;
      case 'short-status': cell.textContent = quoteStates[quote.status]?.[0] || '待数据'; break;
      case 'status': cell.innerHTML = statusChip(quote.status); break;
      default: cell.innerHTML = timeMarkup(quote[cell.dataset.quoteField]); break;
    }
  }
  const captured = view.querySelector('#latest-capture');
  const live = view.querySelector('#live-summary');
  const accountStatus = view.querySelector('#quote-account-status');
  if (captured) captured.textContent = latestCaptureLabel();
  if (live) live.innerHTML = liveSummary();
  if (accountStatus) {
    const info = state.dashboard.service?.quotes;
    accountStatus.hidden = !info?.detail;
    accountStatus.textContent = localQuoteDetail();
    accountStatus.classList.toggle('warn', !['live', 'public_snapshot'].includes(info?.status));
  }
}

async function api(path, method = 'GET', body) {
  const parameters = { method, cache: 'no-store', headers: { Accept: 'application/json' } };
  if (typeof AbortSignal.timeout === 'function') parameters.signal = AbortSignal.timeout(method === 'GET' ? 8000 : 35000);
  if (body !== undefined) {
    parameters.headers['Content-Type'] = 'application/json';
    parameters.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, parameters);
  } catch {
    throw new Error('工作台暂时无法连接，请稍后重试。');
  }
  const raw = await response.text();
  let result = null;
  try {
    result = raw ? JSON.parse(raw) : null;
  } catch {
    throw new Error('收到无法读取的服务响应。');
  }
  if (!response.ok) throw new Error(typeof result?.error === 'string' ? result.error : `请求失败（${response.status}）。`);
  return result;
}

function updateClock() {
  const now = new Date();
  const clock = document.querySelector('#clock');
  clock.dateTime = now.toISOString();
  clock.textContent = `${hongKongDate.format(now)} ${preciseTime.format(now)} HKT`;
}

function setConnection(connected) {
  document.querySelector('#connection-dot').className = `status-dot ${connected ? 'good' : 'warn'}`;
  document.querySelector('#connection-label').textContent = connected ? '工作台已连接' : '无法连接工作台';
}

function showToast(message, isError = false) {
  clearTimeout(state.toastTimer);
  toastElement.textContent = message;
  toastElement.className = isError ? 'toast error' : 'toast';
  toastElement.hidden = false;
  state.toastTimer = setTimeout(() => { toastElement.hidden = true; }, isError ? 6500 : 3800);
}

const pageRenderers = { overview: renderOverview, market: renderMarket, ipo: renderIPO, offers: renderOffers, operations: renderOperations, ledger: renderLedger, profiles: renderProfiles, settings: renderSettings };

function routeFromHash() {
  const hash = window.location.hash.slice(1);
  return Object.hasOwn(routes, hash) ? hash : 'overview';
}

async function loadPage({ silent = false, scroll = false } = {}) {
  const route = routeFromHash();
  const serial = ++state.pageLoad;
  if (state.route !== route) {
    state.chartObserver?.disconnect();
    view.replaceChildren();
  }
  state.route = route;
  for (const link of document.querySelectorAll('[data-route]')) {
    if (link.dataset.route === route) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  document.querySelector('#breadcrumb').textContent = routes[route];
  document.title = `${routes[route]} · 港股工作台`;
  if (!silent) view.setAttribute('aria-busy', 'true');
  try {
    const [dashboard, offers, ipos, ledger, profiles, reservations, tasks, alerts, preferences, notices] = await Promise.all([
      api('/api/dashboard'),
      ['overview', 'offers', 'ledger', 'operations'].includes(route) ? api('/api/offers') : null,
      ['overview', 'ipo'].includes(route) ? api('/api/ipos') : null,
      route === 'ledger' ? api('/api/ledger') : null,
      route === 'profiles' ? api('/api/profiles') : null,
      route === 'operations' ? api('/api/reservations') : null,
      ['operations', 'overview'].includes(route) ? api('/api/tasks') : null,
      null,
      null,
      ['overview', 'settings'].includes(route) ? api('/api/notices').catch(() => ({ events: [], unread: 0, unavailable: true })) : null,
    ]);
    if (state.pageLoad !== serial) return;
    state.dashboard = dashboard || {};
    if (offers) state.offers = offers;
    if (ipos) state.ipos = ipos;
    if (ledger) state.ledger = ledger;
    if (profiles) { state.profiles = profiles.profiles || []; state.budget = profiles.budget || {}; }
    if (reservations) { state.reservations = reservations; state.budget = reservations.budget || {}; }
    if (tasks) state.tasks = tasks;
    if (alerts) state.alerts = alerts;
    if (preferences) state.preferences = preferences;
    if (notices) state.notices = notices;
    setConnection(true);
    view.innerHTML = pageRenderers[route]();
    void loadHistory();
    if (scroll) window.scrollTo(0, 0);
  } catch (error) {
    if (state.pageLoad !== serial) return;
    setConnection(false);
    if (silent) {
      showToast(error.message, true);
    } else {
      view.innerHTML = `${heading(routes[route])}<section class="card">${empty('工作台加载失败', error.message)}<div class="actions">${action('重试', 'reload', '', 'button-primary')}</div></section>`;
    }
  } finally {
    if (state.pageLoad === serial) view.setAttribute('aria-busy', 'false');
  }
}

function showDialog(title, body) {
  dialog.innerHTML = `<header class="dialog-header"><h2 id="dialog-title">${escapeHtml(title)}</h2>${action('关闭', 'close-dialog', '', 'button-quiet')}</header><div class="dialog-body"><p class="info-band dialog-feedback" role="status" aria-live="polite" hidden></p>${body}</div>`;
  if (!dialog.open) dialog.showModal();
  dialog.scrollTop = 0;
  const target = dialog.querySelector('[autofocus]') || dialog.querySelector('.dialog-header button');
  target?.focus({ preventScroll: true });
}

function formFrame(name, fields, button, value = '') {
  const submission = ['ledger', 'settle-ledger'].includes(name) ? `<input type="hidden" name="idempotency_key" value="${globalThis.crypto?.randomUUID?.() || `web-${Date.now()}-${Math.random().toString(36).slice(2)}`}">` : '';
  return `<form class="stack" data-form="${escapeHtml(name)}" data-value="${escapeHtml(value)}">${submission}<div class="form-grid">${fields}</div><p class="form-message" role="alert"></p><div class="actions"><button type="submit" class="button button-primary">${escapeHtml(button)}</button></div></form>`;
}

function field(name, title, value = '', { inputType = 'text', required = false, wide = false, attributes = '', placeholder = '' } = {}) {
  const input = `<input name="${name}" type="${inputType}" value="${escapeHtml(value)}"${required ? ' required' : ''}${attributes ? ` ${attributes}` : ''}${placeholder ? ` placeholder="${escapeHtml(placeholder)}"` : ''}>`;
  return `<label${wide ? ' class="wide"' : ''}>${escapeHtml(title)}${input}</label>`;
}

function selectField(name, title, values, selected, wide = false) {
  return `<label${wide ? ' class="wide"' : ''}>${escapeHtml(title)}<select name="${name}" aria-label="${escapeHtml(title)}">${options(values, selected)}</select></label>`;
}

function textField(name, title, value = '', attributes = '') {
  return `<label class="wide">${escapeHtml(title)}<textarea name="${name}"${attributes ? ` ${attributes}` : ''}>${escapeHtml(value)}</textarea></label>`;
}

function formAddSymbol() {
  const fields = field('symbol', '证券代码', '', { required: true, attributes: 'autofocus maxlength="16"', placeholder: '例如 HK.00700 或 00700' }) +
    field('name', '显示名称', '', { attributes: 'maxlength="80"', placeholder: '例如 腾讯控股' });
  showDialog('添加自选股', formFrame('watchlist', fields, '添加自选'));
}

function formAddOffer(platform = 'za') {
  const fields = selectField('platform', '平台', platforms, platform || 'za') +
    field('title', '活动名称', '', { required: true, attributes: 'autofocus maxlength="200"' }) +
    field('url', '官方活动链接', '', { inputType: 'url', wide: true, placeholder: 'https://' }) +
    field('ends_on', '活动结束日', '', { inputType: 'date' }) +
    field('deadline', '已查证截止时间（香港时间）', '', { inputType: 'datetime-local' }) +
    textField('text', '公告或活动条款', '', 'maxlength="50000" placeholder="可粘贴 App 公告的活动标题、参与条件和奖励条款。"');
  showDialog('录入平台活动', formFrame('offer', fields, '保存活动'));
}

function formAddLedger(selected = '') {
  const deals = { '': '其他' };
  for (const offer of state.offers) deals[offer.id] = `${platforms[offer.platform] || '平台活动'} · ${offer.title}`;
  const fields = selectField('offer_id', '关联活动', deals, selected, true) +
    selectField('kind', '记录类型', Object.fromEntries(Object.entries(ledgerKinds).map(([kind, [label]]) => [kind, label])), 'pending_cash') +
    field('amount', '原币金额（亏损填负数）', '', { inputType: 'number', required: true, attributes: 'autofocus step="0.01" inputmode="decimal"' }) +
    selectField('currency', '币种', currencies, 'HKD') +
    field('rate_hkd', '1 单位原币折合港币', 1, { inputType: 'number', required: true, attributes: 'min="0.000001" step="0.000001"' }) +
    field('occurred_on', '日期', formatDate(new Date()), { inputType: 'date', required: true }) +
    field('expected_on', '预计到账日（待到账项填写）', '', { inputType: 'date' }) +
    field('voucher_scope', '券用途 / 使用限制（券类填写）', '', { wide: true, attributes: 'maxlength="1500"' }) +
    field('redeemed_value', '券已实际抵扣的原币金额', '', { inputType: 'number', attributes: 'min="0" step="0.01"' }) +
    field('note', '备注', '', { wide: true, attributes: 'maxlength="500"' });
  showDialog('新增收益记录', `<p class="info-band">现金、费用和券填正数；交易或汇兑亏损填负数。港币汇率填 1，外币按记账时实际汇率填写。</p>${formFrame('ledger', fields, '保存记录')}`);
}

function formSettleLedger(id) {
  const entry = (state.ledger.entries || []).find((item) => String(item.id) === String(id));
  if (!entry) return;
  const kind = entry.kind === 'pending_voucher' ? 'voucher' : 'cash';
  const remaining = entry.remaining_amount ?? entry.amount;
  const fields = `<input type="hidden" name="kind" value="${kind}"><input type="hidden" name="currency" value="${escapeHtml(entry.currency || 'HKD')}">` +
    field('amount', `本次到账金额（${entry.currency || 'HKD'}）`, remaining, { inputType: 'number', required: true, attributes: `autofocus min="0.01" max="${Number(remaining)}" step="0.01" inputmode="decimal"` }) +
    field('rate_hkd', '到账时 1 单位原币折合港币', entry.rate_hkd || 1, { inputType: 'number', required: true, attributes: 'min="0.000001" step="0.000001"' }) +
    field('occurred_on', '到账日期', formatDate(new Date()), { inputType: 'date', required: true }) + (kind === 'voucher' ? field('voucher_scope', '券用途 / 使用限制', entry.voucher_scope, { wide: true, attributes: 'maxlength="1500"' }) + field('redeemed_value', '券已实际抵扣的原币金额', '', { inputType: 'number', attributes: 'min="0" step="0.01"' }) : '') + field('note', '到账备注', '', { wide: true, attributes: 'maxlength="1000"' });
  showDialog('登记本次到账', `<p class="info-band">原预计 ${currencyMoney(entry.amount, entry.currency)}，已兑现 ${currencyMoney(entry.settled_amount || 0, entry.currency)}，尚待 ${currencyMoney(remaining, entry.currency)}。本次到账将保留为独立流水，原预计记录继续保留。</p>${formFrame('settle-ledger', fields, '保存本次到账', id)}`);
}

function formVoucher(id) {
  const entry = (state.ledger.entries || []).find((item) => String(item.id) === String(id));
  if (!entry || entry.kind !== 'voucher') return;
  const fields = field('redeemed_value', `累计已抵扣金额（${entry.currency || 'HKD'}）`, entry.redeemed_value || 0, { inputType: 'number', required: true, attributes: `min="0" max="${Number(entry.amount)}" step="0.01" autofocus` }) + field('voucher_scope', '券用途 / 使用限制', entry.voucher_scope, { wide: true, attributes: 'maxlength="1500"' }) + field('note', '使用记录与备注', entry.note, { wide: true, attributes: 'maxlength="1200"' });
  showDialog('记录券使用', `<p class="info-band">券面值 ${currencyMoney(entry.amount, entry.currency)}。填写累计抵扣金额，系统保留原面值和变更记录。</p>${formFrame('voucher-use', fields, '保存使用记录', id)}`);
}

function localDateTime(value) {
  return value ? formatDate(value, true).replace(' ', 'T') : '';
}

function hongKongTimestamp(value) {
  if (typeof value !== 'string' || value === '') return '';
  return /^[\d-]{10}T[\d:]{5,8}$/.test(value) ? `${value}${value.length === 16 ? ':00' : ''}+08:00` : value;
}

function formatEvidence(value) {
  if (!value) return '';
  if (typeof value === 'string' || typeof value === 'number') return String(value);
  if (Array.isArray(value)) return value.map(formatEvidence).filter(Boolean).join('\n');
  return Object.entries(value).flatMap(([key, fieldValue]) => {
    const text = formatEvidence(fieldValue);
    return text ? [`${reviewFields[key] || key} · ${text}`] : [];
  }).join('\n');
}

function renderOfferDetail(offer) {
  const source = offer.source_kind === 'manual' ? '手动录入' : '官方来源';
  const body = escapeHtml(offer.text || '暂无条款正文，请打开官方页面或补充 App 公告。');
  const extractedFacts = formatEvidence(offer.facts);
  const evidence = formatEvidence(offer.evidence);
  const termsLinks = (offer.terms_urls || []).map((url, index) => externalLink(url, `官方条款 ${index + 1}`)).join('');
  const history = (offer.history || []).map((item) => `<details><summary>第 ${escapeHtml(item.version)} 版 · ${timeMarkup(item.created_at)}${item.deadline ? ` · 截止 ${timeMarkup(item.deadline)}` : item.ends_on ? ` · 结束 ${timeMarkup(item.ends_on, false)}` : ''}</summary><pre class="terms">${escapeHtml(item.text || item.title || '暂无正文')}</pre></details>`).join('');
  const review = offer.review || {};
  const fields = selectField('review_state', '我的参与资格', { pending: '待核对', eligible: '符合', ineligible: '不符合' }, offer.review_state || 'pending') +
    field('deadline', '已查证截止时间（香港时间）', localDateTime(offer.deadline), { inputType: 'datetime-local' }) +
    Object.entries(reviewFields).map(([key, label]) => key === 'note' ? textField(key, label, review[key], 'maxlength="4000"') : field(key, label, review[key], { attributes: 'maxlength="500"' })).join('');
  return `<div class="actions">${platformTag(offer)}${chip(source)}${offerStageChip(offer)}${statusChip(offer.review_state, reviewStates)}</div><div class="detail-meta"><span>${offerTime(offer)}</span><span>条款第 ${escapeHtml(offer.version)} 版 · 最近更新 ${timeMarkup(offer.updated_at || offer.seen_at)}</span></div><div class="actions">${action(offer.followed ? '取消关注' : '关注活动', 'toggle-follow', offer.id, offer.followed ? '' : 'button-primary')}${action('实操清单', 'personal-tutorial', offer.id)}${action('分享草稿', 'public-tutorial', offer.id)}${action('记入收益', 'offer-ledger', offer.id)}${externalLink(offer.url || offer.source_url, '官方页面')}</div>${requirementsSection(offer)}${tasksSection(offer)}${economicsSection(offer)}<section class="card"><h3>条款与证据</h3>${termsLinks ? `<div class="actions">${termsLinks}</div>` : ''}${extractedFacts ? `<p class="detail-help">自动提取，需与原条款核对。</p><pre class="terms">${escapeHtml(extractedFacts)}</pre>` : ''}${evidence ? `<details><summary class="detail-help">查看来源证据</summary><pre class="terms">${escapeHtml(evidence)}</pre></details>` : ''}<details${extractedFacts ? '' : ' open'}><summary class="detail-help">查看条款正文</summary><pre class="terms">${body}</pre></details></section>${reviewHistorySection(offer)}<section class="card"><h3>我的条款核对</h3>${formFrame('offer-review', fields, '保存核对', offer.id)}</section>${retrospectiveSection(offer)}${history ? `<section class="card history"><h3>官方版本记录</h3>${history}</section>` : ''}`;
}

async function openOffer(id) {
  const serial = ++state.dialogLoad;
  showDialog('活动详情', empty('正在读取活动条款…'));
  try {
    const offer = await api(`/api/offers/${encodeURIComponent(id)}`);
    if (state.dialogLoad !== serial || !dialog.open) return;
    state.currentOffer = offer;
    dialog.dataset.originalDeadline = localDateTime(offer.deadline);
    dialog.dataset.reviewedVersion = String(offer.version);
    showDialog(offer.title, renderOfferDetail(offer));
  } catch (error) {
    if (state.dialogLoad === serial && dialog.open) showDialog('活动详情', empty('活动详情读取失败', error.message));
  }
}

function renderTutorial(result, id, kind) {
  const isPublic = kind === 'public';
  const tool = ['ai', 'openrouter'].includes(result?.generated_by) ? 'AI 草稿' : '条款清单模板';
  const draftNote = isPublic ? '核对活动条款和截止时间后再发布。' : '对照官方条款与自己的账户资格后再操作。';
  showDialog(isPublic ? '分享草稿' : '实操清单', `<div class="actions">${chip(tool)}${action('返回活动', 'offer-detail', id)}${action('复制', 'copy-tutorial')}${action('下载 Markdown', 'download-tutorial')}</div><p class="info-band">${draftNote}</p><pre class="tutorial">${escapeHtml(result?.text || '暂无草稿内容。')}</pre>`);
}

async function openTutorial(id, kind, ai = false) {
  const serial = ++state.dialogLoad;
  const previous = state.tutorial;
  showDialog(kind === 'public' ? '分享草稿' : '实操清单', empty(ai ? '正在生成草稿…' : '正在读取教程…'));
  try {
    const path = `/api/offers/${encodeURIComponent(id)}/tutorial`;
    const result = ai ? await api(path, 'POST', { kind, ai: true }) : await api(`${path}?kind=${kind}`);
    if (state.dialogLoad !== serial || !dialog.open) return;
    state.tutorial = { ...result, id, kind };
    renderTutorial(result, id, kind);
  } catch (error) {
    if (state.dialogLoad !== serial || !dialog.open) return;
    if (previous?.id === id && previous.kind === kind) {
      renderTutorial(previous, id, kind);
    } else {
      showDialog('教程读取失败', `${empty('暂时无法读取教程', error.message)}${action('返回活动', 'offer-detail', id)}`);
    }
    showToast(error.message, true);
  }
}

function confirmDelete(title, message, formName, value) {
  showDialog(title, `<p class="info-band warn">${escapeHtml(message)}</p>${formFrame(formName, '', title, value)}`);
}

function valueMap(form) {
  return Object.fromEntries([...new FormData(form)].map(([key, value]) => [key, value.trim()]));
}

function offerPayload(fields) {
  const { platform, title, url, text, ends_on, deadline } = fields;
  if (!url && !text) throw new Error('请填官方链接，或粘贴活动条款。');
  return { platform, title, url, text, ...(ends_on && { ends_on }), ...(deadline && { deadline: hongKongTimestamp(deadline) }) };
}

function ledgerPayload(fields) {
  return { kind: fields.kind, amount: Number(fields.amount), occurred_on: fields.occurred_on, currency: fields.currency || 'HKD', rate_hkd: Number(fields.rate_hkd || 1), idempotency_key: fields.idempotency_key, note: fields.note, expected_on: fields.expected_on || null, voucher_scope: fields.voucher_scope || '', redeemed_value: optionalNumber(fields.redeemed_value) };
}

function optionalNumber(value) { return value === '' || value == null ? null : Number(value); }
function nullableBoolean(value) { return value === '' || value == null ? null : value === 'true'; }

async function saveForm(name, id, fields) {
  switch (name) {
    case 'watchlist': {
      const code = fields.symbol.toUpperCase();
      const numericCode = /^(?:HK\.)?(\d{1,5})$/.exec(code);
      if (!numericCode) throw new Error('请输入有效的港股代码，例如 00700。');
      const symbol = `HK.${numericCode[1].padStart(5, '0')}`;
      await api('/api/watchlist', 'POST', { symbol, name: fields.name || symbol });
      return '已添加自选股。';
    }
    case 'remove-symbol':
      await api(`/api/watchlist/${encodeURIComponent(id)}`, 'DELETE');
      return '已移出自选列表。';
    case 'offer':
      await api('/api/offers', 'POST', offerPayload(fields));
      return '已保存平台活动。';
    case 'account-remove': {
      const [platform, kind, entryId] = id.split(':');
      await api(`/api/account-data/${platform}/${kind}/${entryId}`, 'DELETE');
      return '已移除本机记录。';
    }
    case 'offer-review': {
      const review = Object.fromEntries(Object.keys(reviewFields).map((fieldName) => [fieldName, fields[fieldName]]));
      let deadline;
      if (fields.deadline !== dialog.dataset.originalDeadline) deadline = fields.deadline ? hongKongTimestamp(fields.deadline) : null;
      await api(`/api/offers/${encodeURIComponent(id)}`, 'PATCH', { review_state: fields.review_state, review, reviewed_version: Number(dialog.dataset.reviewedVersion), ...(deadline !== undefined && { deadline }) });
      return '已保存条款核对。';
    }
    case 'profile':
      await api(`/api/profiles/${encodeURIComponent(id)}`, 'PUT', { ...fields, invited: nullableBoolean(fields.invited), prior_owner: nullableBoolean(fields.prior_owner), available_capital: optionalNumber(fields.available_capital), rate_hkd: Number(fields.rate_hkd) });
      return '账户资料已保存，活动匹配已更新。';
    case 'budget':
      await api('/api/budget', 'PUT', { amount: Number(fields.amount), currency: fields.currency, rate_hkd: Number(fields.rate_hkd) });
      return '活动总预算已保存。';
    case 'requirements':
      await api(`/api/offers/${encodeURIComponent(id)}/requirements`, 'PUT', { reviewed: fields.reviewed === 'true', reviewed_version: Number(state.currentOffer?.version), source_url: fields.source_url, eligibility: { new_customer: nullableBoolean(fields.new_customer), invitation_required: nullableBoolean(fields.invitation_required), opened_after: fields.opened_after || null, opened_before: fields.opened_before || null }, capital_hkd: optionalNumber(fields.capital_hkd), holding_days: optionalNumber(fields.holding_days), notes: fields.notes });
      return '活动条件已保存并重新匹配。';
    case 'task': {
      const [offerId, taskId] = id.split(':');
      await api(`/api/offers/${encodeURIComponent(offerId)}/tasks`, 'POST', { id: Number(taskId), stage: fields.stage, title: fields.title, due_on: fields.due_on || null, completed: fields.completed === 'true', evidence: fields.evidence, prerequisite_ids: Object.keys(fields).filter((key) => key.startsWith('prerequisite_')).map((key) => Number(fields[key])) });
      return '任务记录已保存。';
    }
    case 'reservation':
      await api('/api/reservations', 'POST', { ...fields, ...(id && { id: Number(id) }), offer_id: Number(fields.offer_id), amount: Number(fields.amount), rate_hkd: Number(fields.rate_hkd) });
      return '资金安排已保存，冲突检查已更新。';
    case 'retrospective':
      await api(`/api/offers/${encodeURIComponent(id)}/retrospective`, 'PUT', { actual_minutes: Number(fields.actual_minutes), problems: fields.problems, shareable_notes: fields.shareable_notes, shareable_approved: fields.shareable_approved === 'true' });
      return '实操复盘已保存。';
    case 'alert':
      await api('/api/alerts', 'POST', { ...fields, ...(id && { id: Number(id) }), threshold: Number(fields.threshold), enabled: fields.enabled === 'true' });
      return '行情预警已保存。';
    case 'remove-alert':
      await api(`/api/alerts/${encodeURIComponent(id)}`, 'DELETE');
      return '行情预警已删除。';
    case 'notifications':
      await api('/api/notifications', 'PUT', { daily_digest: fields.daily_digest === 'true', digest_hour: Number(fields.digest_hour), urgent_changes: fields.urgent_changes === 'true', task_reminders: fields.task_reminders === 'true' });
      return '邮件提醒偏好已保存。';
    case 'ipo-cutoff':
      await api(`/api/ipos/${encodeURIComponent(id)}/cutoffs`, 'POST', { ...fields, deadline: hongKongTimestamp(fields.deadline), verified: fields.verified === 'true' });
      return '券商截止时间已保存。';
    case 'ledger':
      await api('/api/ledger', 'POST', { ...ledgerPayload(fields), note: fields.note, ...(fields.offer_id && { offer_id: fields.offer_id }) });
      return '收益记录已保存。';
    case 'settle-ledger':
      await api(`/api/ledger/${encodeURIComponent(id)}`, 'PATCH', ledgerPayload(fields));
      return '本次到账已登记，原预计记录已保留。';
    case 'voucher-use':
      await api(`/api/ledger/${encodeURIComponent(id)}/voucher`, 'PATCH', { redeemed_value: Number(fields.redeemed_value), voucher_scope: fields.voucher_scope, note: fields.note });
      return '券使用记录已保存。';
    case 'remove-ledger':
      await api(`/api/ledger/${encodeURIComponent(id)}`, 'DELETE');
      return '收益记录已撤销，汇总已更新。';
    default:
      throw new Error('无法识别表单。');
  }
}

async function submitForm(event) {
  const form = event.target.closest('form[data-form]');
  if (!form) return;
  event.preventDefault();
  const button = form.querySelector('button[type="submit"]');
  const message = form.querySelector('.form-message');
  if (button.disabled) return;
  button.disabled = true;
  message.textContent = '';
  const { form: name, value: id } = form.dataset;
  try {
    if (name === 'account-preview' || name === 'account-single') {
      const fields = valueMap(form);
      const content = name === 'account-preview' ? fields.csv : accountRecordCsv(id.split(':')[1], fields);
      await previewAccountImport(id, content);
      return;
    }
    const result = await saveForm(name, id, valueMap(form));
    showToast(result);
    if (dialog.open && form.isConnected) {
      if (['offer-review', 'requirements', 'retrospective'].includes(name)) void openOffer(id);
      else if (name === 'task') void openOffer(id.split(':')[0]);
      else if (name === 'account-remove') void openAccountData(id.split(':')[0]);
      else dialog.close();
    }
    void loadPage({ silent: true });
  } catch (error) {
    if (message.isConnected) message.textContent = error.message;
    else showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function refreshOfferFilter() {
  const results = view.querySelector('#offer-results');
  if (results) results.innerHTML = offerResults();
}

async function clipboardWrite(value) {
  if (navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // The selected textarea works for private HTTP endpoints too.
    }
  }
  const text = document.createElement('textarea');
  text.className = 'clipboard-target';
  text.value = value;
  text.readOnly = true;
  dialog.append(text);
  text.select();
  const copied = document.execCommand('copy');
  text.remove();
  if (!copied) throw new Error('复制未完成，请用下载 Markdown 保存。');
}

function saveTutorialDownload() {
  const draft = state.tutorial;
  if (!draft?.text) return;
  const file = new Blob([draft.text], { type: 'text/markdown;charset=utf-8' });
  const href = URL.createObjectURL(file);
  const link = document.createElement('a');
  link.href = href;
  link.download = draft.kind === 'public' ? '活动分享草稿.md' : '活动实操清单.md';
  link.click();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}

async function runAction(name, id, button) {
  switch (name) {
    case 'show-market':
      state.symbol = id;
      window.location.hash = 'market';
      break;
    case 'pick-quote':
      state.symbol = id;
      view.innerHTML = renderMarket();
      void loadHistory();
      view.querySelector('.chart-page')?.scrollIntoView({ block: 'nearest' });
      break;
    case 'add-symbol': formAddSymbol(); break;
    case 'remove-symbol': confirmDelete('移出自选', `将 ${quoteName(id)} 移出自选列表。`, 'remove-symbol', id); break;
    case 'add-offer': formAddOffer(id); break;
    case 'edit-profile': formProfile(id); break;
    case 'view-account-data':
      state.accountPreview = null;
      await openAccountData(id);
      break;
    case 'platform-offers':
      state.offerFilters = { query: '', platform: id, stage: 'all' };
      state.offerLimit = 12;
      window.location.hash = 'offers';
      break;
    case 'account-import':
      state.accountPreview = null;
      openAccountImport(id);
      break;
    case 'account-record': openAccountRecord(id); break;
    case 'account-remove-record': confirmDelete('移除账户记录', '这只会移除工作台中的该条记录。', 'account-remove', id); break;
    case 'account-edit-preview':
      if (state.accountPreview?.id === id) accountImportForm(id, state.accountPreview.content);
      break;
    case 'account-confirm': {
      const pending = state.accountPreview;
      if (pending?.id !== id) throw new Error('请重新预览资料。');
      const [platform, kind] = id.split(':');
      const result = await api(`/api/account-data/${platform}/${kind}/import`, 'POST', { csv: pending.content, digest: pending.digest });
      state.accountPreview = null;
      showToast(`已录入 ${result.inserted} 条${accountKinds[kind]}记录，${result.existing} 条已存在。`);
      if (dialog.open) await openAccountData(platform);
      break;
    }
    case 'edit-budget': formBudget(); break;
    case 'edit-task': await formTask(id); break;
    case 'add-reservation': formReservation(); break;
    case 'edit-reservation': formReservation(id); break;
    case 'offer-reservation':
      state.reservations = await api('/api/reservations');
      if (!state.offers.length) state.offers = await api('/api/offers');
      formReservation('', id);
      break;
    case 'initialize-tasks': {
      const current = await api(`/api/offers/${encodeURIComponent(id)}`);
      const tasks = current.tasks || [];
      let previous = null;
      for (const [stage, title] of Object.entries(taskStages)) {
        let task = tasks.find((item) => item.stage === stage);
        if (!task) task = await api(`/api/offers/${encodeURIComponent(id)}/tasks`, 'POST', { stage, title, completed: false, due_on: null, evidence: '', prerequisite_ids: previous ? [Number(previous.id)] : [] });
        previous = task;
      }
      showToast('六步任务已建立，请按实际条款填写日期和前置步骤。');
      await openOffer(id);
      void loadPage({ silent: true });
      break;
    }
    case 'add-alert': formAlert(); break;
    case 'edit-alert': formAlert(id); break;
    case 'remove-alert': confirmDelete('删除行情预警', '这条行情规则将停止提醒。', 'remove-alert', id); break;
    case 'ipo-cutoff': formIPOCutoff(id); break;
    case 'offer-detail': await openOffer(id); break;
    case 'show-more-offers': state.offerLimit += 12; refreshOfferFilter(); break;
    case 'add-ledger': formAddLedger(); break;
    case 'offer-ledger': formAddLedger(id); break;
    case 'settle-ledger': formSettleLedger(id); break;
    case 'voucher-use': formVoucher(id); break;
    case 'remove-ledger': confirmDelete('撤销收益记录', '撤销后重新计算汇总，原始记录留档。已有兑现记录的预计奖励须先撤销其关联到账。', 'remove-ledger', id); break;
    case 'toggle-follow': {
      const followed = button.textContent.trim() === '关注活动';
      await api(`/api/offers/${encodeURIComponent(id)}`, 'PATCH', { followed });
      showToast(followed ? '已关注活动。' : '已取消关注。');
      if (dialog.open && button.isConnected) void openOffer(id);
      void loadPage({ silent: true });
      break;
    }
    case 'personal-tutorial': await openTutorial(id, 'personal'); break;
    case 'public-tutorial': await openTutorial(id, 'public'); break;
    case 'generate-personal': await openTutorial(id, 'personal', true); break;
    case 'generate-public': await openTutorial(id, 'public', true); break;
    case 'copy-tutorial':
      if (state.tutorial?.text) {
        const feedback = dialog.querySelector('.dialog-feedback');
        try {
          await clipboardWrite(state.tutorial.text);
          feedback.textContent = '教程已复制。';
          feedback.classList.remove('warn');
        } catch (error) {
          feedback.textContent = error.message;
          feedback.classList.add('warn');
        }
        feedback.hidden = false;
      }
      break;
    case 'download-tutorial': saveTutorialDownload(); break;
    case 'queue-refresh':
      await api('/api/refresh', 'POST', { scope: id });
      showToast(id === 'quotes' ? '已开始更新公开行情快照。' : '已开始检查公开来源，稍后刷新查看结果。');
      setTimeout(() => { if (!dialog.open) void loadPage({ silent: true }); }, 4000);
      break;
    case 'read-notice':
      await api(`/api/notices/${encodeURIComponent(id)}/read`, 'POST', {});
      await loadPage({ silent: true });
      break;
    case 'read-all-notices':
      await api('/api/notices/read-all', 'POST', {});
      await loadPage({ silent: true });
      break;
    case 'test-email':
      await api('/api/email/test', 'POST', {});
      showToast('测试邮件已排队，投递后请查收邮箱。');
      setTimeout(() => { if (!dialog.open) void loadPage({ silent: true }); }, 2000);
      break;
    case 'reload': await loadPage(); break;
    case 'close-dialog': dialog.close(); break;
    default: break;
  }
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-action]');
  if (!button || button.disabled) return;
  button.disabled = true;
  try {
    await runAction(button.dataset.action, button.dataset.value, button);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

document.addEventListener('submit', submitForm);
document.addEventListener('input', (event) => {
  if (event.target.id !== 'offer-query') return;
  state.offerFilters.query = event.target.value;
  state.offerLimit = 12;
  refreshOfferFilter();
});
document.addEventListener('change', (event) => {
  const { id, value } = event.target;
  if (id === 'account-csv-file') {
    const file = event.target.files?.[0];
    const input = dialog.querySelector('#account-csv-text');
    if (!file || !input) return;
    if (file.size > 70000) {
      showToast('CSV 文件过大，请按资料日期分批导入。', true);
      event.target.value = '';
      return;
    }
    file.text().then((content) => { if (input.isConnected) input.value = content; }, () => showToast('未能读取 CSV 文件。', true));
    return;
  }
  if (event.target.name === 'currency') {
    const rate = event.target.closest('form')?.querySelector('[name="rate_hkd"]');
    if (rate) { rate.value = value === 'HKD' ? '1' : ''; rate.placeholder = value === 'HKD' ? '' : '填写实际折算汇率'; }
  }
  if (id === 'offer-platform' || id === 'offer-stage') {
    state.offerFilters[id === 'offer-platform' ? 'platform' : 'stage'] = value;
    state.offerLimit = 12;
    refreshOfferFilter();
  }
  if (id === 'chart-days' || id === 'chart-symbol') {
    if (id === 'chart-days') state.chartDays = Number(value);
    else state.symbol = value;
    view.innerHTML = pageRenderers[state.route]();
    view.querySelector(`#${id}`)?.focus({ preventScroll: true });
    void loadHistory();
  }
});
document.querySelector('#refresh-view').addEventListener('click', () => void loadPage({ silent: true }));
document.querySelector('.skip-link').addEventListener('click', (event) => {
  event.preventDefault();
  view.focus();
});
dialog.addEventListener('close', () => {
  state.dialogLoad += 1;
  state.accountPreview = null;
});
window.addEventListener('hashchange', () => void loadPage({ scroll: true }));

updateClock();
setInterval(updateClock, 1000);
setInterval(() => void refreshQuoteNumbers(), 30000);
setInterval(() => {
  if (document.hidden || dialog.open) return;
  if (['overview', 'market'].includes(state.route)) void loadHistory();
  if (['settings', 'overview'].includes(state.route) && !view.contains(document.activeElement)) void loadPage({ silent: true });
}, 60000);
void loadPage();
