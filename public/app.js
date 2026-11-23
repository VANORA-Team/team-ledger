'use strict';

const $ = (q, root = document) => root.querySelector(q);
const $$ = (q, root = document) => [...root.querySelectorAll(q)];
const hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

function randomId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = [...bytes].map(b => b.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  return 'id-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 14) + Math.random().toString(36).slice(2, 10);
}

const feishuEnv = (() => {
  const ua = typeof navigator !== 'undefined' ? (navigator.userAgent || '') : '';
  const isFeishu = /Lark|Feishu/i.test(ua) || typeof window.h5sdk !== 'undefined' || typeof window.tt !== 'undefined';
  const isMobile = /Android|iPhone|iPad|iPod|Mobile/i.test(ua);
  return {isFeishu, isMobile, isDesktop: isFeishu && !isMobile};
})();

if (feishuEnv.isFeishu) {
  document.documentElement.dataset.client = 'feishu';
  document.documentElement.dataset.feishuPlatform = feishuEnv.isMobile ? 'mobile' : 'desktop';
}

const paths = {
  book: 'M5 3h14v18H5z M9 7h6 M9 11h6 M9 15h3 M3 7h4 M3 12h4 M3 17h4',
  wallet: 'M3 6h17v14H3z M3 6l14-3v3 M15 11h6v5h-6z M17 13.5h1',
  calc: 'M5 3h14v18H5z M8 6h8v4H8z M8 14h1 M12 14h1 M16 14h1 M8 18h1 M12 18h1 M16 18h1',
  users: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8 M17 4a4 4 0 0 1 0 7 M22 21v-2a4 4 0 0 0-3-3.87',
  send: 'm22 2-7 20-4-9-9-4 20-7z M22 2 11 13',
  message: 'M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z',
  settings: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z',
  plus: 'M12 5v14 M5 12h14',
  down: 'M12 3v17 M5 13l7 7 7-7',
  arrow: 'M5 12h14 M13 6l6 6-6 6',
  arrowUpRight: 'M7 17 17 7 M8 7h9v9',
  chevronDown: 'm6 9 6 6 6-6',
  chevronRight: 'm9 18 6-6-6-6',
  close: 'M18 6 6 18 M6 6l12 12',
  refresh: 'M21 12a9 9 0 1 1-2.64-6.36 M21 3v6h-6',
  download: 'M12 3v12 M7 10l5 5 5-5 M4 17v3h16v-3',
  info: 'M12 8h.01 M12 11v6 M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20',
  spark: 'm12 2 2.4 6.6L21 11l-6.6 2.4L12 20l-2.4-6.6L3 11l6.6-2.4L12 2z',
  check: 'm5 13 4 4L19 7',
  copy: 'M9 9h10v10H9z M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1',
  search: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14 M21 21l-4.35-4.35',
  filter: 'M3 5h18 M6 12h12 M10 19h4',
  receipt: 'M5 2h14v20l-3-2-4 2-4-2-3 2z M8 7h8 M8 11h8 M8 15h5',
  edit: 'M12 20h9 M16.5 3.5a2.12 2.12 0 1 1 3 3L7 19l-4 1 1-4 12.5-12.5z',
  trash: 'M3 6h18 M8 6V4h8v2 M19 6l-1 14H6L5 6',
  card: 'M3 6h18v12H3z M3 10h18',
  move: 'M8 7 4 11l4 4 M16 7l4 4-4 4 M4 11h16',
  logout: 'M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4 M16 17l5-5-5-5 M21 12H9',
  user: 'M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2 M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8',
  shield: 'M12 2 4 5v6c0 5.25 3.4 10.1 8 11 4.6-.9 8-5.75 8-11V5l-8-3z',
  database: 'M12 3c4.97 0 9 1.34 9 3s-4.03 3-9 3-9-1.34-9-3 4.03-3 9-3z M3 6v6c0 1.66 4.03 3 9 3s9-1.34 9-3V6 M3 12v6c0 1.66 4.03 3 9 3s9-1.34 9-3v-6',
  sun: 'M12 8a4 4 0 1 0 0 8 4 4 0 1 0 0-8 M12 2v2 M12 20v2 M4.93 4.93l1.41 1.41 M17.66 17.66l1.41 1.41 M2 12h2 M20 12h2 M6.34 17.66l-1.41 1.41 M19.07 4.93l-1.41 1.41',
  moon: 'M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9z',
  monitor: 'M4 3h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z M8 21h8 M12 17v4',
};

const icon = (name, cls = '') =>
  `<svg class="icon${cls ? ' ' + cls : ''}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.book}"/></svg>`;

const PAGES = {
  overview: ['总览', 'overview'],
  ledger: ['收支明细', 'ledger'],
  reimbursements: ['报销与资金', 'reimbursements'],
  settings: ['设置', 'settings'],
};

const STATUS = {draft: '草稿', pending: '待报销', partial: '部分报销', reimbursed: '已报销', posted: '已入账', voided: '已作废'};
const entryStatus = e => e.status === 'pending' && e.reimbursed_minor > 0 ? 'partial' : e.status;
const entryType = e => e.kind === 'income' ? '团队收入' : e.expense_type === 'public' ? '公账支出' : '成员垫付';
const entryDescription = e => e.description || '未填写说明';
const ROLES = {owner: '账本所有者', finance: '财务管理员', member: '普通成员'};
const roleName = role => ROLES[role] || '未识别角色';
const COLORS = ['green', 'orange', 'blue', 'purple'];

const AUTH_ERRORS = {
  oidc_unconfigured: '服务器尚未配置 Vanora OAuth，请联系管理员完成环境变量配置。',
  cancelled: '你已取消 Vanora 授权登录，请重试。',
  invalid_state: '登录状态已过期或校验失败，请重新点击登录。',
  exchange_failed: '与 Vanora OAuth 交换令牌失败，请稍后重试或检查网络配置。',
  invalid_token: 'Vanora 身份令牌校验失败，请重新登录。',
  disabled: '你的 Vanora 账号已被停用，请联系管理员在 Vanora 管理后台检查账号状态。',
  not_member: '当前 Vanora 账号尚未被分配本团队账本的客户端角色（owner / finance / member），请联系管理员在 Vanora 管理后台授权后再登录。',
};
const RETURN_HASH_RE = /^#[a-z]{1,30}(?:\/[a-z0-9-]{1,30})?$/;

const state = {
  user: null, csrf: '', accountUrl: '', adminUrl: '', boot: null, authError: '',
  conf: null, members: [], entries: [], integrations: {}, report: null, page: 'overview',
  pendingEntryId: null,
  filter: {text: '', kind: 'all', currency: 'all', payer: 'all', status: 'all', from: '', to: ''},
  filterOpen: false,
  reimburseMode: 'queue',
  reimburseTab: 'pending',
  settingsTab: 'members',
  selected: new Set(), entryKind: 'expense', editing: null, entryKey: null,
  action: null, base: null, loadId: 0,
  exportState: {ids: [], source: 'overview', activePreset: 'all'},
  calculator: {mode: 'auto', budget: '', budgetBase: null, ids: new Set(), amount: '', currency: null, report: null, request: 0},
  ai: {busy: false, conversation: [], plan: null, selected: new Set(), error: ''},
};

const isOwner = () => state.user?.role === 'owner';
const canManage = () => isOwner() || state.user?.role === 'finance';
const canManageEntry = entry => canManage() ||
  (entry.kind === 'expense' && entry.expense_type === 'advance' && entry.payer_id === state.user?.id);
const cur = code => state.conf?.currencies.find(c => c.code === code) || {code, symbol: code, decimals: 2};

const money = (amount, code, withCode = true) => {
  const c = cur(code);
  const num = Number(amount);
  if (!Number.isFinite(num)) return '—';
  return `${esc(c.symbol)}${num.toLocaleString('zh-CN', {minimumFractionDigits: c.decimals, maximumFractionDigits: c.decimals})}${withCode ? ` <span class="money-code">${esc(code)}</span>` : ''}`;
};

const badge = status => `<span class="badge ${esc(status)}">${esc(STATUS[status] || status)}</span>`;
const avatar = (name, id = 0) => `<span class="avatar ${COLORS[Math.abs(Number(id)) % COLORS.length]}">${esc([...String(name || 'T')][0] || 'T')}</span>`;
const options = (items, value, label, selected) =>
  items.map(item => `<option value="${esc(value(item))}" ${String(value(item)) === String(selected) ? 'selected' : ''}>${esc(label(item))}</option>`).join('');
const currencyOptions = selected => options(state.conf.currencies, c => c.code, c => `${c.code} · ${c.name}`, selected);

function copyButtonHtml(text, label = '复制') {
  return `<button type="button" class="copy-label" data-copy-text="${esc(text)}" data-copied="false" aria-label="${esc(label)}"><span class="copy-icon-stack" aria-hidden="true"><svg class="copy-icon copy-icon-default" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round"><path d="${paths.copy}"/></svg><svg class="copy-icon copy-icon-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="${paths.check}"/></svg></span><span class="copy-label-text"><span class="copy-label-default">${esc(label)}</span><span class="copy-label-done" aria-hidden="true">已复制</span></span></button>`;
}

async function copyText(text, button) {
  let ok = false;
  if (window.tt && typeof window.tt.setClipboardData === 'function') {
    ok = await new Promise(resolve => {
      try {
        window.tt.setClipboardData({data: text, success: () => resolve(true), fail: () => resolve(false)});
      } catch {
        resolve(false);
      }
    });
  }
  if (!ok && navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
    try {
      await navigator.clipboard.writeText(text);
      ok = true;
    } catch {
      ok = false;
    }
  }
  if (!ok) {
    try {
      const area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.className = 'sr-only-copy';
      document.body.append(area);
      area.select();
      ok = document.execCommand('copy');
      area.remove();
    } catch {
      ok = false;
    }
  }
  if (!ok) {
    toast('当前浏览器不允许自动复制，请长按或手动选中复制。', true);
    return;
  }
  if (button) {
    const defEl = $('.copy-label-default', button);
    const doneEl = $('.copy-label-done', button);
    const origLabel = button.dataset.copyOrigLabel || button.getAttribute('aria-label') || '复制';
    button.dataset.copyOrigLabel = origLabel;
    button.dataset.copied = 'true';
    button.setAttribute('aria-label', '已复制');
    if (defEl) defEl.setAttribute('aria-hidden', 'true');
    if (doneEl) doneEl.removeAttribute('aria-hidden');
    clearTimeout(button._copyTimer);
    button._copyTimer = setTimeout(() => {
      button.dataset.copied = 'false';
      button.setAttribute('aria-label', origLabel);
      if (defEl) defEl.removeAttribute('aria-hidden');
      if (doneEl) doneEl.setAttribute('aria-hidden', 'true');
    }, 2200);
  }
  toast('已复制到剪贴板');
}

function triggerDownload(url, filename = '') {
  if (feishuEnv.isFeishu && feishuEnv.isMobile && window.tt && typeof window.tt.downloadFile === 'function') {
    const fullUrl = new URL(url, location.origin).href;
    window.tt.downloadFile({
      url: fullUrl,
      success: res => {
        if (res?.tempFilePath && typeof window.tt.openDocument === 'function') {
          const openOpts = {filePath: res.tempFilePath, showMenu: true};
          if (String(filename).toLowerCase().endsWith('.xlsx')) openOpts.fileType = 'xlsx';
          window.tt.openDocument(openOpts);
        } else {
          toast('文件已下载');
        }
      },
      fail: () => {
        const link = document.createElement('a');
        link.href = url;
        if (filename) link.download = filename;
        link.target = '_blank';
        link.rel = 'noreferrer';
        document.body.append(link);
        link.click();
        link.remove();
        toast('如飞书内置浏览器拦截下载，请点击右上角在系统浏览器中打开。');
      },
    });
    return;
  }
  const link = document.createElement('a');
  link.href = url;
  if (filename) link.download = filename;
  if (feishuEnv.isFeishu && feishuEnv.isMobile) {
    link.target = '_blank';
    link.rel = 'noreferrer';
  }
  document.body.append(link);
  link.click();
  link.remove();
  if (feishuEnv.isFeishu && feishuEnv.isMobile) {
    toast('已发起下载；如未开始，请点击右上角「在浏览器中打开」。');
  }
}

/* Custom Vanora Confirm Dialog (zero native confirm/prompt) */
let confirmResolver = null;
function uiConfirm({title = '请确认', message = '', confirmLabel = '确认', danger = false, inputLabel = '', inputPlaceholder = '', inputType = 'text'} = {}) {
  return new Promise(resolve => {
    const dialog = $('#confirm-dialog');
    const okBtn = $('#confirm-ok');
    const inputWrap = $('#confirm-input-wrap');
    const inputEl = $('#confirm-input');
    $('#confirm-title').textContent = title;
    $('#confirm-message').textContent = message;
    $('#confirm-error').textContent = '';
    okBtn.textContent = confirmLabel;
    okBtn.className = `button ${danger ? 'button-danger' : 'button-primary'}`;
    if (inputLabel) {
      $('#confirm-input-label').textContent = inputLabel;
      inputEl.type = inputType;
      inputEl.placeholder = inputPlaceholder;
      inputEl.value = '';
      inputEl.required = true;
      inputWrap.hidden = false;
    } else {
      inputEl.value = '';
      inputEl.required = false;
      inputWrap.hidden = true;
    }
    confirmResolver = {resolve, hasInput: Boolean(inputLabel)};
    if (!dialog.open) dialog.showModal();
    setTimeout(() => (inputLabel ? inputEl : okBtn).focus(), 40);
  });
}

function resolveConfirm(value) {
  const pending = confirmResolver;
  confirmResolver = null;
  const dialog = $('#confirm-dialog');
  if (dialog?.open) dialog.close();
  if (pending) pending.resolve(value);
}

$('#confirm-form').addEventListener('submit', event => {
  event.preventDefault();
  if (!confirmResolver) return;
  if (confirmResolver.hasInput) {
    const value = $('#confirm-input').value;
    if (!value) {
      $('#confirm-error').textContent = '内容不能为空';
      return;
    }
    resolveConfirm(value);
  } else {
    resolveConfirm(true);
  }
});

$('#confirm-dialog').addEventListener('cancel', event => {
  event.preventDefault();
  resolveConfirm(confirmResolver?.hasInput ? null : false);
});

async function api(path, method = 'GET', body, extraHeaders = {}) {
  const headers = {...extraHeaders};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (method !== 'GET' && state.csrf) headers['X-CSRF-Token'] = state.csrf;
  const response = await fetch('/api' + path, {method, headers, credentials: 'same-origin', body: body === undefined ? undefined : JSON.stringify(body)});
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error('服务器返回异常，请稍后重试');
  }
  if (!response.ok) {
    if (response.status === 401 && state.user) {
      state.user = null;
      state.csrf = '';
      $$('dialog[open]').forEach(d => d.close());
      await showAuth();
    }
    const error = new Error(typeof data.error === 'string' ? data.error : response.status === 422 ? '输入格式不正确，请检查表单' : '操作失败，请稍后重试');
    error.status = response.status;
    throw error;
  }
  return data;
}

let toastTimer;
function toast(message, error = false) {
  const element = $('#toast');
  const textEl = $('#toast-text');
  if (!element) return;
  if (textEl) textEl.textContent = message;
  else element.textContent = message;
  element.classList.toggle('error', error);
  element.classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.classList.remove('visible'); }, error ? 6500 : 3500);
}

async function busy(button, fn) {
  if (!button || button.disabled) return;
  button.disabled = true;
  try {
    return await fn();
  } finally {
    button.disabled = false;
  }
}

function empty(title = '暂无记录', description = '开始记下团队的第一笔收支吧。', action = '') {
  return `<div class="empty"><div class="empty-message" role="status">${icon('book')}<h3>${esc(title)}</h3><p>${esc(description)}</p></div>${action}</div>`;
}

function heading(title, actions = '', extra = '') {
  return `<div class="page-heading"><div class="page-heading-lead"><h1>${esc(title)}</h1>${extra}</div>${actions ? `<div class="heading-actions">${actions}</div>` : ''}</div>`;
}

function newButton(kind = 'expense', text = '记一笔') {
  return `<button type="button" class="button button-primary" data-new="${kind}">${icon('plus', 'ico-plus')}${esc(text)}</button>`;
}

function baseSelect() {
  return `<select id="report-base" aria-label="汇总参考币种">${currencyOptions(state.base || state.conf.reference_currency)}</select>`;
}

function groupedTotals(entries, field = 'amount') {
  const sums = {};
  for (const e of entries) sums[e.currency] = (sums[e.currency] || 0) + Number(e[field]);
  return Object.entries(sums).map(([code, amount]) => money(amount, code)).join(' <span class="muted">/</span> ') || '—';
}

function paymentSummary(e) {
  if (e.kind !== 'expense' || !e.reimbursed_minor) return '';
  return `<span class="payment-progress">已报 ${money(e.reimbursed_amount, e.currency)} · 剩余 ${money(e.remaining_amount, e.currency)}${settlementDisplay({...e, amount: e.remaining_amount})}</span>`;
}

function paymentHistory(e) {
  if (!e.payments?.length) return '';
  return `<section class="drawer-card"><h4>报销付款记录</h4><div class="timeline">${e.payments.map(p => `<div class="timeline-item"><strong>实际付款 ${money(p.amount, p.currency)}</strong> · 抵扣 ${money(p.entry_amount, e.currency)}${p.currency !== e.currency ? `<br>汇率 1 ${esc(e.currency)} = ${esc(p.fx_rate)} ${esc(p.currency)} · ${esc(p.fx_date)}` : ''}<br>${esc(p.name)} · ${esc(p.note)}<small>${esc(p.created_at.replace('T', ' ').slice(0, 19))}</small></div>`).join('')}</div></section>`;
}

function settlementDisplay(entry, report = state.report, compact = false) {
  const base = report?.base;
  const rate = report?.fx?.rates?.[entry.currency];
  if (!base || entry.currency === base || !rate) return '';
  const convertedAmount = Number(entry.amount) * Number(rate.rate);
  if (!Number.isFinite(convertedAmount)) return '';
  if (compact) return `<span class="settlement-line">≈ ${money(convertedAmount, base)}</span>`;
  return `<span class="settlement-line">≈ ${money(convertedAmount, base)} <small>参考${rate.date ? ` · ${esc(rate.date)}` : ''}</small></span>`;
}

function compactAmountSubline(e) {
  if (e.kind === 'expense' && e.reimbursed_minor > 0) {
    return `<span class="payment-progress">剩余 ${money(e.remaining_amount, e.currency, false)} ${esc(e.currency)}</span>`;
  }
  return settlementDisplay(e, state.report, true);
}

function receiptLink(entry, label = '凭证') {
  return `<a class="tiny-link" href="/api/entries/${entry.id}/receipt" data-receipt="${entry.id}">${esc(label)}</a>`;
}

function sourceLabel(source) {
  return ({web: 'Web', telegram: 'Telegram', feishu: '飞书', ai: 'AI', migration: '迁移'})[source] || source;
}

function entryKindDot(entry) {
  const cls = entry.kind === 'income' ? 'income' : entry.expense_type === 'public' ? 'public' : 'advance';
  return `<span class="kind-dot ${cls}" aria-hidden="true"></span>`;
}

function table(entries, {checkbox = false, label = '账目列表', emptyCta = true} = {}) {
  if (!entries.length) {
    const cta = emptyCta ? `<button type="button" class="empty-action-btn" data-new="expense">记一笔 ${icon('arrow', 'icon-sm')}</button>` : '';
    return empty('暂无符合条件的账目', '调整筛选条件，或记下新的一笔。', cta);
  }
  return `<p class="table-scroll-hint" aria-hidden="true">${icon('move', 'icon-sm')}左右滑动查看完整列表</p><div class="table-wrap" role="region" aria-label="${esc(label)}" tabindex="0"><table class="data-table"><thead><tr>${checkbox ? '<th class="check-cell"><input type="checkbox" id="select-all" aria-label="选择本页全部账目"></th>' : ''}<th>用途 / 说明</th><th>成员</th><th>日期</th><th class="money">金额</th><th>状态</th><th class="row-chevron-col"></th></tr></thead><tbody>${entries.map(e => `<tr data-entry="${e.id}" tabindex="0" aria-label="查看 ${esc(entryDescription(e))}">${checkbox ? `<td class="check-cell"><input type="checkbox" data-select="${e.id}" ${state.selected.has(e.id) ? 'checked' : ''} aria-label="选择 ${esc(entryDescription(e))}"></td>` : ''}<td><div class="entry-title-cell"><strong>${esc(entryDescription(e))}</strong><span class="subline entry-meta-line">${entryKindDot(e)}<span>${entryType(e)}</span>${e.receipt ? '<span class="meta-sep">·</span>' + receiptLink(e) : ''}</span></div></td><td>${esc(e.payer_name)}</td><td class="mono muted-cell">${esc(e.date)}</td><td class="money ${e.kind === 'income' ? 'income' : ''}"><div class="amount-primary"><strong>${e.kind === 'income' ? '+' : '−'} ${money(e.amount, e.currency)}</strong></div>${compactAmountSubline(e)}</td><td>${badge(entryStatus(e))}</td><td class="row-chevron">${icon('chevronRight', 'icon-sm')}</td></tr>`).join('')}</tbody></table></div>`;
}

function reportWarning(report = state.report) {
  if (!report) return `<div class="callout warning">${icon('info')}<span class="callout-body">资金汇总暂不可用，请刷新重试。原币种记录不受影响。</span></div>`;
  if (report.fx.warning) return `<div class="callout ${report.complete ? 'warning' : 'error'}">${icon('info')}<span class="callout-body">${esc(report.fx.warning)}${report.fx.missing.length ? ' 缺少币种：' + esc(report.fx.missing.join('、')) : ''}</span></div>`;
  return '';
}

/* ============================================================================
 * AI Drawer Logic
 * ========================================================================== */

const AI_LABELS = {create_entry: '新增账目', edit_entry: '修改账目', void_entry: '作废账目', submit_entry: '草稿入账', pay_entry: '登记报销', batch_pay: '批量报销', delete_entry: '永久删除'};
const AI_ICON_NAMES = {create_entry: 'receipt', edit_entry: 'edit', void_entry: 'trash', submit_entry: 'check', pay_entry: 'card', batch_pay: 'wallet', delete_entry: 'trash'};
const AI_STATUS = {draft: '草稿', pending: '待报销', posted: '已入账', reimbursed: '已报销', voided: '已作废'};
const AI_EVENT_NAMES = {created: '创建', edited: '修改', submit: '记入账本', approve: '历史：通过', reject: '历史：退回', pay: '登记付款', void: '作废', migrated: '历史支出迁移', receipt: '上传凭证'};

const aiSelected = () => state.ai.plan ? state.ai.plan.actions.filter(a => !a.errors?.length && state.ai.selected.has(a.index)).length : 0;

function aiMessage(role, text) {
  state.ai.conversation.push({role, text: String(text)});
  if (state.ai.conversation.length > 24) state.ai.conversation = state.ai.conversation.slice(-24);
}

function aiHistory() {
  const turns = state.ai.conversation.slice(-10).map(m => ({role: m.role, text: String(m.text).slice(0, 900)}));
  if (state.ai.plan?.actions?.length) {
    const summary = state.ai.plan.actions.map((a, i) => `${i + 1}. ${a.summary}`).join('\n');
    if (summary) turns.push({role: 'assistant', text: '当前操作清单：\n' + summary.slice(0, 900)});
  }
  if (state.ai.plan?.results?.length) {
    const summary = state.ai.plan.results.map(r => aiQueryText(r.card)).filter(Boolean).join('\n');
    if (summary) turns.push({role: 'assistant', text: '上一轮查询结果：\n' + summary.slice(0, 900)});
  }
  return turns;
}

function aiFieldRows(action) {
  const r = action.resolved || {}, rows = [];
  const add = (label, value) => { if (value !== undefined && value !== null && value !== '') rows.push(`<span><b>${esc(label)}</b> ${esc(value)}</span>`); };
  if (action.type === 'create_entry') {
    add('类型', entryType(r)); add('金额', `${r.amount} ${r.currency}`); add('用途', entryDescription(r));
    add('日期', r.date); add('成员', r.payer_name || r.payer_id); add('状态', AI_STATUS[r.status] || r.status); add('备注', r.note);
  } else if (action.type === 'edit_entry') {
    add('编号', `#${r.id}`); add('金额', `${r.amount} ${r.currency}`); add('用途', entryDescription(r)); add('日期', r.date); add('备注', r.note);
  } else if (['void_entry', 'submit_entry'].includes(action.type)) {
    add('编号', `#${r.id}`); add('备注', r.note);
  } else if (action.type === 'pay_entry') {
    add('编号', `#${r.id}`); add('本次付款', `${r.amount} ${r.currency}`); add('抵扣', `${r.entry_amount} ${r.entry_currency}`);
    add('登记后剩余', `${r.remaining_amount} ${r.entry_currency}`); add('付款备注', r.note);
  } else if (action.type === 'batch_pay') {
    add('付款币种', r.mode === 'settlement' ? '团队结算货币' : '各笔记账货币');
    add('限定成员', r.payer_name || (r.payer_id ? `#${r.payer_id}` : ''));
    add('报销预算', r.budget !== null && r.budget !== undefined ? `${r.budget} ${r.base}` : '');
    add('日期范围', [r.date_from, r.date_to].filter(Boolean).join(' 至 '));
    add('付款备注', r.note);
  } else if (action.type === 'delete_entry') {
    add('编号', `#${r.id}`); add('说明', entryDescription(r));
  }
  return rows.length ? `<div class="ai-fields">${rows.join('')}</div>` : '';
}

function aiActionCard(action, position) {
  const errors = action.errors || [], warnings = action.warnings || [], checked = state.ai.selected.has(action.index), disabled = Boolean(errors.length);
  const batch = action.type === 'batch_pay' && action.resolved?.items?.length
    ? `<div class="ai-batch-items">${action.resolved.items.slice(0, 12).map(item => `<div>#${esc(item.id)} ${esc(entryDescription(item))} · 实际付款 ${esc(item.amount)} ${esc(item.currency)} · 抵扣 ${esc(item.credited)} ${esc(item.entry_currency)}</div>`).join('')}${action.resolved.items.length > 12 ? `<div>另有 ${action.resolved.items.length - 12} 笔，确认后一次登记。</div>` : ''}</div>`
    : '';
  return `<article class="ai-action-card ${disabled ? 'has-error' : ''}"><label class="ai-action-check"><input type="checkbox" data-ai-select="${action.index}" ${checked ? 'checked' : ''} ${disabled ? 'disabled' : ''} aria-label="选择第 ${position} 项操作"></label><div class="ai-action-body"><div class="ai-action-head"><span class="ai-action-icon">${icon(AI_ICON_NAMES[action.type] || 'receipt')}</span><strong>${esc(action.label || AI_LABELS[action.type] || action.type)}</strong></div><p class="ai-action-summary">${esc(action.summary)}</p>${errors.length ? `<div class="ai-action-error">${errors.map(esc).join('<br>')}</div>` : ''}${warnings.length ? `<div class="ai-action-warning">${warnings.map(esc).join('<br>')}</div>` : ''}${aiFieldRows(action)}${batch}${action.type === 'create_entry' && !disabled ? `<button type="button" class="button button-outline button-small" data-ai-edit-form="${action.index}">在表单中修改</button>` : ''}</div></article>`;
}

function aiQueryText(card) {
  if (!card) return '';
  if (card.kind === 'entries') return `查到 ${card.total} 笔账目`;
  if (card.kind === 'summary') return `统计共 ${card.count} 笔`;
  if (card.kind === 'balance' || card.kind === 'due' || card.kind === 'funds') return card.complete ? `可用 ${card.cash} ${card.base} · 待报销 ${card.due} ${card.base}` : (card.warning || '资金数据暂不可用');
  if (card.kind === 'simulation') return card.complete ? `模拟进账后可用 ${card.cash} ${card.base}` : (card.warning || '模拟暂不可用');
  if (card.kind === 'entry') return `#${card.entry.id} ${entryDescription(card.entry)}`;
  if (card.kind === 'error') return card.error;
  return '';
}

function aiQueryCard(card) {
  if (!card) return '';
  if (card.kind === 'entries') {
    const totals = Object.entries(card.totals || {}).map(([code, amount]) => money(amount, code)).join(' / ') || '—';
    return `<div class="ai-query-card"><div class="ai-query-head">查询结果 <span>共 ${esc(card.total)} 笔 · ${totals}</span></div>${table(card.entries || [], {emptyCta: false})}</div>`;
  }
  if (card.kind === 'summary') {
    const slots = [...(card.by_kind?.expense || []).map(x => ({...x, kind: 'expense'})), ...(card.by_kind?.income || []).map(x => ({...x, kind: 'income'}))];
    return `<div class="ai-query-card"><div class="ai-query-head">统计汇总 <span>${esc(card.count)} 笔${card.from ? ` · ${esc(card.from)}${card.to ? ` 至 ${esc(card.to)}` : ''}` : ''}</span></div><div class="ai-metrics">${slots.length ? slots.map(x => `<span>${x.kind === 'income' ? '收入' : '支出'} ${money(x.amount, x.currency)} · ${esc(x.count)} 笔</span>`).join('') : '<span>没有符合条件的数据</span>'}</div></div>`;
  }
  if (['balance', 'due', 'funds'].includes(card.kind)) {
    if (!card.complete) return `<div class="ai-query-card warn">${esc(card.warning || '资金数据暂不可用')}</div>`;
    const base = card.base;
    return `<div class="ai-query-card"><div class="ai-query-head">资金概览 <span class="mono">${esc(base)}</span></div><div class="ai-metrics"><span>可用 ${money(card.cash, base)}</span><span>在账收入 ${money(card.income, base)}</span><span>已报销 ${money(card.paid, base)}</span><span>公账支出 ${money(card.public_paid, base)}</span><span>待报销 ${money(card.due, base)}</span><span>可安排 ${money(card.payable, base)}</span><span>缺口 ${money(card.shortage, base)}</span></div>${card.members?.length ? `<div class="ai-batch-items">${card.members.filter(m => Number(m.due) > 0).slice(0, 8).map(m => `<div>${esc(m.name)} · 待报销 ${money(m.due, base)} · 建议 ${money(m.suggested, base)}</div>`).join('')}</div>` : ''}${card.warning ? `<div class="ai-action-warning">${esc(card.warning)}</div>` : ''}</div>`;
  }
  if (card.kind === 'simulation') {
    if (!card.complete) return `<div class="ai-query-card warn">${esc(card.warning || '模拟暂不可用')}</div>`;
    const base = card.base;
    return `<div class="ai-query-card"><div class="ai-query-head">模拟进账 <span>试算</span></div><div class="ai-metrics"><span>模拟后可用 ${money(card.cash, base)}</span><span>待报销 ${money(card.due, base)}</span><span>可安排 ${money(card.payable, base)}</span><span>缺口 ${money(card.shortage, base)}</span></div>${card.warning ? `<div class="ai-action-warning">${esc(card.warning)}</div>` : ''}</div>`;
  }
  if (card.kind === 'entry') {
    const e = card.entry;
    const payments = (e.payments || []).map(p => `<div>${esc(p.created_at.slice(0, 10))} · ${money(p.amount, p.currency)} · 抵扣 ${money(p.entry_amount, e.currency)}</div>`).join('');
    const events = (card.events || []).slice(-5).map(ev => `<div>${esc(ev.created_at.replace('T', ' ').slice(0, 16))} · ${esc(ev.name)} · ${esc(AI_EVENT_NAMES[ev.action] || ev.action)}${ev.detail ? ' · ' + esc(ev.detail) : ''}</div>`).join('');
    return `<div class="ai-query-card"><div class="ai-query-head">账目 #${esc(e.id)} <span>${badge(entryStatus(e))}</span></div><div class="ai-metrics"><span>${esc(entryDescription(e))}</span><span>${money(e.amount, e.currency)}</span><span>${esc(e.payer_name)}</span><span>${esc(e.date)}</span>${e.kind === 'expense' && e.reimbursed_minor > 0 ? `<span>剩余 ${money(e.remaining_amount, e.currency)}</span>` : ''}</div>${payments ? `<div class="ai-batch-items">${payments}</div>` : ''}${events ? `<div class="ai-batch-items">${events}</div>` : ''}</div>`;
  }
  if (card.kind === 'error') return `<div class="ai-query-card error">${esc(card.error)}</div>`;
  return '';
}

function renderAiPlan() {
  const plan = state.ai.plan;
  if (!plan) return '';
  const actions = plan.actions || [], results = plan.results || [], valid = actions.filter(a => !a.errors?.length), count = aiSelected();
  let html = '<section class="ai-plan">';
  if (results.length) html += `<div class="ai-results">${results.map(r => aiQueryCard(r.card)).join('')}</div>`;
  if (actions.length) {
    html += `<div class="ai-plan-head"><strong>待确认操作</strong><span id="ai-plan-count">${actions.length} 项 · 已选 ${count} 项</span></div>`;
    html += actions.map((action, position) => aiActionCard(action, position + 1)).join('');
  }
  if (plan.plan_id) {
    html += `<div class="ai-plan-footer"><span id="ai-selected-count">已选 ${count} / ${valid.length} 项</span><button type="button" class="button button-outline" data-ai-cancel-plan>取消</button><button type="button" class="button button-primary" data-ai-confirm ${count ? '' : 'disabled'}>确认执行 ${count} 项</button></div>`;
  } else if (actions.length) {
    html += '<p class="ai-plan-note">暂无可执行操作，请按提示补充描述。</p>';
  }
  html += '</section>';
  return html;
}

function renderAi() {
  const transcript = $('#ai-transcript');
  if (!transcript) return;
  const aiCfg = state.integrations?.ai;
  const disabledInWeb = Boolean(aiCfg && !aiCfg.enabled && (aiCfg.has_api_key || !isOwner()));
  const unconfiguredHint = !state.integrations?.ai_configured
    ? `<div class="callout warning">${icon('info')}<div class="callout-body">${isOwner()
      ? (disabledInWeb ? 'AI 记账引擎已停用，可在设置中重新启用。' : '尚未配置 AI 接口，请在设置中填写接口地址、密钥与模型。')
      : (disabledInWeb ? 'AI 记账引擎已停用，请联系账本所有者启用。' : '尚未配置 AI 接口，请联系账本所有者配置。')}${isOwner() ? `<div class="ai-welcome-actions"><a href="#settings/bots-ai" class="console-inline-link" data-close>前往配置 ${icon('arrowUpRight', 'icon-sm')}</a></div>` : ''}</div></div>`
    : '';
  const welcome = !state.ai.conversation.length && !state.ai.plan
    ? `${unconfiguredHint}<div class="ai-welcome"><strong>自然语言记账与查账</strong>支持批量新增、修改、作废、报销和查账，写入前会生成清单供核对。</div>`
    : '';
  const bubbles = state.ai.conversation.map(m => `<div class="ai-bubble ${m.role === 'user' ? 'user' : 'assistant'}">${esc(m.text).replace(/\n/g, '<br>')}</div>`).join('');
  const typing = state.ai.busy ? '<div class="ai-bubble assistant typing" aria-label="AI 正在思考"><span></span><span></span><span></span></div>' : '';
  transcript.innerHTML = welcome + bubbles + typing + renderAiPlan();
  if (!state.ai.busy && state.ai.plan) {
    const anchor = transcript.querySelector('.ai-bubble.assistant:last-of-type');
    transcript.scrollTop = anchor ? Math.max(0, anchor.offsetTop - 14) : transcript.scrollHeight;
  } else {
    transcript.scrollTop = transcript.scrollHeight;
  }
  const form = $('#ai-form');
  if (form) {
    form.elements.text.disabled = state.ai.busy;
    const button = $('button[type=submit]', form);
    if (button) button.disabled = state.ai.busy;
  }
}

function openAi(prefill = '') {
  const dialog = $('#ai-dialog');
  if (!dialog) return;
  renderAi();
  if (!dialog.open) dialog.showModal();
  const form = $('#ai-form');
  if (prefill && form) {
    form.elements.text.value = prefill;
    submitAi(form);
  } else {
    setTimeout(() => form?.elements.text.focus(), 60);
  }
}

function closeAi() {
  const dialog = $('#ai-dialog');
  if (dialog?.open) dialog.close();
}

async function submitAi(form) {
  const text = form.elements.text.value.trim();
  if (!text || state.ai.busy) return;
  const history = aiHistory();
  $('#ai-error').textContent = '';
  aiMessage('user', text);
  form.elements.text.value = '';
  state.ai.busy = true;
  state.ai.plan = null;
  state.ai.selected.clear();
  renderAi();
  try {
    const data = await api('/ai/plan', 'POST', {text, history});
    const summary = [data.reply || '', ...(data.questions || []).map(q => '需要确认：' + q)].filter(Boolean).join('\n');
    const fallback = (data.actions?.length || data.results?.length) ? '已整理操作清单，请核对后确认。' : '未识别出可执行操作，请补充说明。';
    aiMessage('assistant', summary || fallback);
    state.ai.plan = data;
    state.ai.selected = new Set((data.actions || []).filter(a => !a.errors?.length).map(a => a.index));
    renderAi();
  } catch (e) {
    $('#ai-error').textContent = e.message;
    aiMessage('assistant', e.message);
    renderAi();
  } finally {
    state.ai.busy = false;
    renderAi();
  }
}

async function executeAiPlan() {
  const plan = state.ai.plan;
  if (!plan?.plan_id || state.ai.busy) return;
  const actions = [...state.ai.selected].sort((a, b) => a - b);
  if (!actions.length) {
    toast('请先勾选要执行的操作', true);
    return;
  }
  state.ai.busy = true;
  renderAi();
  try {
    const data = await api('/ai/execute', 'POST', {plan_id: plan.plan_id, actions}, {'Idempotency-Key': randomId()});
    const lines = [`已完成 ${data.results.length} 项操作`];
    for (const r of data.results) {
      if (r.type === 'create_entry' || r.type === 'edit_entry') lines.push(`#${r.entry_id} ${entryDescription(r)} · ${r.amount} ${r.currency}`);
      else if (r.type === 'pay_entry') lines.push(`#${r.entry_id} 已登记付款`);
      else if (r.type === 'batch_pay') {
        const totals = Object.entries(r.totals || {}).map(([code, amount]) => `${amount} ${code}`).join(' / ');
        lines.push(`已登记 ${r.count} 笔报销${totals ? ` · ${totals}` : ''}`);
      } else if (r.type === 'delete_entry') lines.push(`#${r.entry_id} 已永久删除`);
      else lines.push(`#${r.entry_id} 已更新`);
    }
    for (const q of data.queries || []) {
      const line = aiQueryText(q.card);
      if (line) lines.push(line);
    }
    aiMessage('assistant', lines.join('\n'));
    state.ai.plan = null;
    state.ai.selected.clear();
    await refresh(false);
    render();
    renderAi();
    toast(`已执行 ${data.results.length} 项操作`);
  } catch (e) {
    $('#ai-error').textContent = e.message;
    aiMessage('assistant', e.message + (e.status === 409 ? '\n清单已失效，请重新描述。' : ''));
    if (e.status === 409) {
      state.ai.plan = null;
      state.ai.selected.clear();
    }
    renderAi();
  } finally {
    state.ai.busy = false;
    renderAi();
  }
}

function cancelAiPlan() {
  if (!state.ai.plan) return;
  aiMessage('assistant', '已取消本次操作清单。');
  state.ai.plan = null;
  state.ai.selected.clear();
  renderAi();
}

function aiEditInForm(index) {
  const plan = state.ai.plan;
  if (!plan) return;
  const action = plan.actions.find(a => a.index === index);
  if (!action || action.type !== 'create_entry') return;
  const r = action.resolved;
  state.ai.plan = {...plan, actions: plan.actions.filter(a => a.index !== index)};
  state.ai.selected.delete(index);
  closeAi();
  openEntry(r.kind, {
    kind: r.kind, expense_type: r.expense_type, amount: r.amount, currency: r.currency,
    description: r.description, date: r.date, payer_id: r.payer_id, note: r.note, status: r.status,
  });
  toast('已在表单中打开，保存后不会重复执行此项');
}

/* ============================================================================
 * Charts & View Renderers
 * ========================================================================== */

function rateDetails(report = state.report) {
  if (!report) return '';
  const rows = Object.entries(report.fx.rates).filter(([code]) => code !== report.base);
  return `${rows.length ? `<div class="rates">${rows.map(([code, r]) => `<span class="rate-chip">1 ${esc(code)} ≈ ${Number(r.rate).toLocaleString('zh-CN', {maximumFractionDigits: 6})} ${esc(report.base)} · ${esc(r.date)}</span>`).join('')}</div>` : '<p class="muted">当前账目无需跨币种换算。</p>'}<p class="fx-caption">${rows.length ? `${esc(report.fx.source)}${report.fx.stale ? ' · 缓存沿用' : ''}` : ''}</p>`;
}

function converted(entry, report = state.report) {
  const r = report?.fx.rates[entry.currency];
  return r ? Number(entry.amount) * Number(r.rate) : 0;
}

function chart() {
  const r = state.report;
  if (!r?.complete) return empty('等待汇率数据', '汇率恢复后将显示最近 7 天资金流向。');
  const days = [];
  for (let i = 6; i >= 0; i--) {
    const d = new Date(state.conf.today + 'T12:00:00Z');
    d.setUTCDate(d.getUTCDate() - i);
    const date = d.toISOString().slice(0, 10);
    const income = state.entries.filter(e => e.kind === 'income' && e.status === 'posted' && e.date === date).reduce((s, e) => s + converted(e), 0);
    const paid = state.entries.filter(e => e.kind === 'expense' && e.expense_type === 'public' && e.status === 'posted' && e.date === date).reduce((s, e) => s + converted(e), 0)
      + state.entries.reduce((s, e) => s + (e.payments || []).filter(p => p.created_at.slice(0, 10) === date).reduce((sum, p) => sum + converted(p), 0), 0);
    days.push({date, income, paid, net: income - paid});
  }
  const rawMax = Math.max(0, ...days.flatMap(d => [d.income, d.paid]));
  const max = rawMax > 0 ? rawMax : 100;
  const fmtTick = v => v.toLocaleString('zh-CN', {notation: 'compact', maximumFractionDigits: 1});
  const ticks = rawMax > 0 ? [fmtTick(max), fmtTick(max / 2), '0'] : ['—', '—', '0'];
  const c = cur(r.base);
  const fmtMoney = v => `${c.symbol}${v.toLocaleString('zh-CN', {minimumFractionDigits: c.decimals, maximumFractionDigits: c.decimals})} ${r.base}`;
  return `
    <div class="chart-container" id="cashflow-chart" role="group" aria-label="最近七天团队收入与公账付款，单位 ${esc(r.base)}">
      <div class="trend-chart-layout">
        <div class="trend-chart-axis" aria-hidden="true">
          ${ticks.map(t => `<span>${esc(t)}</span>`).join('')}
        </div>
        <div class="trend-chart-plot">
          <div class="trend-chart-bars">
            ${days.map((d, idx) => {
              const ip = rawMax > 0 && d.income > 0 ? Math.max(4, Math.round(d.income / max * 1000) / 10) : 0;
              const pp = rawMax > 0 && d.paid > 0 ? Math.max(4, Math.round(d.paid / max * 1000) / 10) : 0;
              const shortDate = d.date.slice(5).replace('-', '/');
              const netSign = d.net > 0 ? '+' : d.net < 0 ? '−' : '';
              return `<button type="button" class="trend-chart-col" data-chart-col="${idx}" data-date="${esc(d.date)}" data-income="${esc(fmtMoney(d.income))}" data-paid="${esc(fmtMoney(d.paid))}" data-net="${esc(netSign + fmtMoney(Math.abs(d.net)))}" aria-label="${esc(d.date)} 收入 ${esc(fmtMoney(d.income))}，支出 ${esc(fmtMoney(d.paid))}">
                <span class="trend-chart-track">
                  <i class="trend-bar income-bar" data-bar-h="${ip}"></i>
                  <i class="trend-bar paid-bar" data-bar-h="${pp}"></i>
                </span>
                <small>${esc(shortDate)}</small>
              </button>`;
            }).join('')}
          </div>
          <div class="activity-chart-tooltip chart-tooltip" id="cashflow-tooltip" hidden aria-live="polite">
            <strong id="ct-date"></strong>
            <dl>
              <div><dt>团队收入</dt><dd id="ct-income"></dd></div>
              <div><dt>公账与报销</dt><dd id="ct-paid"></dd></div>
              <div><dt>当日净变化</dt><dd id="ct-net"></dd></div>
            </dl>
          </div>
        </div>
      </div>
    </div>`;
}

function hydrateChart() {
  const root = $('#cashflow-chart');
  if (!root) return;
  $$('[data-bar-h]', root).forEach(bar => {
    bar.style.height = `${bar.dataset.barH || 0}%`;
  });
  const cols = $$('[data-chart-col]', root);
  const tooltip = $('#cashflow-tooltip');
  if (!tooltip || !cols.length) return;
  const showCol = col => {
    cols.forEach(c => c.classList.toggle('is-active', c === col));
    $('#ct-date').textContent = col.dataset.date || '';
    $('#ct-income').textContent = col.dataset.income || '';
    $('#ct-paid').textContent = col.dataset.paid || '';
    $('#ct-net').textContent = col.dataset.net || '';
    const idx = Number(col.dataset.chartCol || 0);
    tooltip.classList.toggle('is-right', idx < 4);
    tooltip.classList.toggle('is-left', idx >= 4);
    tooltip.hidden = false;
  };
  const hideTip = () => {
    cols.forEach(c => c.classList.remove('is-active'));
    tooltip.hidden = true;
  };
  cols.forEach((col, index) => {
    col.addEventListener('pointerenter', e => {
      if (e.pointerType !== 'touch') showCol(col);
    });
    col.addEventListener('focus', () => showCol(col));
    col.addEventListener('click', () => {
      if (col.classList.contains('is-active') && !tooltip.hidden) hideTip();
      else showCol(col);
    });
    col.addEventListener('keydown', e => {
      if (e.key === 'ArrowRight' && index < cols.length - 1) { e.preventDefault(); cols[index + 1].focus(); }
      else if (e.key === 'ArrowLeft' && index > 0) { e.preventDefault(); cols[index - 1].focus(); }
      else if (e.key === 'Escape') { hideTip(); }
    });
  });
  root.addEventListener('pointerleave', e => {
    if (e.pointerType !== 'touch') hideTip();
  });
  root.addEventListener('focusout', e => {
    if (!root.contains(e.relatedTarget)) hideTip();
  });
}

function renderOverview() {
  const r = state.report, ok = r?.complete, base = r?.base || state.conf.reference_currency;
  const recent = state.entries.filter(e => e.status !== 'voided');
  const pendingCount = state.entries.filter(e => e.status === 'pending').length;
  const metric = k => ok ? money(r[k], base) : '—';
  const memberRows = ok ? r.members : [];
  const teamLabel = state.conf.team_name ? `<span class="heading-team-tag">${esc(state.conf.team_name)}</span>` : '';

  return heading(
    '团队总览',
    `${canManage() ? `<button type="button" class="button button-outline" data-export-open="overview">${icon('download', 'ico-download')}导出 Excel</button>` : ''}${newButton()}`,
    teamLabel,
  ) + `
    ${reportWarning()}
    <div class="activity-summary-grid">
      <section class="summary-card">
        <div class="summary-card-head">
          <h2>团队可用余额</h2>
          <span class="mono muted">${esc(base)}</span>
        </div>
        <div class="summary-card-value">${metric('cash')}</div>
        <div class="summary-card-foot">
          <span>${ok && Number(r.shortage) > 0 ? `缺口 ${money(r.shortage, base, false)}` : ok ? `报销后结余 ${money(r.remaining, base, false)}` : '参考换算'}</span>
          <a class="activity-summary-link" href="#reimbursements/calculator">资金测算 ${icon('arrow', 'icon-sm')}</a>
        </div>
      </section>
      <section class="summary-card">
        <div class="summary-card-head">
          <h2>累计团队收入</h2>
          <span class="mono muted">${ok ? `今日 ${money(r.today_income, base, false)}` : ''}</span>
        </div>
        <div class="summary-card-value">${metric('income')}</div>
        <div class="summary-card-foot">
          <span>累计已报销 ${ok ? money(r.paid, base, false) : '—'}</span>
          ${canManage() ? `<a class="activity-summary-link" href="#ledger" data-income-link>登记收入 ${icon('arrow', 'icon-sm')}</a>` : '<span></span>'}
        </div>
      </section>
      <section class="summary-card">
        <div class="summary-card-head">
          <h2>团队待报销</h2>
          <span class="mono muted">${pendingCount} 笔</span>
        </div>
        <div class="summary-card-value">${metric('due')}</div>
        <div class="summary-card-foot">
          <span>可安排 ${ok ? money(r.payable, base, false) : '—'}</span>
          <a class="activity-summary-link" href="#reimbursements">去处理 ${icon('arrow', 'icon-sm')}</a>
        </div>
      </section>
    </div>

    <div class="overview-mid-grid">
      <section class="panel overview-chart-panel">
        <div class="panel-heading">
          <h3>最近 7 天资金流向 <span class="panel-meta mono">${esc(base)}</span></h3>
          <div class="chart-key"><span><i></i>收入</span><span><i class="gold"></i>支出</span></div>
        </div>
        ${chart()}
      </section>

      <section class="panel overview-members-panel">
        <div class="panel-heading">
          <h3>待报销成员</h3>
          <a href="#reimbursements" class="console-inline-link">去处理 ${icon('arrowUpRight', 'icon-sm')}</a>
        </div>
        <div class="overview-members-body">
          ${memberRows.length
            ? `<div class="activity-log-list">${memberRows.slice(0, 6).map(m => `<div class="member-row">${avatar(m.name, m.id)}<div class="member-info"><strong>${esc(m.name)}${m.id === state.user.id ? ' · 我' : ''}</strong><small>${state.entries.filter(e => e.payer_id === m.id && e.status === 'pending').length} 笔待报销</small></div><div class="member-amount">${money(m.due, base, false)} <small>${esc(base)}</small></div></div>`).join('')}</div>`
            : empty(ok ? '暂无待报销' : '等待汇率换算', ok ? '所有成员垫付已结清。' : '可在收支明细查看原币种记录。')}
        </div>
      </section>
    </div>

    <section class="panel recent-panel">
      <div class="panel-heading">
        <h3>最近账目 <span class="panel-meta mono">${recent.length} 笔</span></h3>
        <a href="#ledger" class="console-inline-link">全部收支 ${icon('arrowUpRight', 'icon-sm')}</a>
      </div>
      ${table(recent.slice(0, 6))}
    </section>`;
}

function filteredEntries() {
  const f = state.filter;
  return state.entries.filter(e =>
    (!f.text || [e.description, e.note, e.payer_name].join(' ').toLowerCase().includes(f.text.toLowerCase())) &&
    (f.kind === 'all' || e.kind === f.kind || (e.kind === 'expense' && e.expense_type === f.kind)) &&
    (f.currency === 'all' || e.currency === f.currency) &&
    (f.payer === 'all' || e.payer_id === Number(f.payer)) &&
    (f.status === 'all' ? e.status !== 'voided' : f.status === 'partial' ? entryStatus(e) === 'partial' : e.status === f.status) &&
    (!f.from || e.date >= f.from) &&
    (!f.to || e.date <= f.to)
  );
}

function secondaryFilterCount() {
  const f = state.filter;
  return [f.currency !== 'all', f.payer !== 'all', Boolean(f.from), Boolean(f.to)].filter(Boolean).length;
}

function ledgerHasFilter() {
  const f = state.filter;
  return Boolean(f.text || f.kind !== 'all' || f.currency !== 'all' || f.payer !== 'all' || f.status !== 'all' || f.from || f.to);
}

function ledgerResults() {
  const rows = filteredEntries();
  return table(rows);
}

function syncLedgerChrome() {
  const btn = $('#ledger-export-btn');
  if (btn) btn.innerHTML = `${icon('download', 'ico-download')}${ledgerHasFilter() ? '导出筛选 Excel' : '导出 Excel'}`;
  const countEl = $('#ledger-count');
  if (countEl) {
    const rows = filteredEntries();
    const total = state.filter.status === 'all' ? state.entries.filter(e => e.status !== 'voided').length : state.entries.length;
    countEl.textContent = ledgerHasFilter() ? `${rows.length} / ${total} 笔` : `${total} 笔`;
  }
  const secCount = secondaryFilterCount();
  const moreBtn = $('#toggle-more-filters');
  if (moreBtn) {
    moreBtn.classList.toggle('is-active', state.filterOpen || secCount > 0);
    moreBtn.setAttribute('aria-expanded', String(state.filterOpen));
    moreBtn.innerHTML = `${icon('filter', 'icon-sm')}<span>更多筛选</span>${secCount ? `<small class="filter-badge">${secCount}</small>` : ''}`;
  }
  const resetBtn = $('#reset-filters');
  if (resetBtn) resetBtn.hidden = !ledgerHasFilter();
}

function renderLedger() {
  const f = state.filter;
  const hasFilter = ledgerHasFilter();
  const secCount = secondaryFilterCount();
  const rows = filteredEntries();
  const total = f.status === 'all' ? state.entries.filter(e => e.status !== 'voided').length : state.entries.length;
  return heading(
    '收支明细',
    `${canManage() ? `<button type="button" class="button button-outline" id="ledger-export-btn" data-export-open="ledger">${icon('download', 'ico-download')}${hasFilter ? '导出筛选 Excel' : '导出 Excel'}</button>` : ''}${newButton()}`,
    `<span class="heading-count mono" id="ledger-count">${hasFilter ? `${rows.length} / ${total} 笔` : `${total} 笔`}</span>`,
  ) + `
    <section class="panel">
      <div class="filter-tabs" role="tablist" aria-label="收支类型筛选">
        ${[['all', '全部记录'], ['public', '公账支出'], ['advance', '成员垫付'], ['income', '团队收入']].map(([value, label]) => `<button type="button" role="tab" aria-selected="${f.kind === value}" data-filter-kind="${value}" class="${f.kind === value ? 'active' : ''}">${label}</button>`).join('')}
      </div>
      <div class="filters">
        <div class="search-box">
          ${icon('search')}
          <input id="search-entries" placeholder="搜索用途、成员、备注…" value="${esc(f.text)}" aria-label="搜索账目">
        </div>
        <select data-filter="status" aria-label="筛选状态"><option value="all">全部状态</option>${options(Object.entries(STATUS), x => x[0], x => x[1], f.status)}</select>
        <button type="button" class="button button-outline filter-more-btn${state.filterOpen || secCount > 0 ? ' is-active' : ''}" id="toggle-more-filters" aria-expanded="${state.filterOpen}" aria-controls="more-filters-panel">${icon('filter', 'icon-sm')}<span>更多筛选</span>${secCount ? `<small class="filter-badge">${secCount}</small>` : ''}</button>
        <button type="button" class="button button-quiet button-small" id="reset-filters" ${hasFilter ? '' : 'hidden'}>重置</button>
      </div>
      <div class="filters-secondary" id="more-filters-panel" ${state.filterOpen ? '' : 'hidden'}>
        <select data-filter="currency" aria-label="筛选币种"><option value="all">全部币种</option>${options(state.conf.currencies, c => c.code, c => c.code, f.currency)}</select>
        <select data-filter="payer" aria-label="筛选成员"><option value="all">全部成员</option>${options(state.members, m => m.id, m => m.name, f.payer)}</select>
        <label class="filter-date-field"><span>从</span><input type="date" data-filter="from" value="${esc(f.from)}" aria-label="开始日期"></label>
        <label class="filter-date-field"><span>至</span><input type="date" data-filter="to" value="${esc(f.to)}" aria-label="结束日期"></label>
      </div>
      <div id="ledger-results">${table(rows)}</div>
    </section>`;
}

function reimbursementRows() {
  return state.entries.filter(e => e.kind === 'expense' && e.expense_type === 'advance' && e.status === state.reimburseTab);
}

function batchToolbar() {
  const n = state.selected.size;
  return `<span>已选 ${n} 笔</span>${canManage() ? `<button type="button" class="button button-outline button-small" data-export-selected ${n ? '' : 'disabled'}>${icon('download', 'ico-download')}导出已选 Excel</button>` : ''}${state.reimburseTab === 'pending' ? '<button type="button" class="button button-primary button-small" data-batch="pay">登记已付款</button>' : ''}${state.reimburseTab === 'draft' ? '<button type="button" class="button button-primary button-small" data-batch="submit">记入账本</button>' : ''}`;
}

function calcSummary(report, scenario = false) {
  if (!report?.complete) {
    return `<div class="calc-summary"><span class="calc-summary-kicker">资金测算</span><div class="calc-amount">等待汇率</div><p>缺少可靠汇率时暂停合并计算。</p></div>${reportWarning(report)}`;
  }
  const b = report.base, mode = report.reimbursement?.mode || 'auto';
  const label = {auto: '按最早日期自动安排', budget: '按指定预算安排', manual: '仅安排手动勾选项目'}[mode];
  return `
    <div class="calc-summary">
      <div class="calc-summary-top">
        <span class="calc-summary-kicker">${scenario ? '新进账预演' : '当前余额规划'}</span>
        <span class="calc-summary-mode">${label}</span>
      </div>
      <div class="calc-amount">${money(report.reserve, b)}</div>
      <div class="calc-rows">
        <div class="calc-row"><span>${scenario ? '模拟后' : '当前'}可用资金</span><strong>${money(report.cash, b)}</strong></div>
        <div class="calc-row"><span>全部待报销</span><strong>${money(report.due, b)}</strong></div>
        ${mode === 'manual' ? `<div class="calc-row"><span>已选项目待报销</span><strong>${money(report.selected_due, b)}</strong></div>` : ''}
        ${mode === 'budget' ? `<div class="calc-row"><span>指定预算</span><strong>${money(report.reimbursement.budget, b)}</strong></div>` : ''}
        <div class="calc-row"><span>本次可安排付款</span><strong>${money(report.payable, b)}</strong></div>
        <div class="calc-row highlight"><span>安排后结余</span><strong>${money(report.remaining, b)}</strong></div>
        <div class="calc-row"><span>安排后仍待报销</span><strong>${money(report.unplanned, b)}</strong></div>
        <div class="calc-row ${Number(report.shortage) > 0 ? 'shortage' : ''}"><span>覆盖全部待报销缺口</span><strong>${money(report.shortage, b)}</strong></div>
      </div>
    </div>`;
}

function planGroups(report, selectable = false) {
  if (!report?.complete || !report.members.length) {
    return empty('暂无待报销项目', '有成员垫付待结清时，这里会生成每位成员的建议付款安排。', '');
  }
  return `<div class="plan-groups">${report.members.map(m => `
    <div class="plan-group">
      <div class="plan-group-head">
        <div class="plan-group-member">${avatar(m.name, m.id)}<strong>${esc(m.name)}</strong><span class="mono muted">${m.entries.length} 笔</span></div>
        <div class="plan-group-totals">
          <span>待报 ${money(m.due, report.base, false)}</span>
          <strong class="plan-group-suggested">建议 ${money(m.suggested, report.base)}</strong>
        </div>
      </div>
      <div class="plan-group-items">
        ${m.entries.map(entry => {
          const hasSuggestion = Number(entry.suggested) > 0;
          const status = hasSuggestion ? (entry.fully_covered ? '建议付款' : '部分安排') : '暂不安排';
          const original = money(entry.amount, entry.currency, false);
          const convertedText = entry.currency === report.base ? '' : ` ≈ ${money(entry.settlement, report.base, false)} ${esc(report.base)}`;
          return `<div class="plan-item ${hasSuggestion ? 'is-selected' : 'is-waiting'}">
            ${selectable ? `<input class="plan-checkbox" type="checkbox" data-calc-select="${entry.id}" ${state.calculator.ids.has(entry.id) ? 'checked' : ''} aria-label="选择报销项目 #${entry.id} ${esc(entryDescription(entry))}">` : ''}
            <span class="plan-item-status">${status}</span>
            <div class="plan-item-main">
              <strong>${esc(entryDescription(entry))}</strong>
              <small class="mono">#${entry.id} · ${esc(entry.date)} · 剩余 ${original} ${esc(entry.currency)}${convertedText}${Number(entry.reimbursed_amount) > 0 ? ` · 已报 ${money(entry.reimbursed_amount, entry.currency, false)}` : ''}</small>
            </div>
            <div class="plan-item-suggested">${hasSuggestion ? money(entry.suggested, report.base, false) : '—'}</div>
          </div>`;
        }).join('')}
      </div>
    </div>`).join('')}</div>`;
}

function renderReimbursements() {
  const mode = state.reimburseMode === 'calculator' ? 'calculator' : 'queue';
  const c = state.calculator;
  const r = mode === 'calculator' ? (c.report || state.report) : state.report;
  const base = r?.base || state.conf.reference_currency;
  const metric = k => r?.complete ? money(r[k], base) : '—';
  const rows = reimbursementRows();
  const selectable = canManage() && ['pending', 'draft'].includes(state.reimburseTab);
  const modeSwitch = `
    <div class="segmented segmented-compact" role="tablist" aria-label="报销与资金视图">
      <button type="button" role="tab" aria-selected="${mode === 'queue'}" class="${mode === 'queue' ? 'active' : ''}" data-reimburse-mode="queue">报销处理</button>
      <button type="button" role="tab" aria-selected="${mode === 'calculator'}" class="${mode === 'calculator' ? 'active' : ''}" data-reimburse-mode="calculator">资金测算</button>
    </div>`;

  const actions = mode === 'calculator'
    ? `${baseSelect()}${canManage() ? `<button type="button" class="button button-outline" data-export-open="calculator">${icon('download', 'ico-download')}导出 Excel</button>` : ''}`
    : `${canManage() ? `<button type="button" class="button button-outline" data-export-open="reimbursements">${icon('download', 'ico-download')}导出 Excel</button>` : ''}${newButton()}`;

  const summaryBar = `
    <div class="metric-strip">
      <div class="metric-cell"><small>全部待报销</small><strong>${metric('due')}</strong></div>
      <div class="metric-cell"><small>当前可安排资金</small><strong>${metric('payable')}</strong></div>
      <div class="metric-cell"><small>累计已报销</small><strong>${metric('paid')}</strong></div>
    </div>`;

  if (mode === 'queue') {
    return heading('报销与资金', actions, modeSwitch) + summaryBar + reportWarning() + `
      <section class="panel">
        <div class="filter-tabs" role="tablist" aria-label="报销状态">
          ${['pending', 'reimbursed', 'draft', 'voided'].map(s => `<button type="button" role="tab" aria-selected="${state.reimburseTab === s}" data-reimburse-tab="${s}" class="${state.reimburseTab === s ? 'active' : ''}">${STATUS[s]}<small>${state.entries.filter(e => e.kind === 'expense' && e.expense_type === 'advance' && e.status === s).length}</small></button>`).join('')}
        </div>
        <div id="reimbursement-results">${table(rows, {checkbox: selectable})}</div>
        ${rows.length && selectable ? `<div class="batch-toolbar" id="batch-toolbar">${batchToolbar()}</div>` : ''}
      </section>`;
  }

  if (c.budgetBase && c.budgetBase !== base) c.budget = '';
  c.budgetBase = base;
  const fxIssue = Boolean(!r?.complete || r?.fx?.warning);
  const fxCount = r?.fx?.rates ? Object.keys(r.fx.rates).filter(code => code !== r.base).length : 0;

  return heading('报销与资金', actions, modeSwitch) + summaryBar + `
    <div class="calculator-grid">
      <section class="panel"><div class="panel-inner">
        <h3>测算参数</h3>
        <form id="calculator-form">
          <div class="form-grid">
            <label><span>模拟新增进账 <small class="optional">选填</small></span><input name="amount" value="${esc(c.amount)}" inputmode="decimal" placeholder="留空按当前余额"></label>
            <label><span>收入币种</span><select name="currency">${currencyOptions(c.currency || state.conf.default_currency)}</select></label>
          </div>
          <label><span>报销方式</span><select name="reimbursement_mode" id="calculator-mode">${options([{id: 'auto', name: '自动安排 · 最早发生优先'}, {id: 'budget', name: '指定报销预算'}, {id: 'manual', name: '手动勾选报销项目'}], m => m.id, m => m.name, c.mode)}</select></label>
          <label id="calculator-budget-field" ${c.mode === 'budget' ? '' : 'hidden'}><span>报销预算（${esc(base)}）</span><input name="budget" value="${esc(c.budget)}" inputmode="decimal" placeholder="例如：100.00" ${c.mode === 'budget' ? 'required' : ''}></label>
          <p id="calculator-selection-hint" class="calc-selection-inline" ${c.mode === 'manual' ? '' : 'hidden'}>在下方清单勾选本次要报销的项目 · 已选 <strong id="calculator-selection-count">${c.ids.size}</strong> 项</p>
          <div class="integration-actions">
            <button class="button button-primary full-width" type="submit">${icon('calc')}计算安排</button>
          </div>
          <div class="form-error" id="calculator-error" role="alert"></div>
        </form>
      </div></section>
      <section id="calculator-result" aria-live="polite">${calcSummary(r, Boolean(r?.scenario))}</section>
    </div>

    <section class="panel recent-panel">
      <div class="panel-heading">
        <h3>成员报销安排 <span class="panel-meta mono">${esc(base)}</span></h3>
        <button type="button" class="console-inline-link" data-reimburse-mode="queue">返回报销处理 ${icon('arrow', 'icon-sm')}</button>
      </div>
      <div id="plan-table">${planGroups(r, c.mode === 'manual')}</div>
    </section>

    <details class="panel fx-disclosure recent-panel" ${fxIssue ? 'open' : ''}>
      <summary class="fx-disclosure-summary">
        <div class="fx-disclosure-title">
          <h3>参考汇率</h3>
          <span class="badge ${fxIssue ? 'warn' : 'neutral'}">${fxIssue ? '需要检查' : fxCount ? `${fxCount} 组汇率` : '单币种'}</span>
        </div>
        <button type="button" class="button button-outline button-small" id="refresh-fx">${icon('refresh', 'ico-refresh')}刷新汇率</button>
      </summary>
      <div class="panel-inner fx-disclosure-body" id="fx-details">${rateDetails(r)}${reportWarning(r)}</div>
    </details>`;
}

async function calculateFunds() {
  const form = $('#calculator-form');
  if (!form) return;
  const c = state.calculator;
  c.amount = form.elements.amount.value.trim();
  c.currency = form.elements.currency.value;
  c.mode = form.elements.reimbursement_mode.value;
  c.budget = form.elements.budget.value.trim();
  $('#calculator-error').textContent = '';
  const reimbursement = c.mode === 'budget'
    ? {mode: 'budget', budget: c.budget || '0'}
    : c.mode === 'manual'
      ? {mode: 'manual', entry_ids: [...c.ids]}
      : {mode: 'auto'};
  const request = ++c.request;
  $('#calculator-result')?.setAttribute('aria-busy', 'true');
  try {
    const data = await api('/calculate', 'POST', {
      amount: c.amount, currency: c.currency,
      base: state.base || state.conf.reference_currency,
      reimbursement,
    });
    if (request !== c.request || !$('#calculator-form')) return;
    c.report = data;
    $('#calculator-result').innerHTML = calcSummary(data, Boolean(data.scenario));
    $('#plan-table').innerHTML = planGroups(data, c.mode === 'manual');
    $('#fx-details').innerHTML = rateDetails(data) + reportWarning(data);
  } catch (e) {
    if (request !== c.request || !$('#calculator-form')) return;
    c.report = null;
    $('#calculator-error').textContent = e.message;
    $('#calculator-result').innerHTML = '<div class="calc-summary"><h3>请调整参数后重新计算</h3><p>当前输入尚未生成有效的资金安排。</p></div>';
    $('#plan-table').innerHTML = empty('尚未生成安排', '请检查输入后重新计算。', '');
  } finally {
    if (request === c.request) $('#calculator-result')?.setAttribute('aria-busy', 'false');
  }
}

/* ============================================================================
 * Settings Workbench (Role-Scoped Left Pill Sidebar)
 * ========================================================================== */

function allowedSettingsTabs() {
  return isOwner() ? ['members', 'my-bots', 'general', 'bots-ai', 'backups'] : ['members', 'my-bots'];
}

function currencyEditorRow(c) {
  return `<div class="currency-edit-row"><input data-cur="code" value="${esc(c.code)}" maxlength="3" aria-label="币种代码" placeholder="USD" required><input data-cur="name" value="${esc(c.name)}" maxlength="20" aria-label="币种名称" placeholder="美元" required><input data-cur="symbol" value="${esc(c.symbol)}" maxlength="8" aria-label="币种符号" placeholder="$" required><select data-cur="decimals" aria-label="币种小数位">${[0, 2, 3, 4].map(d => `<option ${d === c.decimals ? 'selected' : ''}>${d}</option>`).join('')}</select><button type="button" class="icon-button" data-remove-currency aria-label="移除此币种">${icon('close', 'icon-sm')}</button></div>`;
}

function renderSettingsMembers() {
  const activeCount = state.members.filter(m => m.active).length;
  const actions = [];
  if (canManage()) {
    actions.push(`<button type="button" class="button button-outline" id="sync-members">${icon('refresh', 'ico-refresh')}同步权限</button>`);
  }
  if (isOwner() && state.adminUrl) {
    actions.push(`<a class="button button-primary" href="${esc(state.adminUrl)}" target="_blank" rel="noreferrer">Vanora 管理后台 ${icon('arrowUpRight', 'icon-sm')}</a>`);
  }
  return `
    <div class="settings-section-head">
      <div>
        <h2>团队成员 <span class="panel-meta mono">${activeCount} 位启用</span></h2>
      </div>
      ${actions.length ? `<div class="heading-actions">${actions.join('')}</div>` : ''}
    </div>
    <section class="panel">
      ${state.members.length ? `<p class="table-scroll-hint" aria-hidden="true">${icon('move', 'icon-sm')}左右滑动查看完整列表</p><div class="table-wrap" role="region" aria-label="团队成员列表" tabindex="0"><table class="data-table members-table"><thead><tr><th>成员</th><th>角色</th><th>绑定状态</th><th class="money">待报销</th></tr></thead><tbody>${state.members.map(m => `<tr class="${m.active ? '' : 'is-inactive'}"><td><div class="member-table-cell">${avatar(m.name, m.id)}<div><strong>${esc(m.name)}${m.id === state.user.id ? ' · 我' : ''}</strong><span class="subline mono">@${esc(m.username)}${m.oidc_email ? ` · ${esc(m.oidc_email)}` : ''}</span></div></div></td><td><span class="badge ${m.active ? 'good' : 'draft'}">${esc(roleName(m.role))}${m.active ? '' : ' · 已停用'}</span></td><td><div class="member-links-cell"><span class="link-pill ${m.oidc_linked ? 'on' : ''}"><i class="status-dot ${m.oidc_linked ? 'online' : ''}"></i>Vanora</span><span class="link-pill ${m.feishu_linked ? 'on' : ''}"><i class="status-dot ${m.feishu_linked ? 'online' : ''}"></i>飞书</span><span class="link-pill ${m.telegram_linked ? 'on' : ''}"><i class="status-dot ${m.telegram_linked ? 'online' : ''}"></i>TG</span></div></td><td class="money">${groupedTotals(state.entries.filter(e => e.payer_id === m.id && e.status === 'pending'), 'remaining_amount')}</td></tr>`).join('')}</tbody></table></div>` : empty('暂无已同步成员', '在 Vanora OAuth 为成员分配客户端角色后，点击右上角「同步权限」。', '')}
    </section>`;
}

function renderSettingsMyBots() {
  const i = state.integrations || {};
  return `
    <div class="settings-section-head">
      <h2>我的机器人</h2>
    </div>
    <div class="my-bots-grid">
      <section class="panel bot-bind-card">
        <div class="bot-bind-head">
          <div class="integration-title-wrap">
            <span class="integration-icon feishu">${icon('message')}</span>
            <div>
              <h3>飞书 / Lark</h3>
              <div class="bot-status-line">
                <span><i class="status-dot ${i.feishu_linked ? 'online' : ''}"></i>${i.feishu_linked ? '已绑定' : '未绑定'}</span>
                <span class="meta-sep">·</span>
                <span><i class="status-dot ${i.feishu_online ? 'online' : i.feishu_configured ? 'warn' : ''}"></i>${i.feishu_online ? '在线' : i.feishu_configured ? '未在线' : '未配置'}</span>
              </div>
            </div>
          </div>
        </div>
        ${i.feishu_bot_name ? `<p class="bot-meta-line">当前应用：<strong>${esc(i.feishu_bot_name)}</strong></p>` : ''}
        ${i.feishu_last_error ? `<div class="callout warning">${icon('info')}<span class="callout-body">${esc(i.feishu_last_error)}</span></div>` : ''}
        <div class="integration-actions">
          <button type="button" class="button button-primary" id="generate-feishu-link">${i.feishu_linked ? '重新生成绑定码' : '生成绑定码'}</button>
          ${i.feishu_linked ? '<button type="button" class="button button-outline button-danger-outline" id="unlink-feishu">解除绑定</button>' : ''}
        </div>
        <div id="feishu-link-code-result"></div>
      </section>

      <section class="panel bot-bind-card">
        <div class="bot-bind-head">
          <div class="integration-title-wrap">
            <span class="integration-icon">${icon('send')}</span>
            <div>
              <h3>Telegram</h3>
              <div class="bot-status-line">
                <span><i class="status-dot ${i.telegram_linked ? 'online' : ''}"></i>${i.telegram_linked ? '已绑定' : '未绑定'}</span>
                <span class="meta-sep">·</span>
                <span><i class="status-dot ${i.bot_online ? 'online' : i.bot_configured ? 'warn' : ''}"></i>${i.bot_online ? '在线' : i.bot_configured ? '未在线' : '未配置'}</span>
              </div>
            </div>
          </div>
        </div>
        ${i.bot_username ? `<p class="bot-meta-line">机器人：<a class="console-inline-link" href="https://t.me/${esc(i.bot_username)}" target="_blank" rel="noreferrer">@${esc(i.bot_username)} ${icon('arrowUpRight', 'icon-sm')}</a></p>` : ''}
        <div class="integration-actions">
          <button type="button" class="button button-primary" id="generate-link">${i.telegram_linked ? '重新生成绑定码' : '生成绑定码'}</button>
          ${i.telegram_linked ? '<button type="button" class="button button-outline button-danger-outline" id="unlink-telegram">解除绑定</button>' : ''}
        </div>
        <div id="link-code-result"></div>
      </section>
    </div>`;
}

function renderSettingsGeneral() {
  const c = state.conf;
  return `
    <div class="settings-section-head">
      <h2>常规与币种</h2>
    </div>
    <form id="settings-form">
      <section class="panel">
        <div class="panel-inner">
          <h3>团队偏好</h3>
          <div class="settings-three-col">
            <label><span>团队名称</span><input name="team_name" value="${esc(c.team_name)}" maxlength="40" required></label>
            <label><span>默认记账币种</span><select name="default_currency" id="default-currency">${currencyOptions(c.default_currency)}</select></label>
            <label><span>资金汇总参考币种</span><select name="reference_currency" id="reference-currency">${currencyOptions(c.reference_currency)}</select></label>
          </div>
          <p class="fx-caption">账目时区 <span class="mono">${esc(c.timezone)}</span></p>
        </div>
        <div class="panel-divider"></div>
        <div class="panel-inner">
          <div class="panel-inner-head">
            <h3>可用币种</h3>
            <button type="button" class="button button-outline button-small" id="add-currency">${icon('plus', 'ico-plus')}添加币种</button>
          </div>
          <div class="currencies-editor">
            <div class="currency-edit-head"><span>代码</span><span>名称</span><span>符号</span><span>小数位</span><span></span></div>
            <div id="currency-rows">${c.currencies.map(currencyEditorRow).join('')}</div>
          </div>
        </div>
        <div class="panel-footer-actions">
          <div class="form-error panel-footer-error" id="settings-error" role="alert"></div>
          <button class="button button-primary" type="submit">保存团队设置</button>
        </div>
      </section>
    </form>`;
}

function renderSettingsBotsAi() {
  const i = state.integrations || {};
  const c = state.conf;
  const domainVal = i.feishu_domain || 'feishu';
  const presetDomain = ['feishu', 'lark'].includes(domainVal) ? domainVal : 'custom';
  const aiCfg = i.ai || {
    enabled: true, configured: Boolean(i.ai_configured), source: 'none',
    base_url: 'https://api.openai.com/v1', has_api_key: false, model: '',
    response_format: 'json_object', timeout: 40, context_entries: 120,
    context_chars: 60000, max_actions: 20, updated_at: '',
  };
  const statusClass = aiCfg.configured ? 'good' : (!aiCfg.enabled && aiCfg.has_api_key ? 'warn' : 'draft');
  const statusLabel = aiCfg.configured ? '已就绪' : (!aiCfg.enabled && aiCfg.has_api_key ? '已停用' : '未配置');

  return `
    <div class="settings-section-head">
      <h2>机器人与 AI</h2>
      <button type="button" class="button button-outline" data-ai-open>${icon('spark')}打开 AI 助手</button>
    </div>

    <div class="settings-stack">
      <form id="ai-config-form" class="panel">
        <div class="panel-heading">
          <div class="panel-heading-inline">
            <h3>AI 记账引擎</h3>
            <span class="badge ${statusClass}">${statusLabel}</span>
            ${aiCfg.configured && aiCfg.model ? `<span class="badge neutral mono">${esc(aiCfg.model)}</span>` : ''}
            ${aiCfg.source === 'env' ? '<span class="badge warn">环境变量</span>' : ''}
          </div>
          <label class="ai-switch-row">
            <input class="settings-switch" id="ai-enabled" name="enabled" type="checkbox" ${aiCfg.enabled ? 'checked' : ''}>
            <span>启用</span>
          </label>
        </div>
        <div class="panel-inner form-stack">
          <label>
            <span>接口地址（Base URL）</span>
            <input id="ai-base-url" name="base_url" type="url" value="${esc(aiCfg.base_url || 'https://api.openai.com/v1')}" placeholder="https://api.openai.com/v1" autocomplete="off">
          </label>
          <div class="form-grid">
            <label>
              <span>模型名称</span>
              <input id="ai-model" name="model" value="${esc(aiCfg.model || '')}" placeholder="例如：gpt-4o-mini" autocomplete="off">
            </label>
            <label>
              <span>返回格式</span>
              <select id="ai-response-format" name="response_format">
                <option value="json_object" ${aiCfg.response_format === 'json_object' ? 'selected' : ''}>JSON 对象（推荐）</option>
                <option value="json_schema" ${aiCfg.response_format === 'json_schema' ? 'selected' : ''}>结构化输出（json_schema）</option>
                <option value="none" ${aiCfg.response_format === 'none' ? 'selected' : ''}>不指定</option>
              </select>
            </label>
          </div>
          <label>
            <span>API Key <small class="optional">${aiCfg.has_api_key ? '已保存，留空保留原密钥' : '选填占位符'}</small></span>
            <input id="ai-api-key" name="api_key" type="password" autocomplete="new-password" placeholder="${aiCfg.has_api_key ? '已保存，留空保留原密钥' : 'sk-...'}">
          </label>
          <details class="disclosure-box">
            <summary><span>高级参数（超时、上下文与上限）</span></summary>
            <div class="disclosure-body">
              <div class="form-grid">
                <label><span>请求超时（秒）</span><input id="ai-timeout" name="timeout" type="number" min="5" max="180" step="1" value="${esc(aiCfg.timeout)}"></label>
                <label><span>单次最多操作数</span><input id="ai-max-actions" name="max_actions" type="number" min="1" max="50" step="1" value="${esc(aiCfg.max_actions)}"></label>
                <label><span>上下文最近条数</span><input id="ai-context-entries" name="context_entries" type="number" min="20" max="500" step="1" value="${esc(aiCfg.context_entries)}"></label>
                <label><span>上下文字符上限</span><input id="ai-context-chars" name="context_chars" type="number" min="5000" max="200000" step="1000" value="${esc(aiCfg.context_chars)}"></label>
              </div>
            </div>
          </details>
          <details class="disclosure-box">
            <summary><span>自然语言指令示例</span></summary>
            <div class="disclosure-body">
              <div class="code-block">昨天我垫了打车 35 元，老王垫的聚餐 240 元，
收到客户尾款 2000 美元；
公账支付服务器费 100 元；看看现在能报多少。</div>
            </div>
          </details>
        </div>
        <div class="panel-footer-actions panel-footer-split">
          <div class="panel-footer-left">
            ${aiCfg.has_api_key || aiCfg.source === 'db' ? '<button type="button" class="button button-outline button-danger-outline" id="clear-ai-config">清除配置</button>' : ''}
            <div class="form-error panel-footer-error" id="ai-config-error" role="alert"></div>
          </div>
          <div class="modal-actions-group">
            <button type="button" class="button button-outline" id="test-ai-config">保存并测试</button>
            <button type="submit" class="button button-primary" id="save-ai-config">保存 AI 配置</button>
          </div>
        </div>
      </form>

      <form id="feishu-config-form" class="panel">
        <div class="panel-heading">
          <div class="panel-heading-inline">
            <h3>飞书 / Lark 应用</h3>
            <span class="badge ${i.feishu_online ? 'good' : i.feishu_configured ? 'warn' : 'draft'}">${i.feishu_online ? '在线' : i.feishu_configured ? '未在线' : '未配置'}</span>
            ${i.feishu_embed_enabled ? '<span class="badge good">工作台嵌入</span>' : ''}
          </div>
        </div>
        <div class="panel-inner form-stack">
          ${i.feishu_last_error ? `<div class="callout warning">${icon('info')}<span class="callout-body">${esc(i.feishu_last_error)}</span></div>` : ''}
          <div class="form-grid">
            <label><span>App ID</span><input id="feishu-app-id" name="app_id" value="${esc(i.feishu_app_id || '')}" placeholder="cli_xxxxxxxxxxxxxxxx" autocomplete="off" required></label>
            <label><span>App Secret</span><input id="feishu-app-secret" name="app_secret" type="password" autocomplete="new-password" placeholder="${i.feishu_configured ? '留空保留已保存密钥' : '填写应用密钥'}"></label>
          </div>
          <div class="form-grid">
            <label>
              <span>服务区域 / 域名</span>
              <select id="feishu-domain-preset" name="domain_preset">
                <option value="feishu" ${presetDomain === 'feishu' ? 'selected' : ''}>飞书国内版（open.feishu.cn）</option>
                <option value="lark" ${presetDomain === 'lark' ? 'selected' : ''}>Lark 国际版（open.larksuite.com）</option>
                <option value="custom" ${presetDomain === 'custom' ? 'selected' : ''}>自定义私有化域名</option>
              </select>
            </label>
            <label id="feishu-domain-custom-wrap" ${presetDomain === 'custom' ? '' : 'hidden'}>
              <span>自定义服务地址</span>
              <input id="feishu-domain-custom" name="domain_custom" type="url" value="${presetDomain === 'custom' ? esc(domainVal) : ''}" placeholder="https://open.example.com" autocomplete="off">
            </label>
          </div>
          <details class="disclosure-box">
            <summary><span>飞书与 Telegram 机器人部署指引</span></summary>
            <div class="disclosure-body help-steps">
              <p><strong>飞书 / Lark：</strong>在开放平台创建企业自建应用并启用机器人，开启长连接事件 <code>im.message.receive_v1</code> 与卡片回调 <code>card.action.trigger</code>，启动 <code>python -m app.feishu</code>；成员在「我的机器人」生成绑定码私聊发送。</p>
              <p><strong>Telegram：</strong>通过 BotFather 获取 Token 后写入 <code>TELEGRAM_BOT_TOKEN</code> 并启动 <code>python -m app.bot</code>；私聊发送 <code>/start 绑定码</code> 完成绑定。</p>
            </div>
          </details>
        </div>
        <div class="panel-footer-actions">
          <button type="submit" class="button button-primary" id="save-feishu-config">保存飞书配置</button>
        </div>
      </form>

      <form id="allowlist-form" class="panel">
        <div class="panel-heading">
          <h3>群组允许名单</h3>
        </div>
        <div class="panel-inner">
          <div class="form-grid">
            <label>
              <span>飞书群组 ID <small class="optional">一行一个，群内发 /groupid 获取</small></span>
              <textarea name="feishu_allowed_chats" rows="4" placeholder="例如：oc_123456789">${esc((c.feishu_allowed_chats || []).join('\n'))}</textarea>
            </label>
            <label>
              <span>Telegram 群组 ID <small class="optional">一行一个</small></span>
              <textarea name="telegram_allowed_chats" rows="4" placeholder="例如：-1001234567890">${esc((c.telegram_allowed_chats || []).join('\n'))}</textarea>
            </label>
          </div>
        </div>
        <div class="panel-footer-actions">
          <div class="form-error panel-footer-error" id="allowlist-error" role="alert"></div>
          <button class="button button-primary" type="submit" id="save-allowlist">保存群组白名单</button>
        </div>
      </form>
    </div>`;
}

function renderSettings() {
  const allowed = allowedSettingsTabs();
  if (!allowed.includes(state.settingsTab)) state.settingsTab = allowed[0];
  const tab = state.settingsTab;
  const personalGroup = [
    ['members', '团队成员', 'users'],
    ['my-bots', '我的机器人', 'message'],
  ];
  const teamGroup = isOwner() ? [
    ['general', '常规与币种', 'settings'],
    ['bots-ai', '机器人与 AI', 'spark'],
    ['backups', '备份与恢复', 'database'],
  ] : [];

  let bodyHtml = '';
  if (tab === 'members') bodyHtml = renderSettingsMembers();
  else if (tab === 'my-bots') bodyHtml = renderSettingsMyBots();
  else if (tab === 'general') bodyHtml = renderSettingsGeneral();
  else if (tab === 'bots-ai') bodyHtml = renderSettingsBotsAi();
  else if (tab === 'backups') bodyHtml = '<div id="backup-settings-slot"></div>';

  const navItem = ([id, label, ico]) =>
    `<button type="button" class="settings-nav-item${tab === id ? ' active' : ''}" data-settings-tab="${id}" ${tab === id ? 'aria-current="page"' : ''}>${icon(ico, 'icon-sm')}<span>${esc(label)}</span></button>`;

  return heading('设置') + `
    <div class="settings-shell">
      <nav class="settings-sidebar" aria-label="设置分组">
        <div class="settings-nav-group">
          <span class="settings-nav-label">个人与团队</span>
          ${personalGroup.map(navItem).join('')}
        </div>
        ${teamGroup.length ? `<div class="settings-nav-group">
          <span class="settings-nav-label">团队管理</span>
          ${teamGroup.map(navItem).join('')}
        </div>` : ''}
      </nav>
      <div class="settings-pane">${bodyHtml}</div>
    </div>`;
}

const renderers = {
  overview: renderOverview,
  ledger: renderLedger,
  reimbursements: renderReimbursements,
  settings: renderSettings,
};

/* ============================================================================
 * Vanora Top Navigation Sliding Indicator & Account Dropdown
 * ========================================================================== */

let navObserver = null;
let navInitialized = false;

function updateNavIndicator() {
  const nav = $('#navigation');
  const indicator = $('#nav-indicator');
  if (!nav || !indicator) return;
  const selected = nav.querySelector('a.active');
  if (!selected || selected.offsetWidth <= 0) {
    nav.removeAttribute('data-indicator');
    return;
  }
  const navRect = nav.getBoundingClientRect();
  const selRect = selected.getBoundingClientRect();
  const left = selRect.left - navRect.left + nav.scrollLeft - nav.clientLeft;
  const width = selRect.width;
  indicator.style.setProperty('--console-nav-left', `${left}px`);
  indicator.style.setProperty('--console-nav-width', `${width}px`);
  nav.setAttribute('data-indicator', 'ready');
}

function scrollActiveNavIntoView() {
  const nav = $('#navigation');
  const selected = nav?.querySelector('a.active');
  if (!nav || !selected) return;
  const padding = parseFloat(getComputedStyle(nav).paddingLeft) || 16;
  const left = selected.offsetLeft - padding;
  const right = selected.offsetLeft + selected.offsetWidth + padding;
  if (left < nav.scrollLeft || right > nav.scrollLeft + nav.clientWidth) {
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    nav.scrollTo({
      left: left < nav.scrollLeft ? left : right - nav.clientWidth,
      behavior: navInitialized && !reduced ? 'smooth' : 'auto',
    });
  }
  navInitialized = true;
}

/* ============================================================================
 * Vanora Unified Theme Manager (Light / Dark / System + Cross-Subdomain Sync)
 * ========================================================================== */

const THEME_STORAGE_KEY = 'vanora.theme.v1';
const THEME_COOKIE_NAME = 'vanora_theme';
const THEME_COOKIE_MAX_AGE = 60 * 60 * 24 * 365;
const THEME_COLOR_LIGHT = '#fafafa';
const THEME_COLOR_DARK = '#111111';
const THEME_OPTIONS = [
  {value: 'light', label: '浅色', icon: 'sun'},
  {value: 'dark', label: '深色', icon: 'moon'},
  {value: 'system', label: '跟随系统', icon: 'monitor'},
];
const THEME_LABELS = {light: '浅色', dark: '深色', system: '跟随系统'};
const THEME_SECOND_LEVEL_SUFFIXES = new Set([
  'ac.cn', 'com.cn', 'edu.cn', 'gov.cn', 'net.cn', 'org.cn',
  'co.jp', 'ne.jp', 'or.jp', 'co.uk', 'org.uk', 'ac.uk',
  'com.au', 'net.au', 'org.au', 'com.hk', 'com.tw', 'com.sg', 'co.kr',
]);

let themePreference = 'system';
let themeMediaQuery = null;

function parseThemePreference(value) {
  return value === 'light' || value === 'dark' || value === 'system' ? value : null;
}

function readThemeCookie() {
  if (typeof document === 'undefined' || !document.cookie) return null;
  const padded = `; ${document.cookie}`;
  const prefix = `; ${THEME_COOKIE_NAME}=`;
  const idx = padded.indexOf(prefix);
  if (idx === -1) return null;
  const raw = padded.slice(idx + prefix.length).split(';', 1)[0].trim();
  try {
    return parseThemePreference(decodeURIComponent(raw));
  } catch {
    return parseThemePreference(raw);
  }
}

function resolveThemeCookieDomain(hostname) {
  const lower = String(hostname || '').toLowerCase().replace(/\.$/, '');
  if (!lower || lower === 'localhost' || lower.endsWith('.localhost')) return null;
  if (/^\d{1,3}(?:\.\d{1,3}){3}$/.test(lower) || lower.includes(':')) return null;
  const parts = lower.split('.').filter(Boolean);
  if (parts.length < 2) return null;
  const lastTwo = parts.slice(-2).join('.');
  const root = THEME_SECOND_LEVEL_SUFFIXES.has(lastTwo)
    ? (parts.length >= 3 ? parts.slice(-3).join('.') : null)
    : lastTwo;
  if (!root) return null;
  if (lower !== root && lower.endsWith(`.${root}`)) return `.${root}`;
  return null;
}

function writeThemeCookie(pref) {
  if (typeof document === 'undefined' || typeof location === 'undefined') return;
  const secureFlag = location.protocol === 'https:' ? '; Secure' : '';
  const sharedDomain = resolveThemeCookieDomain(location.hostname);
  try {
    if (sharedDomain) {
      document.cookie = `${THEME_COOKIE_NAME}=; Path=/; Max-Age=0; SameSite=Lax${secureFlag}`;
      document.cookie = `${THEME_COOKIE_NAME}=${encodeURIComponent(pref)}; Path=/; Domain=${sharedDomain}; Max-Age=${THEME_COOKIE_MAX_AGE}; SameSite=Lax${secureFlag}`;
    } else {
      document.cookie = `${THEME_COOKIE_NAME}=${encodeURIComponent(pref)}; Path=/; Max-Age=${THEME_COOKIE_MAX_AGE}; SameSite=Lax${secureFlag}`;
    }
  } catch {
    // ignore cookie errors
  }
}

function readThemePreference() {
  const fromCookie = readThemeCookie();
  if (fromCookie) return fromCookie;
  try {
    const fromStorage = parseThemePreference(localStorage.getItem(THEME_STORAGE_KEY));
    if (fromStorage) return fromStorage;
  } catch {
    // ignore storage errors
  }
  return 'system';
}

function isSystemDark() {
  return Boolean(typeof matchMedia === 'function' && matchMedia('(prefers-color-scheme: dark)').matches);
}

function resolveTheme(pref = themePreference) {
  if (pref === 'dark') return 'dark';
  if (pref === 'light') return 'light';
  return isSystemDark() ? 'dark' : 'light';
}

function renderThemeSwitchers() {
  const resolved = resolveTheme(themePreference);
  const triggerIcon = resolved === 'dark' ? 'moon' : 'sun';
  const label = `切换主题（当前：${THEME_LABELS[themePreference] || '跟随系统'}）`;
  $$('[data-theme-switcher]').forEach(root => {
    const trigger = $('.theme-switcher-trigger', root);
    const menu = $('.theme-switcher-menu', root);
    if (trigger) {
      trigger.innerHTML = icon(triggerIcon);
      trigger.setAttribute('aria-label', label);
      trigger.setAttribute('title', label);
    }
    if (menu) {
      menu.innerHTML = THEME_OPTIONS.map(opt => {
        const selected = themePreference === opt.value;
        return `<button type="button" role="menuitemradio" aria-checked="${selected}" class="theme-switcher-option" data-theme-option="${opt.value}">${icon(opt.icon)}<span>${opt.label}</span>${selected ? icon('check', 'theme-switcher-check') : ''}</button>`;
      }).join('');
    }
  });
}

function applyThemeToDocument(pref = themePreference) {
  themePreference = parseThemePreference(pref) || 'system';
  const resolved = resolveTheme(themePreference);
  const root = document.documentElement;
  root.dataset.theme = resolved;
  root.dataset.themePreference = themePreference;
  root.style.colorScheme = resolved;
  const themeColorMeta = $('meta[name="theme-color"]');
  if (themeColorMeta) themeColorMeta.setAttribute('content', resolved === 'dark' ? THEME_COLOR_DARK : THEME_COLOR_LIGHT);
  const colorSchemeMeta = $('meta[name="color-scheme"]');
  if (colorSchemeMeta) colorSchemeMeta.setAttribute('content', resolved);
  renderThemeSwitchers();
  return resolved;
}

function setThemePreference(next) {
  const pref = parseThemePreference(next) || 'system';
  try {
    localStorage.setItem(THEME_STORAGE_KEY, pref);
  } catch {
    // ignore storage errors
  }
  writeThemeCookie(pref);
  applyThemeToDocument(pref);
}

function setThemeMenuOpen(root, open) {
  if (!root) return;
  const trigger = $('.theme-switcher-trigger', root);
  const menu = $('.theme-switcher-menu', root);
  if (!trigger || !menu) return;
  if (open) {
    closeAllThemeMenus(root);
    setAccountMenuOpen(false);
  }
  root.dataset.open = open ? 'true' : 'false';
  menu.dataset.open = open ? 'true' : 'false';
  trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
  menu.inert = !open;
}

function closeAllThemeMenus(except = null) {
  $$('[data-theme-switcher][data-open="true"]').forEach(root => {
    if (root !== except) setThemeMenuOpen(root, false);
  });
}

function initThemeManager() {
  const pref = readThemePreference();
  try {
    if (localStorage.getItem(THEME_STORAGE_KEY) !== pref) {
      localStorage.setItem(THEME_STORAGE_KEY, pref);
    }
  } catch {
    // ignore storage errors
  }
  applyThemeToDocument(pref);
  if (typeof matchMedia === 'function' && !themeMediaQuery) {
    themeMediaQuery = matchMedia('(prefers-color-scheme: dark)');
    const onMediaChange = () => {
      if (themePreference === 'system') applyThemeToDocument('system');
    };
    if (typeof themeMediaQuery.addEventListener === 'function') {
      themeMediaQuery.addEventListener('change', onMediaChange);
    } else if (typeof themeMediaQuery.addListener === 'function') {
      themeMediaQuery.addListener(onMediaChange);
    }
  }
  window.addEventListener('storage', event => {
    if (event.key !== THEME_STORAGE_KEY) return;
    const next = parseThemePreference(event.newValue) || 'system';
    writeThemeCookie(next);
    applyThemeToDocument(next);
  });
}

function setAccountMenuOpen(open) {
  const root = $('#account-dropdown');
  const trigger = $('#account-trigger');
  const menu = $('#account-menu');
  if (!root || !trigger || !menu) return;
  if (open) closeAllThemeMenus();
  root.dataset.open = open ? 'true' : 'false';
  menu.dataset.open = open ? 'true' : 'false';
  trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
  menu.inert = !open;
}

function hydrateStaticChrome() {
  initThemeManager();
  $('#ai-launcher-icon').innerHTML = icon('spark');
  $('#refresh-ledger').innerHTML = icon('refresh', 'ico-refresh');
  $('#account-chevron').innerHTML = icon('chevronDown');
  $$('[data-close], [data-confirm-cancel]').forEach(btn => {
    if (btn.classList.contains('icon-button') && !btn.innerHTML.trim()) {
      btn.innerHTML = icon('close');
    }
  });
  $('#profile-button').innerHTML = `${icon('user')}<span>个人资料</span>`;
  $('#menu-vanora-account').innerHTML = `${icon('arrowUpRight')}<span>Vanora 账户中心</span>`;
  $('#menu-logout').innerHTML = `${icon('logout')}<span>退出登录</span>`;
  if (feishuEnv.isFeishu) {
    const envBadge = $('#feishu-env-badge');
    if (envBadge) {
      envBadge.textContent = feishuEnv.isMobile ? '飞书' : '飞书桌面端';
      envBadge.hidden = false;
    }
  }
}

function renderNav() {
  const nav = $('#navigation');
  if (!nav) return;
  const pendingCount = state.entries.filter(e => e.status === 'pending').length;
  const existingLinks = $$('a[data-nav-page]', nav);
  if (existingLinks.length !== Object.keys(PAGES).length) {
    const linksHtml = Object.entries(PAGES).map(([key, [name]]) =>
      `<a href="#${key}" data-nav-page="${key}" class="${state.page === key ? 'active' : ''}" ${state.page === key ? 'aria-current="page"' : ''}><span>${esc(name)}</span>${key === 'reimbursements' ? `<small class="nav-count" ${pendingCount ? '' : 'hidden'}>${pendingCount}</small>` : ''}</a>`
    ).join('');
    nav.innerHTML = linksHtml + '<span class="console-topnav-indicator" id="nav-indicator" aria-hidden="true"></span>';
    if (typeof ResizeObserver !== 'undefined') {
      if (navObserver) navObserver.disconnect();
      navObserver = new ResizeObserver(updateNavIndicator);
      navObserver.observe(nav);
      nav.querySelectorAll('a').forEach(a => navObserver.observe(a));
    }
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(updateNavIndicator);
    }
  } else {
    existingLinks.forEach(a => {
      const active = a.dataset.navPage === state.page;
      a.classList.toggle('active', active);
      if (active) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
      if (a.dataset.navPage === 'reimbursements') {
        const badgeEl = $('.nav-count', a);
        if (badgeEl) {
          badgeEl.textContent = String(pendingCount);
          badgeEl.hidden = !pendingCount;
        }
      }
    });
  }
  updateNavIndicator();
  scrollActiveNavIntoView();
}

function parseHashRoute() {
  const raw = location.hash.slice(1);
  const entryMatch = /^entry\/(\d+)$/.exec(raw);
  if (entryMatch) {
    state.page = 'ledger';
    state.pendingEntryId = Number(entryMatch[1]);
    return;
  }
  if (raw === 'calculator' || raw === 'reimbursements/calculator') {
    state.page = 'reimbursements';
    state.reimburseMode = 'calculator';
    return;
  }
  if (raw === 'reimbursements' || raw === 'reimbursements/queue') {
    state.page = 'reimbursements';
    state.reimburseMode = 'queue';
    return;
  }
  if (raw === 'members') {
    state.page = 'settings';
    state.settingsTab = 'members';
    return;
  }
  if (raw === 'integrations') {
    state.page = 'settings';
    state.settingsTab = isOwner() ? 'bots-ai' : 'my-bots';
    return;
  }
  const settingsMatch = /^settings\/([a-z-]+)$/.exec(raw);
  if (settingsMatch) {
    state.page = 'settings';
    const sub = settingsMatch[1];
    state.settingsTab = sub === 'bots' ? 'bots-ai' : sub;
    return;
  }
  state.page = hasOwn(PAGES, raw) ? raw : 'overview';
}

function render() {
  if (!state.user || !state.conf) return;
  $('#content').innerHTML = renderers[state.page]();
  if (state.page === 'overview') hydrateChart();
  if (state.page === 'settings') {
    if (state.settingsTab === 'backups') mountBackups();
    const activeSetting = $('.settings-sidebar .settings-nav-item.active');
    if (activeSetting && typeof activeSetting.scrollIntoView === 'function') {
      activeSetting.scrollIntoView({inline: 'nearest', block: 'nearest'});
    }
  }
  renderNav();
  const teamName = state.conf.team_name || 'Vanora 财务管理';
  const initial = [...state.user.name][0] || 'T';
  $('#my-avatar').textContent = initial;
  $('#menu-avatar').textContent = initial;
  $('#my-name').textContent = state.user.name;
  $('#my-role').textContent = `@${state.user.username} · ${roleName(state.user.role)}`;
  $('#menu-team-meta').textContent = `${teamName} · ${state.conf.today}`;
  const accountLink = $('#menu-vanora-account');
  if (accountLink) {
    if (state.accountUrl) {
      accountLink.href = state.accountUrl;
      accountLink.hidden = false;
    } else {
      accountLink.hidden = true;
    }
  }
  $('#top-user').textContent = state.user.name;
  $('#footer-brand').textContent = `© ${new Date().getFullYear()} ${teamName} · Vanora 财务管理`;
  document.title = `${PAGES[state.page][0]} · Vanora 财务管理`;
}

function isEmbedded() {
  try { return window.self !== window.top; } catch { return true; }
}

function safeReturnHash() {
  const hash = location.hash || '';
  return RETURN_HASH_RE.test(hash) ? hash : '';
}

function consumeAuthQueryParams() {
  const params = new URLSearchParams(location.search);
  const errCode = (params.get('auth_error') || '').trim();
  if (errCode) {
    state.authError = AUTH_ERRORS[errCode] || '登录失败，请稍后重试。';
    params.delete('auth_error');
    const qs = params.toString();
    history.replaceState(null, '', location.pathname + (qs ? '?' + qs : '') + location.hash);
  }
}

function buildOidcLoginUrl() {
  const params = new URLSearchParams();
  if (state.boot?.needs_setup) {
    const teamName = $('#auth-team-name')?.value.trim() || '';
    if (teamName) params.set('team_name', teamName);
  }
  const nextHash = safeReturnHash();
  if (nextHash) params.set('next', nextHash);
  const qs = params.toString();
  return '/api/auth/oidc/login' + (qs ? '?' + qs : '');
}

function syncAuthExternalLink() {
  const link = $('#auth-external-link');
  if (link) link.href = buildOidcLoginUrl();
}

function navigateExternal(url) {
  if (isEmbedded()) {
    try {
      window.top.location.assign(url);
      return;
    } catch {
      const opened = window.open(url, '_blank', 'noopener,noreferrer');
      if (opened) return;
    }
  }
  location.assign(url);
}

function openProfileDialog() {
  if (!state.user) return;
  const u = state.user;
  $('#profile-identity').innerHTML = `
    <div class="profile-identity-row"><span>账本账号</span><strong>@${esc(u.username)} · ${esc(roleName(u.role))}</strong></div>
    <div class="profile-identity-row"><span>Vanora 账号</span><span class="badge ${u.oidc_linked ? 'good' : 'warn'}">${u.oidc_linked ? '已绑定' : '未绑定'}</span></div>
    <div class="profile-identity-row"><span>Vanora 邮箱</span><strong>${u.oidc_email ? esc(u.oidc_email) : '未设置'}</strong></div>`;
  $('#profile-name').value = u.name || '';
  $('#profile-error').textContent = '';
  const accountPanel = $('#profile-account-panel');
  const accountLink = $('#vanora-account-link');
  if (state.accountUrl) {
    accountLink.href = state.accountUrl;
    accountLink.innerHTML = `前往 Vanora 账户中心 ${icon('arrowUpRight', 'icon-sm')}`;
    accountPanel.hidden = false;
  } else {
    accountPanel.hidden = true;
  }
  $('#profile-dialog').showModal();
  setTimeout(() => $('#profile-name')?.focus(), 40);
}

async function refresh(renderAfter = true) {
  const loadId = ++state.loadId;
  ++state.calculator.request;
  state.calculator.report = null;
  const [conf, members, entries, integrations] = await Promise.all([
    api('/settings'), api('/members'), api('/entries'), api('/integrations'),
  ]);
  if (loadId !== state.loadId) return;
  state.conf = conf;
  state.members = members;
  state.entries = entries;
  state.integrations = integrations;
  const pendingIds = new Set(entries.filter(e => e.kind === 'expense' && e.status === 'pending').map(e => e.id));
  state.calculator.ids = new Set([...state.calculator.ids].filter(id => pendingIds.has(id)));
  try {
    state.report = await api('/report' + (state.base ? '?base=' + encodeURIComponent(state.base) : ''));
  } catch (e) {
    state.report = null;
    toast(e.message, true);
  }
  if (loadId !== state.loadId) return;
  if (renderAfter) {
    render();
    if (state.page === 'reimbursements' && state.reimburseMode === 'calculator') await calculateFunds();
    if (state.pendingEntryId) {
      const targetId = state.pendingEntryId;
      state.pendingEntryId = null;
      if (state.entries.some(e => e.id === targetId)) {
        openDetail(targetId);
      } else {
        toast(`未找到账目 #${targetId}`, true);
      }
    }
  }
}

async function showAuth(preloadedBoot = null) {
  const boot = preloadedBoot && typeof preloadedBoot === 'object' ? preloadedBoot : await api('/bootstrap');
  state.boot = boot;
  document.documentElement.classList.remove('app-shell-active');
  setAccountMenuOpen(false);
  closeAllThemeMenus();
  $('#loading').hidden = true;
  $('#app').hidden = true;
  $('#auth').hidden = false;

  const page = $('#auth-page');
  const ctx = $('#auth-context');
  const teamWrap = $('#auth-team-wrap');
  const submitBtn = $('#auth-submit');
  const recheckBtn = $('#auth-recheck');
  const embedHint = $('#auth-embed-hint');
  const errorBox = $('#auth-error');
  const noteBox = $('#auth-note');

  page.classList.remove('is-wide');
  ctx.hidden = true;
  ctx.innerHTML = '';
  teamWrap.hidden = true;
  submitBtn.hidden = false;
  submitBtn.disabled = false;
  recheckBtn.hidden = true;
  embedHint.hidden = true;
  errorBox.textContent = state.authError || '';
  state.authError = '';

  if (!boot.oidc_configured) {
    page.classList.add('is-wide');
    $('#auth-title').textContent = '尚未配置 Vanora OAuth';
    $('#auth-subtitle').textContent = '请先配置机密 OIDC 客户端与环境变量后重启服务。';
    ctx.innerHTML = `
      <div class="auth-card">
        <div class="auth-card-title"><span>客户端接入参数</span><span class="badge warn">待配置</span></div>
        <p>启用 Standard Flow、PKCE S256、客户端角色（<code>owner</code> / <code>finance</code> / <code>member</code>）与 <code>vanora.permissions.sync.enabled=true</code>。</p>
        <div class="auth-config-list">
          <div class="auth-config-item">
            <span>Valid redirect URI</span>
            <div class="copy-field"><code>${esc(boot.callback_url || '')}</code>${copyButtonHtml(boot.callback_url || '')}</div>
          </div>
          <div class="auth-config-item">
            <span>Backchannel logout URL</span>
            <div class="copy-field"><code>${esc(boot.backchannel_logout_url || '')}</code>${copyButtonHtml(boot.backchannel_logout_url || '')}</div>
          </div>
        </div>
      </div>`;
    ctx.hidden = false;
    submitBtn.hidden = true;
    recheckBtn.hidden = false;
    noteBox.textContent = '配置并重启后，点击上方重新检测状态。';
    return;
  }

  if (boot.needs_setup) {
    $('#auth-title').textContent = '启用团队账本';
    $('#auth-subtitle').textContent = '使用已授权的 Vanora 账号登录并完成初始化。';
    teamWrap.hidden = false;
    submitBtn.textContent = '使用 Vanora 账号登录';
    noteBox.textContent = '账本所有者首次登录时可一并设定团队名称。';
  } else {
    $('#auth-title').textContent = '欢迎回来';
    $('#auth-subtitle').textContent = '使用 Vanora 账号登录，继续管理团队收支与报销。';
    submitBtn.textContent = '使用 Vanora 账号登录';
    noteBox.textContent = '成员与权限由 Vanora OAuth 统一管理。';
  }

  syncAuthExternalLink();
  embedHint.hidden = !isEmbedded();
}

async function enterApp(data) {
  state.user = data.user;
  state.csrf = data.csrf;
  state.accountUrl = data.account_url || state.boot?.account_url || '';
  state.adminUrl = data.admin_url || state.boot?.admin_url || '';
  document.documentElement.classList.add('app-shell-active');
  $('#auth').hidden = true;
  $('#app').hidden = false;
  $('#loading').hidden = true;
  parseHashRoute();
  $('#content').innerHTML = '<div class="skeleton">正在整理团队账本…</div>';
  await refresh();
}

async function start() {
  hydrateStaticChrome();
  consumeAuthQueryParams();
  try {
    const boot = await api('/bootstrap');
    state.boot = boot;
    if (boot.needs_setup) {
      await showAuth(boot);
      return;
    }
    try {
      await enterApp(await api('/session'));
    } catch (e) {
      if (state.user) throw e;
      await showAuth(boot);
    }
  } catch (e) {
    const loading = $('#loading');
    if (loading) {
      loading.className = 'console-access';
      loading.removeAttribute('aria-busy');
      loading.innerHTML = `<h1>无法连接到账本服务</h1><p>${esc(e.message)}</p><button type="button" class="button button-outline" data-reload>重新连接</button>`;
    }
  }
}

/* ============================================================================
 * Entry, Detail Drawer, Receipt & Payment Modals
 * ========================================================================== */

function openEntry(kind = 'expense', data = null) {
  if ((!canManage() && kind !== 'expense') || (data && !canManageEntry(data))) return;
  const form = $('#entry-form');
  form.reset();
  state.editing = data?.id || null;
  state.entryKey = randomId();
  state.entryKind = data?.kind === 'expense' && data.expense_type === 'public' ? 'public' : data?.kind || kind;
  form.elements.currency.innerHTML = currencyOptions(data?.currency || state.conf.default_currency);
  form.elements.payer_id.innerHTML = options(state.members.filter(m => m.active), m => m.id, m => m.name, data?.payer_id || state.user.id);
  form.elements.payer_id.disabled = !canManage();
  form.elements.date.value = data?.date || state.conf.today;
  form.elements.amount.value = data?.amount || '';
  form.elements.description.value = data?.description || '';
  form.elements.note.value = data?.note || '';
  $('#entry-kind').hidden = Boolean(data?.id);
  $$('#entry-kind button').forEach(b => { b.hidden = b.dataset.kind !== 'expense' && !canManage(); });
  $('#entry-error').textContent = '';
  $('#upload-label').innerHTML = '添加发票或凭证 <small>PNG / JPG / PDF，上限 6 MB</small>';
  entryKindChanged();
  $('#entry-dialog').showModal();
  setTimeout(() => form.elements.amount.focus(), 40);
}

function entryKindChanged() {
  const income = state.entryKind === 'income', publicExpense = state.entryKind === 'public';
  $$('#entry-kind button').forEach(b => {
    const active = b.dataset.kind === state.entryKind;
    b.classList.toggle('active', active);
    b.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  $('#entry-title').textContent = state.editing ? '修改账目' : income ? '登记团队收入' : publicExpense ? '记公账支出' : '记成员垫付';
  $('#save-draft').hidden = income;
  $('#payer-label > span').textContent = income || publicExpense ? '经手成员' : '垫付成员';
  $('#save-entry').textContent = income ? '确认入账' : '确认记账';
}

async function saveEntry(status, button) {
  const form = $('#entry-form');
  if (!form.reportValidity()) return;
  await busy(button, async () => {
    $('#entry-error').textContent = '';
    try {
      const file = form.elements.receipt.files[0];
      if (file && file.size > 6 * 1024 * 1024) throw new Error('凭证大小不能超过 6 MB');
      const body = Object.fromEntries(new FormData(form).entries());
      delete body.receipt;
      body.payer_id = Number(form.elements.payer_id.value);
      body.kind = state.entryKind === 'income' ? 'income' : 'expense';
      body.expense_type = state.entryKind === 'public' ? 'public' : state.entryKind === 'expense' ? 'advance' : '';
      body.status = state.entryKind === 'income' || (state.entryKind === 'public' && status !== 'draft') ? 'posted' : status;
      const entry = await api(state.editing ? `/entries/${state.editing}` : '/entries', state.editing ? 'PUT' : 'POST', body, {'Idempotency-Key': state.entryKey});
      if (file) {
        try {
          const data = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(String(reader.result).split(',')[1]);
            reader.onerror = () => reject(new Error('读取凭证失败'));
            reader.readAsDataURL(file);
          });
          await api(`/entries/${entry.id}/receipt`, 'POST', {data, name: file.name});
        } catch (e) {
          toast(`账目 #${entry.id} 已保存，但凭证上传失败：${e.message}`, true);
        }
      }
      $('#entry-dialog').close();
      toast(`已${state.entryKind === 'income' ? '登记收入' : status === 'draft' ? '保存草稿' : '记账'} · #${entry.id}`);
      await refresh();
    } catch (e) {
      $('#entry-error').textContent = e.message;
    }
  });
}

async function openDetail(id) {
  const e = state.entries.find(x => x.id === id);
  if (!e) return;
  $('#detail-content').innerHTML = `<div class="modal-heading"><h2>账目 #${e.id}</h2><button type="button" class="icon-button" data-close aria-label="关闭">${icon('close')}</button></div><div class="drawer-body"><div class="skeleton">正在读取账目详情…</div></div>`;
  const dialog = $('#detail-dialog');
  if (!dialog.open) dialog.showModal();
  try {
    const events = await api(`/entries/${id}/events`);
    const manageable = canManageEntry(e);
    const actions = [];
    if (manageable && e.kind === 'expense' && !e.reimbursed_minor && (e.expense_type === 'public' ? ['draft', 'posted'] : ['draft', 'pending']).includes(e.status)) {
      actions.push(`<button type="button" class="button button-outline" data-edit-entry="${id}">修改</button>`);
    }
    if (!e.reimbursed_minor && !['reimbursed', 'voided'].includes(e.status) && manageable) {
      actions.push(`<button type="button" class="button button-outline button-danger-outline" data-action="void" data-id="${id}">作废</button>`);
    }
    if (e.status === 'voided' && canManage()) {
      actions.push(`<button type="button" class="button button-danger" data-delete-entry="${id}">永久删除</button>`);
    }
    if (manageable && e.status === 'draft') {
      actions.push(`<button type="button" class="button button-primary" data-action="submit" data-id="${id}">记入账本</button>`);
    }
    if (canManage() && e.status === 'pending') {
      actions.push(`<button type="button" class="button button-primary" data-action="pay" data-id="${id}">登记已付款</button>`);
    }
    const actionNames = {
      migrated: '迁移了历史公账支出', created: '创建了账目', submit: '将草稿记入账本',
      approve: '历史记录：通过', reject: '历史记录：退回', pay: '登记了报销付款',
      edited: '修改了账目', receipt: '上传了凭证', void: '作废了账目',
    };
    const deepUrl = `${location.origin}/#entry/${e.id}`;
    $('#detail-content').innerHTML = `
      <div class="modal-heading">
        <h2>账目详情 <span class="mono muted">#${e.id}</span></h2>
        <div class="modal-heading-actions">
          ${copyButtonHtml(deepUrl, '复制链接')}
          <button type="button" class="icon-button" data-close aria-label="关闭">${icon('close')}</button>
        </div>
      </div>
      <div class="drawer-body drawer-cards-body">
        <section class="drawer-card drawer-hero-card">
          <div class="badge-row">
            ${badge(entryStatus(e))}
            <span class="badge neutral">${entryType(e)}</span>
          </div>
          <h3 class="detail-amount ${e.kind === 'income' ? 'income' : ''}">${e.kind === 'income' ? '+' : '−'} ${money(e.amount, e.currency)}</h3>
          ${settlementDisplay(e)}
          <p class="detail-desc">${esc(entryDescription(e))}</p>
          ${paymentSummary(e)}
        </section>

        <section class="drawer-card">
          <h4>基本信息</h4>
          <dl class="detail-list">
            <div><dt>${e.kind === 'income' || e.expense_type === 'public' ? '经手成员' : '垫付成员'}</dt><dd>${esc(e.payer_name)}</dd></div>
            <div><dt>发生日期</dt><dd class="mono">${esc(e.date)}</dd></div>
            <div><dt>记录来源</dt><dd>${esc(sourceLabel(e.source))} · ${esc(e.creator_name)}</dd></div>
            <div><dt>凭证文件</dt><dd>${e.receipt ? receiptLink(e, e.receipt_name || '查看凭证') : '未附凭证'}</dd></div>
            <div><dt>备注</dt><dd>${esc(e.note || '—')}</dd></div>
            ${e.review_note ? `<div><dt>处理备注</dt><dd>${esc(e.review_note)}</dd></div>` : ''}
          </dl>
          ${manageable && (canManage() || !e.reimbursed_minor) && ['draft', 'pending', 'posted'].includes(e.status) ? `<label class="upload-zone detail-upload-zone"><input type="file" id="detail-receipt" data-id="${id}" accept="image/png,image/jpeg,application/pdf,.png,.jpg,.jpeg,.pdf"><span>${e.receipt ? '替换凭证文件' : '补充发票或凭证'} <small>PNG / JPG / PDF，上限 6 MB</small></span></label>` : ''}
        </section>

        ${paymentHistory(e)}

        <section class="drawer-card">
          <h4>操作记录</h4>
          <div class="timeline">${events.map(event => `<div class="timeline-item"><strong>${esc(event.name)}</strong> ${esc(actionNames[event.action] || event.action)}${event.detail ? `<br>${esc(event.detail)}` : ''}<small>${esc(event.created_at.replace('T', ' ').slice(0, 19))}</small></div>`).join('')}</div>
        </section>
      </div>
      ${actions.length ? `<div class="drawer-footer">${actions.join('')}</div>` : ''}`;
  } catch (err) {
    $('#detail-content').innerHTML = `<div class="modal-heading"><h2>账目详情</h2><button type="button" class="icon-button" data-close aria-label="关闭">${icon('close')}</button></div><div class="drawer-body">${empty('加载失败', err.message, '')}</div>`;
  }
}

function openReceipt(id) {
  const entry = state.entries.find(e => e.id === id);
  if (!entry?.receipt) return;
  const dialog = $('#receipt-dialog'), image = $('#receipt-image'), pdf = $('#receipt-pdf'), status = $('#receipt-status'), zoom = $('[data-receipt-zoom]');
  const url = `/api/entries/${id}/receipt`;
  const downloadUrl = `/api/entries/${id}/receipt?download=1`;
  const isImage = entry.receipt_type?.startsWith('image/');
  const isPdf = entry.receipt_type === 'application/pdf';
  $('#receipt-name').textContent = entry.receipt_name || (isPdf ? 'PDF 凭证' : '凭证图片');
  $('#receipt-original').href = url;
  $('#receipt-download').href = downloadUrl;
  $('#receipt-download').setAttribute('download', entry.receipt_name || `receipt-${id}`);
  $('#receipt-stage').classList.remove('is-zoomed');
  image.onload = image.onerror = null;
  image.removeAttribute('src');
  image.hidden = true;
  pdf.removeAttribute('src');
  pdf.hidden = true;
  if (isImage) {
    zoom.hidden = false;
    zoom.textContent = '查看原尺寸';
    zoom.setAttribute('aria-pressed', 'false');
    zoom.disabled = true;
    status.textContent = '正在加载凭证图片…';
    status.hidden = false;
    image.alt = entryDescription(entry) + '的凭证';
    image.onload = () => {
      image.hidden = false;
      status.hidden = true;
      zoom.disabled = false;
    };
    image.onerror = () => {
      status.textContent = '图片加载失败，请尝试下载或在新窗口打开。';
    };
    image.src = url;
  } else if (isPdf) {
    zoom.hidden = true;
    if (feishuEnv.isFeishu && feishuEnv.isMobile) {
      status.textContent = '移动端内置浏览器不支持直接渲染 PDF，请下载或在新窗口打开。';
      status.hidden = false;
    } else {
      status.hidden = true;
      pdf.src = url;
      pdf.hidden = false;
    }
  } else {
    zoom.hidden = true;
    status.textContent = '暂不支持内嵌预览，请下载查看。';
    status.hidden = false;
  }
  if (!dialog.open) dialog.showModal();
}

$('#receipt-dialog').addEventListener('close', () => {
  const image = $('#receipt-image'), pdf = $('#receipt-pdf');
  image.onload = image.onerror = null;
  image.removeAttribute('src');
  pdf.removeAttribute('src');
});

async function openAction(action, ids) {
  if (!ids.length) {
    toast('请先选择要处理的账目', true);
    return;
  }
  const rows = state.entries.filter(e => ids.includes(e.id));
  const labels = {submit: '记入账本', pay: '登记已付款', void: '作废账目'};
  state.action = {action, ids, key: randomId(), quotes: {}, loading: 0};
  const dialog = $('#action-dialog');
  dialog.classList.toggle('has-payments', action === 'pay');
  $('#action-form').reset();
  $('#action-title').textContent = labels[action];
  const confirmBtn = $('#action-confirm');
  confirmBtn.textContent = `确认${labels[action]}`;
  confirmBtn.className = `button ${action === 'void' ? 'button-danger' : 'button-primary'}`;
  $('#action-description').textContent = action === 'pay'
    ? '填写本次实际转账金额，支持分次与跨币种登记（不触发自动转账）。'
    : `即将对 ${ids.length} 笔账目执行${labels[action]}。`;
  $('#action-totals').innerHTML = action === 'pay'
    ? `<span>剩余待报销：${groupedTotals(rows, 'remaining_amount')}</span>`
    : `<span>${groupedTotals(rows)}</span>`;
  $('#action-note-label').hidden = action === 'submit';
  const base = state.conf.reference_currency;
  $('#action-payment-amounts').innerHTML = action === 'pay'
    ? rows.map(e => `<div class="payment-item"><strong>#${e.id} ${esc(entryDescription(e))}</strong><small>${esc(e.payer_name)} · 原金额 ${money(e.amount, e.currency)} · 剩余 ${money(e.remaining_amount, e.currency)}</small><label><span>付款币种</span><select data-payment-currency="${e.id}"><option value="${esc(e.currency)}">记账货币 · ${esc(e.currency)}</option>${base !== e.currency ? `<option value="${esc(base)}">结算货币 · ${esc(base)}</option>` : ''}</select></label><label><span>本次实付金额</span><input name="payment-${e.id}" data-payment="${e.id}" type="number" inputmode="decimal" required disabled></label><small data-payment-rate="${e.id}"></small><div class="payment-after" data-payment-after="${e.id}"></div></div>`).join('')
    : '';
  $('#action-note-label > span').textContent = action === 'pay' ? '付款方式 / 参考号' : action === 'void' ? '作废原因' : '备注';
  $('#action-form [name=note]').required = ['pay', 'void'].includes(action);
  $('#action-error').textContent = '';
  confirmBtn.disabled = action === 'pay';
  dialog.showModal();
  if (action === 'pay') await Promise.all(rows.map(e => loadPaymentQuote(e.id, e.currency)));
}

async function loadPaymentQuote(id, code) {
  const action = state.action;
  action.loading++;
  delete action.quotes[id];
  $('#action-confirm').disabled = true;
  const input = $(`[data-payment="${id}"]`), rateLabel = $(`[data-payment-rate="${id}"]`);
  input.disabled = true;
  rateLabel.textContent = '正在取得付款换算…';
  $('#action-error').textContent = '';
  try {
    const [quote] = await api('/entries/payment-quotes', 'POST', {ids: [id], currency: code});
    if (state.action !== action || $(`[data-payment-currency="${id}"]`).value !== code) return;
    action.quotes[id] = quote;
    input.step = 10 ** -quote.decimals;
    input.min = input.step;
    input.max = quote.maximum_amount;
    input.value = quote.maximum_amount;
    input.disabled = false;
    input.setAttribute('aria-label', `本次已报销金额 ${quote.currency}`);
    rateLabel.textContent = `上限 ${quote.maximum_amount} ${quote.currency}` + (quote.currency !== quote.entry_currency ? ` · 1 ${quote.entry_currency} = ${quote.rate} ${quote.currency} (${quote.rate_date})` : '');
  } catch (e) {
    if (state.action === action) rateLabel.textContent = e.message;
  } finally {
    action.loading--;
    if (state.action === action) {
      $('#action-confirm').disabled = action.loading > 0 || Object.keys(action.quotes).length !== action.ids.length;
      updatePaymentPreview();
    }
  }
}

function updatePaymentPreview() {
  $$('[data-payment]').forEach(input => {
    const e = state.entries.find(item => item.id === Number(input.dataset.payment));
    const value = Number(input.value), quote = state.action?.quotes[e.id];
    const valid = quote && input.value !== '' && Number.isFinite(value) && value > 0 && value <= Number(quote.maximum_amount);
    const allocation = quote ? (value === Number(quote.maximum_amount) ? e.remaining_minor : Math.round(value / Number(quote.rate) * 10 ** e.decimals)) / 10 ** e.decimals : 0;
    $(`[data-payment-after="${e.id}"]`).innerHTML = valid
      ? `抵扣 ${money(allocation, e.currency)} · 登记后剩余 ${money(Math.max(0, Number(e.remaining_amount) - allocation), e.currency)}`
      : '请输入大于 0 且不超过剩余待报销的金额';
  });
}

$('#action-payment-amounts').addEventListener('input', updatePaymentPreview);
$('#action-payment-amounts').addEventListener('change', event => {
  if (event.target.matches('[data-payment-currency]')) {
    loadPaymentQuote(Number(event.target.dataset.paymentCurrency), event.target.value);
  }
});

/* ============================================================================
 * Backdrop & Dropdown Dismissal
 * ========================================================================== */

$$('dialog.modal').forEach(dialog => {
  const allowBackdropClose = dialog.classList.contains('modal-drawer') || dialog.id === 'receipt-dialog';
  let backdropDown = false;
  dialog.addEventListener('pointerdown', event => {
    backdropDown = allowBackdropClose && event.target === dialog;
  });
  dialog.addEventListener('click', event => {
    if (allowBackdropClose && backdropDown && event.target === dialog) {
      dialog.close();
    }
    backdropDown = false;
  });
});

document.addEventListener('pointerdown', event => {
  const dropdown = $('#account-dropdown');
  if (dropdown?.dataset.open === 'true' && !dropdown.contains(event.target)) {
    setAccountMenuOpen(false);
  }
  $$('[data-theme-switcher][data-open="true"]').forEach(switcher => {
    if (!switcher.contains(event.target)) setThemeMenuOpen(switcher, false);
  });
});

/* ============================================================================
 * Excel Export Dialog (Preset-First + Collapsible Custom Filters)
 * ========================================================================== */

function localIsoDate(d = new Date()) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function exportSpecFromForm() {
  const form = $('#export-form');
  const ids = state.exportState.ids || [];
  return {
    ids,
    from: form.elements.from.value.trim(),
    to: form.elements.to.value.trim(),
    kind: form.elements.kind.value || 'all',
    status: form.elements.status.value || 'all',
    currency: form.elements.currency.value || 'all',
    payer: form.elements.payer.value || 'all',
    q: form.elements.q.value.trim(),
    base: form.elements.base.value || state.base || state.conf.reference_currency,
    include_receipts: Boolean(form.elements.include_receipts.checked),
  };
}

function exportMatchingEntries(spec) {
  const idSet = spec.ids?.length ? new Set(spec.ids.map(Number)) : null;
  const needle = (spec.q || '').toLowerCase();
  return state.entries.filter(e => {
    if (idSet) return idSet.has(e.id);
    if (spec.kind === 'income' && e.kind !== 'income') return false;
    if (spec.kind === 'public' && !(e.kind === 'expense' && e.expense_type === 'public')) return false;
    if (spec.kind === 'advance' && !(e.kind === 'expense' && e.expense_type === 'advance')) return false;
    if (spec.currency !== 'all' && e.currency !== spec.currency) return false;
    if (spec.payer !== 'all' && e.payer_id !== Number(spec.payer)) return false;
    if (spec.status === 'all') {
      if (e.status === 'voided') return false;
    } else if (spec.status === 'all_with_voided') {
      // Include voided entries.
    } else if (spec.status === 'partial') {
      if (entryStatus(e) !== 'partial') return false;
    } else if (e.status !== spec.status) {
      return false;
    }
    if (spec.from && e.date < spec.from) return false;
    if (spec.to && e.date > spec.to) return false;
    if (needle) {
      const hay = [`#${e.id}`, e.description, e.note, e.payer_name, e.review_note].join(' ').toLowerCase();
      if (!hay.includes(needle)) return false;
    }
    return true;
  });
}

function renderExportPresets() {
  const wrap = $('#export-presets');
  if (!wrap) return;
  const presets = [];
  if (state.exportState.source === 'ledger' && ledgerHasFilter()) {
    presets.push(['current_filter', '当前筛选']);
  }
  if (state.exportState.ids.length) {
    presets.push(['selected', `已勾选 ${state.exportState.ids.length} 笔`]);
  }
  presets.push(
    ['all', '全部有效'],
    ['month', '本月'],
    ['last_month', '上月'],
    ['last30', '近 30 天'],
    ['pending', '待报销'],
    ['all_with_voided', '含作废'],
  );
  wrap.innerHTML = presets.map(([key, label]) =>
    `<button type="button" class="export-preset${state.exportState.activePreset === key ? ' active' : ''}" data-export-preset="${key}">${esc(label)}</button>`
  ).join('');
}

function updateExportPreview() {
  const form = $('#export-form');
  if (!form) return;
  const errEl = $('#export-error');
  const previewEl = $('#export-preview');
  const bannerEl = $('#export-selection-banner');
  const detailsEl = $('#export-custom-details');
  const summaryEl = $('#export-custom-summary');
  const submitEl = $('#export-submit');
  const spec = exportSpecFromForm();
  const hasIds = spec.ids.length > 0;

  bannerEl.hidden = !hasIds;
  if (detailsEl) detailsEl.hidden = hasIds;
  if (hasIds) {
    $('#export-selection-text').textContent = `仅导出已勾选的 ${spec.ids.length} 笔项目及其收据。`;
  } else if (summaryEl) {
    const activeCount = [
      Boolean(spec.from || spec.to),
      spec.kind !== 'all',
      spec.status !== 'all',
      spec.currency !== 'all',
      spec.payer !== 'all',
      Boolean(spec.q),
    ].filter(Boolean).length;
    summaryEl.textContent = activeCount ? `已设 ${activeCount} 项 · ${spec.base}` : `参考 ${spec.base}`;
  }

  errEl.textContent = '';
  if (!hasIds && spec.from && spec.to && spec.from > spec.to) {
    errEl.textContent = '开始日期不能晚于结束日期';
    previewEl.innerHTML = '<strong>范围无效</strong><span>请调整起止日期。</span>';
    previewEl.classList.add('is-empty');
    if (submitEl) submitEl.disabled = true;
    return;
  }
  if (submitEl) submitEl.disabled = false;

  const matched = exportMatchingEntries(spec);
  const imgReceipts = matched.filter(e => e.receipt && e.receipt_type !== 'application/pdf').length;
  const pdfReceipts = matched.filter(e => e.receipt && e.receipt_type === 'application/pdf').length;
  const totalReceipts = imgReceipts + pdfReceipts;
  const currencies = [...new Set(matched.map(e => e.currency))];
  const receiptText = totalReceipts
    ? (spec.include_receipts
      ? `${totalReceipts} 份收据（图片 ${imgReceipts}${pdfReceipts ? ` · PDF ${pdfReceipts}` : ''}）`
      : `${totalReceipts} 份收据（不内嵌图片）`)
    : '无收据';
  previewEl.classList.toggle('is-empty', matched.length === 0);
  if (matched.length === 0) {
    previewEl.innerHTML = '<strong>当前范围 0 笔匹配</strong><span>可调整范围或直接导出空报表。</span>';
  } else {
    previewEl.innerHTML = `<strong>预计导出 ${matched.length} 笔账目</strong><span>${esc(receiptText)}${currencies.length ? ` · ${esc(currencies.join(' / '))}` : ''}</span>`;
  }
}

function applyExportPreset(preset) {
  const form = $('#export-form');
  if (!form) return;
  const now = new Date();
  state.exportState.activePreset = preset;
  if (preset !== 'selected') state.exportState.ids = [];

  const clearFields = () => {
    form.elements.from.value = '';
    form.elements.to.value = '';
    form.elements.kind.value = 'all';
    form.elements.status.value = 'all';
    form.elements.currency.value = 'all';
    form.elements.payer.value = 'all';
    form.elements.q.value = '';
  };

  if (preset === 'current_filter') {
    const f = state.filter;
    form.elements.from.value = f.from || '';
    form.elements.to.value = f.to || '';
    form.elements.kind.value = f.kind || 'all';
    form.elements.status.value = f.status || 'all';
    form.elements.currency.value = f.currency || 'all';
    form.elements.payer.value = f.payer || 'all';
    form.elements.q.value = f.text || '';
  } else if (preset === 'all') {
    clearFields();
  } else if (preset === 'all_with_voided') {
    clearFields();
    form.elements.status.value = 'all_with_voided';
  } else if (preset === 'month') {
    clearFields();
    form.elements.from.value = localIsoDate(new Date(now.getFullYear(), now.getMonth(), 1));
    form.elements.to.value = localIsoDate(now);
  } else if (preset === 'last_month') {
    clearFields();
    form.elements.from.value = localIsoDate(new Date(now.getFullYear(), now.getMonth() - 1, 1));
    form.elements.to.value = localIsoDate(new Date(now.getFullYear(), now.getMonth(), 0));
  } else if (preset === 'last30') {
    clearFields();
    form.elements.from.value = localIsoDate(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 29));
    form.elements.to.value = localIsoDate(now);
  } else if (preset === 'pending') {
    clearFields();
    form.elements.kind.value = 'advance';
    form.elements.status.value = 'pending';
  }
  renderExportPresets();
  updateExportPreview();
}

function openExportDialog(source = 'overview', explicitIds = null) {
  if (!canManage()) {
    toast('此操作需要财务管理员权限', true);
    return;
  }
  const form = $('#export-form');
  const statusOptions = [
    ['all', '全部（不含已作废）'],
    ['all_with_voided', '全部（含已作废）'],
    ...Object.entries(STATUS),
  ];
  form.elements.status.innerHTML = options(statusOptions, x => x[0], x => x[1], 'all');
  form.elements.currency.innerHTML = `<option value="all">全部币种</option>${options(state.conf.currencies, c => c.code, c => `${c.code} · ${c.name}`, 'all')}`;
  form.elements.payer.innerHTML = `<option value="all">全部成员</option>${options(state.members, m => m.id, m => m.name, 'all')}`;
  form.elements.base.innerHTML = currencyOptions(state.base || state.conf.reference_currency);
  form.elements.include_receipts.checked = true;

  const detailsEl = $('#export-custom-details');
  if (detailsEl) detailsEl.open = false;

  state.exportState.source = source;
  state.exportState.ids = Array.isArray(explicitIds) && explicitIds.length ? explicitIds.map(Number) : [];

  if (state.exportState.ids.length) {
    state.exportState.activePreset = 'selected';
    renderExportPresets();
    updateExportPreview();
  } else if (source === 'ledger' && ledgerHasFilter()) {
    applyExportPreset('current_filter');
  } else if (source === 'reimbursements') {
    state.exportState.activePreset = state.reimburseTab === 'pending' ? 'pending' : '';
    form.elements.from.value = '';
    form.elements.to.value = '';
    form.elements.kind.value = 'advance';
    form.elements.status.value = state.reimburseTab || 'pending';
    form.elements.currency.value = 'all';
    form.elements.payer.value = 'all';
    form.elements.q.value = '';
    renderExportPresets();
    updateExportPreview();
  } else if (source === 'calculator') {
    if (state.calculator.mode === 'manual' && state.calculator.ids.size) {
      state.exportState.ids = [...state.calculator.ids];
      state.exportState.activePreset = 'selected';
      renderExportPresets();
      updateExportPreview();
    } else {
      applyExportPreset('pending');
    }
  } else {
    applyExportPreset('all');
  }
  $('#export-dialog').showModal();
}

function buildExportUrl(spec) {
  const params = new URLSearchParams();
  if (spec.ids?.length) {
    params.set('ids', spec.ids.join(','));
  } else {
    if (spec.kind && spec.kind !== 'all') params.set('kind', spec.kind);
    if (spec.status && spec.status !== 'all') params.set('status', spec.status);
    if (spec.currency && spec.currency !== 'all') params.set('currency', spec.currency);
    if (spec.payer && spec.payer !== 'all') params.set('payer', spec.payer);
    if (spec.from) params.set('from', spec.from);
    if (spec.to) params.set('to', spec.to);
    if (spec.q) params.set('q', spec.q);
  }
  if (spec.base) params.set('base', spec.base);
  params.set('include_receipts', spec.include_receipts ? '1' : '0');
  const query = params.toString();
  const datePart = spec.from && spec.to ? `${spec.from}_${spec.to}` : (spec.from || spec.to || localIsoDate());
  const filename = `team-ledger-${datePart}.xlsx`;
  return {url: `/api/export.xlsx${query ? '?' + query : ''}`, filename};
}

/* ============================================================================
 * Global Event Listeners
 * ========================================================================== */

document.addEventListener('click', async event => {
  const b = event.target.closest('button,a,tr');
  if (!b) return;
  if (b.id === 'account-trigger') {
    const isOpen = $('#account-dropdown').dataset.open === 'true';
    setAccountMenuOpen(!isOpen);
    return;
  }
  if (b.classList.contains('theme-switcher-trigger')) {
    const switcher = b.closest('[data-theme-switcher]');
    if (switcher) {
      const isOpen = switcher.dataset.open === 'true';
      setThemeMenuOpen(switcher, !isOpen);
    }
    return;
  }
  if (b.hasAttribute('data-theme-option')) {
    const switcher = b.closest('[data-theme-switcher]');
    setThemePreference(b.dataset.themeOption);
    if (switcher) setThemeMenuOpen(switcher, false);
    return;
  }
  if (b.closest('#account-menu')) {
    setAccountMenuOpen(false);
  }
  if (b.hasAttribute('data-confirm-cancel')) {
    resolveConfirm(confirmResolver?.hasInput ? null : false);
    return;
  }
  if (b.hasAttribute('data-copy-text')) {
    event.preventDefault();
    await copyText(b.dataset.copyText, b);
    return;
  }
  if (b.hasAttribute('data-close')) {
    b.closest('dialog')?.close();
    return;
  }
  if (b.hasAttribute('data-receipt')) {
    event.preventDefault();
    openReceipt(Number(b.dataset.receipt));
    return;
  }
  if (b.hasAttribute('data-receipt-zoom')) {
    const zoomed = $('#receipt-stage').classList.toggle('is-zoomed');
    b.textContent = zoomed ? '适应窗口' : '查看原尺寸';
    b.setAttribute('aria-pressed', String(zoomed));
    return;
  }
  if (b.hasAttribute('data-reload')) {
    location.reload();
    return;
  }
  if (b.id === 'ai-launcher' || b.hasAttribute('data-ai-open')) {
    event.preventDefault();
    openAi();
    return;
  }
  if (b.hasAttribute('data-ai-confirm')) {
    event.preventDefault();
    executeAiPlan();
    return;
  }
  if (b.hasAttribute('data-ai-cancel-plan')) {
    event.preventDefault();
    cancelAiPlan();
    return;
  }
  if (b.hasAttribute('data-ai-edit-form')) {
    event.preventDefault();
    aiEditInForm(Number(b.dataset.aiEditForm));
    return;
  }
  if (b.hasAttribute('data-new')) {
    event.preventDefault();
    openEntry(b.dataset.new);
    return;
  }
  if (b.hasAttribute('data-export-open')) {
    event.preventDefault();
    openExportDialog(b.dataset.exportOpen || 'overview');
    return;
  }
  if (b.hasAttribute('data-export-selected')) {
    event.preventDefault();
    const ids = [...state.selected];
    if (!ids.length) {
      toast('请先勾选要导出的项目', true);
      return;
    }
    openExportDialog('reimbursements', ids);
    return;
  }
  if (b.hasAttribute('data-export-preset')) {
    event.preventDefault();
    applyExportPreset(b.dataset.exportPreset);
    return;
  }
  if (b.id === 'export-clear-ids') {
    event.preventDefault();
    state.exportState.ids = [];
    applyExportPreset('all');
    return;
  }
  if (b.id === 'export-reset') {
    event.preventDefault();
    applyExportPreset('all');
    return;
  }
  if (b.hasAttribute('data-income-link')) {
    event.preventDefault();
    openEntry('income');
    return;
  }
  if (b.hasAttribute('data-kind')) {
    state.entryKind = b.dataset.kind;
    entryKindChanged();
    return;
  }
  if (b.hasAttribute('data-filter-kind')) {
    state.filter.kind = b.dataset.filterKind;
    render();
    return;
  }
  if (b.id === 'toggle-more-filters') {
    state.filterOpen = !state.filterOpen;
    const panel = $('#more-filters-panel');
    if (panel) panel.hidden = !state.filterOpen;
    syncLedgerChrome();
    return;
  }
  if (b.id === 'reset-filters') {
    state.filter = {text: '', kind: 'all', currency: 'all', payer: 'all', status: 'all', from: '', to: ''};
    state.filterOpen = false;
    render();
    return;
  }
  if (b.hasAttribute('data-reimburse-mode')) {
    const nextMode = b.dataset.reimburseMode;
    if (['queue', 'calculator'].includes(nextMode) && nextMode !== state.reimburseMode) {
      state.reimburseMode = nextMode;
      state.selected.clear();
      render();
      if (nextMode === 'calculator') await calculateFunds();
    }
    return;
  }
  if (b.hasAttribute('data-reimburse-tab')) {
    state.reimburseTab = b.dataset.reimburseTab;
    state.selected.clear();
    render();
    return;
  }
  if (b.hasAttribute('data-settings-tab')) {
    const nextTab = b.dataset.settingsTab;
    if (allowedSettingsTabs().includes(nextTab) && nextTab !== state.settingsTab) {
      state.settingsTab = nextTab;
      render();
    }
    return;
  }
  if (b.hasAttribute('data-batch')) {
    openAction(b.dataset.batch, [...state.selected]);
    return;
  }
  if (b.hasAttribute('data-entry') && !event.target.closest('input,a,button')) {
    openDetail(Number(b.dataset.entry));
    return;
  }
  if (b.hasAttribute('data-edit-entry')) {
    const e = state.entries.find(item => item.id === Number(b.dataset.editEntry));
    $('#detail-dialog').close();
    openEntry(e.kind, e);
    return;
  }
  if (b.hasAttribute('data-action')) {
    openAction(b.dataset.action, [Number(b.dataset.id)]);
    return;
  }
  if (b.hasAttribute('data-delete-entry')) {
    const id = Number(b.dataset.deleteEntry), e = state.entries.find(x => x.id === id);
    if (!e) return;
    const ok = await uiConfirm({
      title: `永久删除账目 #${id}`,
      message: `确认永久删除「${entryDescription(e)}」？\n账目、操作记录、付款记录与凭证将一并清除，无法恢复。`,
      confirmLabel: '确认永久删除',
      danger: true,
    });
    if (!ok) return;
    await busy(b, async () => {
      try {
        await api(`/entries/${id}`, 'DELETE');
        $('#detail-dialog').close();
        state.selected.delete(id);
        await refresh();
        toast(`已永久删除 #${id}`);
      } catch (err) {
        toast(err.message, true);
      }
    });
    return;
  }
  if (b.hasAttribute('data-remove-currency')) {
    b.closest('.currency-edit-row').remove();
    updateCurrencySelects();
    return;
  }
  if (b.id === 'add-currency') {
    $('#currency-rows').insertAdjacentHTML('beforeend', currencyEditorRow({code: '', name: '', symbol: '', decimals: 2}));
    return;
  }
  if (b.id === 'refresh-ledger') {
    await busy(b, async () => {
      try {
        await refresh();
        toast('团队账本已刷新');
      } catch (e) {
        toast(e.message, true);
      }
    });
    return;
  }
  if (b.id === 'profile-button') {
    openProfileDialog();
    return;
  }
  if (b.id === 'sync-members') {
    await busy(b, async () => {
      try {
        const res = await api('/members/sync', 'POST');
        await refresh();
        toast(`已同步成员权限（${res.total} 位成员）`);
      } catch (e) {
        toast(e.message, true);
      }
    });
    return;
  }
  if (b.id === 'auth-recheck') {
    await busy(b, async () => {
      try {
        await showAuth();
        toast('已重新检测配置状态');
      } catch (e) {
        toast(e.message, true);
      }
    });
    return;
  }
  if (b.id === 'save-draft') {
    await saveEntry('draft', b);
    return;
  }
  if (b.id === 'logout' || b.id === 'menu-logout') {
    await busy(b, async () => {
      try {
        const data = await api('/logout', 'POST');
        state.user = null;
        state.csrf = '';
        $$('dialog[open]').forEach(d => d.close());
        if (data?.logout_url) {
          navigateExternal(data.logout_url);
          return;
        }
        await showAuth();
        toast('已退出登录');
      } catch (e) {
        toast(e.message, true);
      }
    });
    return;
  }
  if (b.id === 'refresh-fx') {
    event.preventDefault();
    event.stopPropagation();
    await busy(b, async () => {
      try {
        state.report = await api('/fx/refresh', 'POST');
        state.base = null;
        ++state.calculator.request;
        state.calculator.report = null;
        render();
        if (state.page === 'reimbursements' && state.reimburseMode === 'calculator') await calculateFunds();
        toast(state.report.complete ? '已更新汇率快照' : '部分汇率尚不可用', !state.report.complete);
      } catch (e) {
        toast(e.message, true);
      }
    });
    return;
  }
  if (b.id === 'generate-feishu-link') {
    await busy(b, async () => {
      try {
        const data = await api('/feishu/link-code', 'POST');
        const cmd = `绑定 ${data.code}`;
        $('#feishu-link-code-result').innerHTML = `<div class="copy-field"><code>${esc(cmd)}</code>${copyButtonHtml(cmd)}</div><p class="fx-caption">10 分钟内有效，私聊发送给飞书机器人。</p>`;
      } catch (e) {
        toast(e.message, true);
      }
    });
    return;
  }
  if (b.id === 'unlink-feishu') {
    const ok = await uiConfirm({
      title: '解除飞书绑定',
      message: '解除后，当前飞书账号将无法继续记账。确认解除？',
      confirmLabel: '确认解除',
      danger: true,
    });
    if (!ok) return;
    try {
      await api('/feishu/unlink', 'POST');
      await refresh();
      toast('已解除飞书绑定');
    } catch (e) {
      toast(e.message, true);
    }
    return;
  }
  if (b.id === 'generate-link') {
    await busy(b, async () => {
      try {
        const data = await api('/telegram/link-code', 'POST');
        const cmd = `/start ${data.code}`;
        $('#link-code-result').innerHTML = `<div class="copy-field"><code>${esc(cmd)}</code>${copyButtonHtml(cmd)}</div><p class="fx-caption">10 分钟内有效，私聊发送给 Telegram 机器人。</p>`;
      } catch (e) {
        toast(e.message, true);
      }
    });
    return;
  }
  if (b.id === 'unlink-telegram') {
    const ok = await uiConfirm({
      title: '解除 Telegram 绑定',
      message: '解除后，当前 Telegram 账号将无法继续记账。确认解除？',
      confirmLabel: '确认解除',
      danger: true,
    });
    if (!ok) return;
    try {
      await api('/telegram/unlink', 'POST');
      await refresh();
      toast('已解除 Telegram 绑定');
    } catch (e) {
      toast(e.message, true);
    }
    return;
  }
  if (b.id === 'test-ai-config') {
    const form = $('#ai-config-form');
    if (!form) return;
    await busy(b, async () => {
      const errBox = $('#ai-config-error');
      if (errBox) errBox.textContent = '';
      try {
        await api('/ai/config', 'PUT', aiConfigPayload(form));
      } catch (e) {
        if (errBox) errBox.textContent = e.message;
        toast(e.message, true);
        return;
      }
      try {
        const res = await api('/ai/test', 'POST', {});
        await refresh();
        toast(`AI 连接成功 · ${res.model}（${res.latency_ms} ms）`);
      } catch (e) {
        await refresh();
        if ($('#ai-config-error')) $('#ai-config-error').textContent = e.message;
        toast(e.message, true);
      }
    });
    return;
  }
  if (b.id === 'clear-ai-config') {
    const ok = await uiConfirm({
      title: '清除 AI 配置',
      message: '确认清除已保存的 API Key 与模型设置并停用 AI 记账引擎？',
      confirmLabel: '确认清除',
      danger: true,
    });
    if (!ok) return;
    await busy(b, async () => {
      try {
        await api('/ai/config', 'DELETE');
        await refresh();
        toast('AI 配置已清除并停用');
      } catch (e) {
        toast(e.message, true);
      }
    });
    return;
  }
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && $('#account-dropdown')?.dataset.open === 'true') {
    setAccountMenuOpen(false);
    $('#account-trigger')?.focus();
  }
  const openTheme = $('[data-theme-switcher][data-open="true"]');
  if (e.key === 'Escape' && openTheme) {
    setThemeMenuOpen(openTheme, false);
    $('.theme-switcher-trigger', openTheme)?.focus();
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k' && state.user) {
    e.preventDefault();
    const dialog = $('#ai-dialog');
    if (dialog?.open) closeAi();
    else openAi();
    return;
  }
  if (e.key === 'Enter' && e.target.matches('tr[data-entry]')) {
    openDetail(Number(e.target.dataset.entry));
  }
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && e.target.matches('#ai-form textarea')) {
    e.preventDefault();
    $('#ai-form').requestSubmit();
  }
});

document.addEventListener('input', event => {
  const el = event.target;
  if (el.id === 'search-entries') {
    state.filter.text = el.value;
    $('#ledger-results').innerHTML = ledgerResults();
    syncLedgerChrome();
  }
  if (el.closest('#export-form')) {
    if (el.name !== 'include_receipts' && el.name !== 'base') {
      state.exportState.activePreset = '';
      renderExportPresets();
    }
    updateExportPreview();
  }
  if (el.id === 'auth-team-name') {
    syncAuthExternalLink();
  }
});

document.addEventListener('change', async event => {
  const el = event.target;
  if (el.id === 'feishu-domain-preset') {
    const wrap = $('#feishu-domain-custom-wrap');
    if (wrap) wrap.hidden = el.value !== 'custom';
    return;
  }
  if (el.id === 'calculator-mode') {
    const c = state.calculator;
    c.mode = el.value;
    if (c.mode === 'budget' && !$('#calculator-form').elements.budget.value) {
      $('#calculator-form').elements.budget.value = c.report?.reserve || state.report?.reserve || '0';
    }
    $('#calculator-budget-field').hidden = c.mode !== 'budget';
    $('#calculator-form').elements.budget.required = c.mode === 'budget';
    $('#calculator-selection-hint').hidden = c.mode !== 'manual';
    $('#plan-table').innerHTML = planGroups(c.report || state.report, c.mode === 'manual');
    await calculateFunds();
    return;
  }
  if (el.hasAttribute('data-calc-select')) {
    const id = Number(el.dataset.calcSelect);
    el.checked ? state.calculator.ids.add(id) : state.calculator.ids.delete(id);
    if ($('#calculator-selection-count')) $('#calculator-selection-count').textContent = state.calculator.ids.size;
    await calculateFunds();
    return;
  }
  if (el.closest('#calculator-form')) {
    await calculateFunds();
    return;
  }
  if (el.dataset.aiSelect !== undefined) {
    const index = Number(el.dataset.aiSelect);
    el.checked ? state.ai.selected.add(index) : state.ai.selected.delete(index);
    const count = aiSelected(), valid = state.ai.plan ? state.ai.plan.actions.filter(a => !a.errors?.length).length : 0;
    if ($('#ai-plan-count')) $('#ai-plan-count').textContent = `${state.ai.plan?.actions?.length || 0} 项 · 已选 ${count} 项`;
    if ($('#ai-selected-count')) $('#ai-selected-count').textContent = `已选 ${count} / ${valid} 项`;
    const confirmBtn = $('[data-ai-confirm]');
    if (confirmBtn) {
      confirmBtn.textContent = `确认执行 ${count} 项`;
      confirmBtn.disabled = !count;
    }
    return;
  }
  if (el.closest('#export-form')) {
    if (el.name !== 'include_receipts' && el.name !== 'base') {
      state.exportState.activePreset = '';
      renderExportPresets();
    }
    updateExportPreview();
    return;
  }
  if (el.dataset.filter) {
    state.filter[el.dataset.filter] = el.value;
    $('#ledger-results').innerHTML = ledgerResults();
    syncLedgerChrome();
  }
  if (el.dataset.select) {
    const id = Number(el.dataset.select);
    el.checked ? state.selected.add(id) : state.selected.delete(id);
    if ($('#batch-toolbar')) $('#batch-toolbar').innerHTML = batchToolbar();
  }
  if (el.id === 'select-all') {
    $$('[data-select]').forEach(c => {
      c.checked = el.checked;
      el.checked ? state.selected.add(Number(c.dataset.select)) : state.selected.delete(Number(c.dataset.select));
    });
    if ($('#batch-toolbar')) $('#batch-toolbar').innerHTML = batchToolbar();
  }
  if (el.name === 'receipt') {
    $('#upload-label').textContent = el.files[0]?.name || '添加发票或凭证';
  }
  if (el.id === 'detail-receipt' && el.files[0]) {
    const id = Number(el.dataset.id), file = el.files[0];
    el.disabled = true;
    try {
      if (file.size > 6 * 1024 * 1024) throw new Error('凭证大小不能超过 6 MB');
      const data = await new Promise((resolve, reject) => {
        const r = new FileReader();
        r.onload = () => resolve(String(r.result).split(',')[1]);
        r.onerror = () => reject(new Error('文件读取失败'));
        r.readAsDataURL(file);
      });
      await api(`/entries/${id}/receipt`, 'POST', {data, name: file.name});
      await refresh();
      $('#detail-dialog').close();
      await openDetail(id);
      toast('凭证已上传');
    } catch (e) {
      el.disabled = false;
      toast(e.message, true);
    }
  }
  if (el.id === 'report-base') {
    state.base = el.value;
    ++state.calculator.request;
    state.calculator.report = null;
    state.calculator.budget = '';
    try {
      state.report = await api('/report?base=' + encodeURIComponent(state.base));
      render();
      if (state.page === 'reimbursements' && state.reimburseMode === 'calculator') await calculateFunds();
    } catch (e) {
      toast(e.message, true);
    }
  }
  if (el.hasAttribute('data-cur')) updateCurrencySelects();
});

function updateCurrencySelects() {
  const currencies = $$('.currency-edit-row').map(r => ({
    code: $('[data-cur=code]', r).value.trim().toUpperCase(),
    name: $('[data-cur=name]', r).value.trim(),
  })).filter(c => /^[A-Z]{3}$/.test(c.code));
  ['default-currency', 'reference-currency'].forEach(id => {
    const el = $('#' + id);
    if (el) {
      const value = el.value;
      el.innerHTML = options(currencies, c => c.code, c => c.code + ' · ' + c.name, value);
    }
  });
}

$('#auth-form').addEventListener('submit', event => {
  event.preventDefault();
  if (!state.boot?.oidc_configured) return;
  navigateExternal(buildOidcLoginUrl());
});

$('#export-form').addEventListener('submit', event => {
  event.preventDefault();
  const spec = exportSpecFromForm();
  if (!spec.ids.length && spec.from && spec.to && spec.from > spec.to) {
    $('#export-error').textContent = '开始日期不能晚于结束日期';
    return;
  }
  const {url, filename} = buildExportUrl(spec);
  $('#export-dialog').close();
  triggerDownload(url, filename);
  toast('正在生成并下载 Excel 报表…');
});

$('#entry-form').addEventListener('submit', async event => {
  event.preventDefault();
  await saveEntry('pending', $('#save-entry'));
});

$('#profile-form').addEventListener('submit', async event => {
  event.preventDefault();
  await busy($('button[type=submit]', event.target), async () => {
    try {
      $('#profile-error').textContent = '';
      const name = $('#profile-name').value.trim();
      const data = await api('/me', 'PATCH', {name});
      state.user = data.user;
      $('#profile-dialog').close();
      await refresh();
      toast('个人资料已更新');
    } catch (e) {
      $('#profile-error').textContent = e.message;
    }
  });
});

$('#action-form').addEventListener('submit', async event => {
  event.preventDefault();
  await busy($('#action-confirm'), async () => {
    try {
      const {action, ids, key, quotes} = state.action;
      const body = {action, ids, note: event.target.elements.note.value};
      if (action === 'pay') {
        body.payments = $$('[data-payment]').map(input => ({
          id: Number(input.dataset.payment),
          amount: input.value,
          quote_id: quotes[input.dataset.payment].id,
        }));
      }
      await api('/entries/action', 'POST', body, {'Idempotency-Key': key});
      $('#action-dialog').close();
      $('#detail-dialog').close();
      state.selected.clear();
      await refresh();
      toast(action === 'pay' ? '已登记本次报销' : '账目状态已更新');
    } catch (e) {
      $('#action-error').textContent = e.message;
    }
  });
});

function aiConfigPayload(form) {
  const intOrRaw = value => /^-?\d+$/.test(String(value).trim()) ? Number(String(value).trim()) : value;
  return {
    enabled: Boolean(form.elements.enabled.checked),
    base_url: form.elements.base_url.value.trim(),
    model: form.elements.model.value.trim(),
    api_key: form.elements.api_key.value,
    response_format: form.elements.response_format.value,
    timeout: intOrRaw(form.elements.timeout.value),
    max_actions: intOrRaw(form.elements.max_actions.value),
    context_entries: intOrRaw(form.elements.context_entries.value),
    context_chars: intOrRaw(form.elements.context_chars.value),
  };
}

document.addEventListener('submit', async event => {
  const form = event.target;
  if (form.id === 'ai-form') {
    event.preventDefault();
    await submitAi(form);
  }
  if (form.id === 'ai-config-form') {
    event.preventDefault();
    await busy($('#save-ai-config'), async () => {
      const errBox = $('#ai-config-error');
      if (errBox) errBox.textContent = '';
      try {
        const saved = await api('/ai/config', 'PUT', aiConfigPayload(form));
        await refresh();
        toast(saved.enabled ? 'AI 配置已保存' : 'AI 已停用');
      } catch (e) {
        if (errBox) errBox.textContent = e.message;
        toast(e.message, true);
      }
    });
  }
  if (form.id === 'feishu-config-form') {
    event.preventDefault();
    await busy($('#save-feishu-config'), async () => {
      try {
        const preset = $('#feishu-domain-preset').value;
        const domain = preset === 'custom' ? $('#feishu-domain-custom').value.trim() : preset;
        await api('/feishu/config', 'POST', {
          app_id: $('#feishu-app-id').value.trim(),
          app_secret: $('#feishu-app-secret').value,
          domain,
        });
        await refresh();
        toast('飞书配置已保存');
      } catch (e) {
        toast(e.message, true);
      }
    });
  }
  if (form.id === 'allowlist-form') {
    event.preventDefault();
    await busy($('#save-allowlist'), async () => {
      const errBox = $('#allowlist-error');
      if (errBox) errBox.textContent = '';
      try {
        const raw = Object.fromEntries(new FormData(form));
        await api('/settings', 'PUT', {
          feishu_allowed_chats: String(raw.feishu_allowed_chats || '').split('\n').map(s => s.trim()).filter(Boolean),
          telegram_allowed_chats: String(raw.telegram_allowed_chats || '').split('\n').map(s => s.trim()).filter(Boolean),
        });
        await refresh();
        toast('群组白名单已保存');
      } catch (e) {
        if (errBox) errBox.textContent = e.message;
        toast(e.message, true);
      }
    });
  }
  if (form.id === 'calculator-form') {
    event.preventDefault();
    await busy($('button[type=submit]', form), calculateFunds);
  }
  if (form.id === 'settings-form') {
    event.preventDefault();
    const submitBtn = event.submitter || $('button[type=submit]', form);
    await busy(submitBtn, async () => {
      try {
        $('#settings-error').textContent = '';
        const raw = Object.fromEntries(new FormData(form));
        const body = {
          team_name: raw.team_name,
          default_currency: raw.default_currency,
          reference_currency: raw.reference_currency,
          currencies: $$('.currency-edit-row', form).map(row => ({
            code: $('[data-cur=code]', row).value.trim().toUpperCase(),
            name: $('[data-cur=name]', row).value.trim(),
            symbol: $('[data-cur=symbol]', row).value.trim(),
            decimals: Number($('[data-cur=decimals]', row).value),
          })),
        };
        await api('/settings', 'PUT', body);
        state.base = null;
        await refresh();
        toast('团队设置已保存');
      } catch (e) {
        $('#settings-error').textContent = e.message;
      }
    });
  }
});

window.addEventListener('hashchange', () => {
  parseHashRoute();
  state.selected.clear();
  setAccountMenuOpen(false);
  closeAllThemeMenus();
  render();
  if (state.page === 'reimbursements' && state.reimburseMode === 'calculator') calculateFunds();
  $('#main')?.scrollTo({top: 0, behavior: 'auto'});
  if (state.pendingEntryId) {
    const targetId = state.pendingEntryId;
    state.pendingEntryId = null;
    if (state.entries.some(e => e.id === targetId)) openDetail(targetId);
  }
});

start();
