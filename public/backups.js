'use strict';

const backupUI = {conf: null, items: [], target: 'local', tab: 'files', busy: false, error: '', listError: '', request: 0};
const BACKUP_TARGETS = {local: '服务器本地', webdav: 'WebDAV', s3: 'S3 存储桶'};
const backupSize = size => size < 1048576 ? `${(size / 1024).toFixed(1)} KB` : `${(size / 1048576).toFixed(1)} MB`;

function backupDate(value) {
  if (!value) return '暂无';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', {timeZone: backupUI.conf?.timezone || state.conf.timezone, hour12: false});
}

async function mountBackups() {
  const request = ++backupUI.request;
  const host = $('#backup-settings-slot');
  if (!host) return;
  host.innerHTML = '<section id="backup-settings" class="backup-section" aria-live="polite"><div class="skeleton">正在读取备份设置…</div></section>';
  const root = $('#backup-settings');
  try {
    const conf = await api('/backups/config');
    if (request !== backupUI.request || !root.isConnected) return;
    backupUI.conf = conf;
    backupUI.error = '';
    await loadBackupList();
    if (request === backupUI.request && root.isConnected) renderBackupSettings();
  } catch (error) {
    if (root.isConnected) {
      root.innerHTML = `<div class="callout error"><span class="callout-body">${esc(error.message)}</span><button type="button" class="button button-outline button-small" data-backup-refresh>重新读取</button></div>`;
    }
  }
}

async function loadBackupList() {
  const target = backupUI.target;
  try {
    const items = await api('/backups?target=' + encodeURIComponent(target));
    if (target === backupUI.target) {
      backupUI.items = items;
      backupUI.listError = '';
    }
  } catch (error) {
    if (target === backupUI.target) {
      backupUI.items = [];
      backupUI.listError = error.message;
    }
  }
}

function renderBackupSettings() {
  const root = $('#backup-settings'), c = backupUI.conf;
  if (!root || !c) return;
  const s = c.schedule, status = c.status || {}, weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
  const tab = backupUI.tab === 'config' ? 'config' : 'files';
  const webdavReady = Boolean(c.webdav.url);
  const s3Ready = Boolean(c.s3.bucket);
  const statusSummary = [
    s.enabled ? `下次 ${backupDate(status.next_run)}` : '定时未启用',
    status.last_success ? `最近成功 ${backupDate(status.last_success)}` : null,
  ].filter(Boolean).join(' · ');

  root.innerHTML = `
    <div class="settings-section-head">
      <h2>备份与恢复</h2>
      <div class="segmented segmented-compact" role="tablist" aria-label="备份视图切换">
        <button type="button" role="tab" aria-selected="${tab === 'files'}" class="${tab === 'files' ? 'active' : ''}" data-backup-tab="files">备份文件与恢复</button>
        <button type="button" role="tab" aria-selected="${tab === 'config'}" class="${tab === 'config' ? 'active' : ''}" data-backup-tab="config">存储与定时配置</button>
      </div>
    </div>

    <div class="backup-status-strip">
      <span class="badge ${s.enabled ? 'posted' : 'draft'}">${s.enabled ? '自动备份已启用' : '自动备份未启用'}</span>
      <span class="backup-status-meta">${esc(statusSummary)}</span>
    </div>
    ${status.last_error ? `<div class="callout error" role="alert">${icon('info')}<span class="callout-body">${esc(status.last_error)}</span></div>` : ''}

    <div class="backup-tab-panel" ${tab === 'files' ? '' : 'hidden'}>
      <section class="panel backup-files">
        <div class="backup-toolbar">
          <h3>备份文件</h3>
          <div class="backup-toolbar-controls">
            <select id="backup-target" aria-label="存储位置">${Object.entries(BACKUP_TARGETS).map(([key, label]) => `<option value="${key}" ${key === backupUI.target ? 'selected' : ''}>${label}</option>`).join('')}</select>
            <button class="button button-outline" type="button" data-backup-refresh>${icon('refresh', 'ico-refresh')}刷新</button>
            <button class="button button-primary" type="button" data-backup-create>${icon('plus', 'ico-plus')}立即备份</button>
          </div>
        </div>
        ${backupUI.listError
          ? `<div class="backup-panel-body"><div class="callout error" role="alert"><span class="callout-body">${esc(backupUI.listError)}</span></div></div>`
          : backupUI.items.length
            ? `<p class="table-scroll-hint" aria-hidden="true">${icon('move', 'icon-sm')}左右滑动查看完整列表</p><div class="table-wrap" role="region" aria-label="备份文件列表" tabindex="0"><table class="data-table"><thead><tr><th>文件名</th><th>大小</th><th>保存时间</th><th class="actions-col">操作</th></tr></thead><tbody>${backupUI.items.map(item => `<tr><td><strong class="backup-filename">${esc(item.name)}</strong>${item.name.startsWith('before-restore-') ? '<span class="subline">恢复前自动快照</span>' : ''}</td><td class="mono">${backupSize(item.size)}</td><td class="mono">${esc(backupDate(item.modified_at))}</td><td class="actions-col"><div class="backup-row-actions"><button class="button button-outline button-small" type="button" data-backup-download="${esc(item.name)}">下载</button><button class="button button-outline button-danger-outline button-small" type="button" data-backup-restore="${esc(item.name)}">恢复</button></div></td></tr>`).join('')}</tbody></table></div>`
            : `<div class="backup-panel-body">${empty('暂无备份文件', '点击右上角「立即备份」生成完整账本快照。')}</div>`}
      </section>

      <section class="panel backup-restore-panel">
        <div class="panel-inner">
          <h3>从本地 ZIP 恢复</h3>
          <p class="backup-restore-hint">上传系统导出的 ZIP 备份（上限 ${backupSize(c.max_upload_bytes)}）。恢复将覆盖当前账本并退出全部登录会话，恢复前会自动保存快照。</p>
          <div class="backup-upload">
            <input type="file" id="backup-upload-file" accept=".zip,application/zip,application/x-zip-compressed" aria-label="选择本地 ZIP 备份文件">
            <button class="button button-outline button-danger-outline" type="button" data-backup-upload>上传并恢复</button>
          </div>
        </div>
      </section>
    </div>

    <form id="backup-config-form" class="backup-tab-panel" ${tab === 'config' ? '' : 'hidden'}>
      <section class="panel">
        <div class="panel-inner">
          <h3>定时自动备份</h3>
          <label class="backup-check"><input class="settings-switch" name="schedule_enabled" type="checkbox" ${s.enabled ? 'checked' : ''}><span>启用定时自动备份（按 ${esc(c.timezone)}）</span></label>
          <div class="form-grid">
            <label><span>备份频率</span><select name="schedule_frequency"><option value="hourly" ${s.frequency === 'hourly' ? 'selected' : ''}>每小时整点</option><option value="daily" ${s.frequency === 'daily' ? 'selected' : ''}>每天</option><option value="weekly" ${s.frequency === 'weekly' ? 'selected' : ''}>每周</option></select></label>
            <label data-backup-time ${s.frequency === 'hourly' ? 'hidden' : ''}><span>备份时间</span><input name="schedule_time" type="time" value="${esc(s.time)}" required></label>
            <label data-backup-weekday ${s.frequency !== 'weekly' ? 'hidden' : ''}><span>星期</span><select name="schedule_weekday">${weekdays.map((day, i) => `<option value="${i}" ${i === s.weekday ? 'selected' : ''}>${day}</option>`).join('')}</select></label>
            <label><span>每个位置保留份数</span><input name="keep" type="number" min="1" max="1000" step="1" value="${c.keep}" required></label>
          </div>
          <fieldset class="backup-targets"><legend>自动备份目标</legend>${Object.entries(BACKUP_TARGETS).map(([key, label]) => `<label class="backup-check"><input type="checkbox" name="schedule_targets" value="${key}" ${s.targets.includes(key) ? 'checked' : ''}><span>${label}</span></label>`).join('')}</fieldset>
          ${status.last_attempt || status.last_restore || status.last_results ? `<div class="backup-run-log">
            ${status.last_attempt ? `<span>最近尝试：${esc(backupDate(status.last_attempt))}</span>` : ''}
            ${status.last_results ? `<span>结果：${Object.entries(status.last_results).map(([target, result]) => `${esc(BACKUP_TARGETS[target] || target)} ${result === 'ok' ? '成功' : '失败'}`).join(' · ')}</span>` : ''}
            ${status.last_restore ? `<span>最近恢复：${esc(backupDate(status.last_restore))}</span>` : ''}
          </div>` : ''}
        </div>
        <div class="panel-divider"></div>
        <div class="panel-inner backup-remote-stack">
          <h3>远程存储目标</h3>
          <details class="disclosure-box" ${webdavReady ? 'open' : ''}>
            <summary>
              <span>WebDAV</span>
              <span class="badge ${webdavReady ? 'good' : 'draft'}">${webdavReady ? '已配置' : '未配置'}</span>
            </summary>
            <div class="disclosure-body">
              <label><span>服务地址</span><input name="webdav_url" type="url" value="${esc(c.webdav.url)}" placeholder="https://cloud.example.com/remote.php/dav/files/用户名/" autocomplete="off"></label>
              <div class="form-grid">
                <label><span>用户名</span><input name="webdav_username" value="${esc(c.webdav.username)}" autocomplete="off"></label>
                <label><span>密码 / 应用密码</span><input name="webdav_password" type="password" autocomplete="new-password" placeholder="${c.webdav.has_password ? '已保存，留空保留原密码' : '填写密码'}"></label>
              </div>
              <label><span>备份目录</span><input name="webdav_prefix" value="${esc(c.webdav.prefix)}" placeholder="team-ledger"></label>
              <div class="integration-actions"><button type="button" class="button button-outline" data-backup-test="webdav">保存并测试 WebDAV</button></div>
            </div>
          </details>
          <details class="disclosure-box" ${s3Ready ? 'open' : ''}>
            <summary>
              <span>S3 兼容存储桶</span>
              <span class="badge ${s3Ready ? 'good' : 'draft'}">${s3Ready ? '已配置' : '未配置'}</span>
            </summary>
            <div class="disclosure-body">
              <label><span>Endpoint <small class="optional">AWS S3 可留空</small></span><input name="s3_endpoint" type="url" value="${esc(c.s3.endpoint)}" placeholder="https://s3.example.com" autocomplete="off"></label>
              <div class="form-grid">
                <label><span>区域</span><input name="s3_region" value="${esc(c.s3.region)}" required placeholder="us-east-1"></label>
                <label><span>存储桶名称</span><input name="s3_bucket" value="${esc(c.s3.bucket)}" autocomplete="off"></label>
              </div>
              <div class="form-grid">
                <label><span>Access Key ID</span><input name="s3_access_key_id" value="${esc(c.s3.access_key_id)}" autocomplete="off"></label>
                <label><span>Secret Access Key</span><input name="s3_secret_access_key" type="password" autocomplete="new-password" placeholder="${c.s3.has_secret_access_key ? '已保存，留空保留原密钥' : '填写密钥'}"></label>
              </div>
              <label><span>备份目录</span><input name="s3_prefix" value="${esc(c.s3.prefix)}" placeholder="team-ledger"></label>
              <label class="backup-check"><input class="settings-switch" name="s3_path_style" type="checkbox" ${c.s3.path_style ? 'checked' : ''}><span>路径风格访问（Path style）</span></label>
              <div class="integration-actions"><button type="button" class="button button-outline" data-backup-test="s3">保存并测试 S3</button></div>
            </div>
          </details>
        </div>
        <div class="panel-footer-actions">
          <div class="form-error panel-footer-error" id="backup-error" role="alert">${esc(backupUI.error)}</div>
          <button class="button button-primary" type="submit">保存备份设置</button>
        </div>
      </section>
    </form>`;
}

function backupFormBody() {
  const form = $('#backup-config-form'), f = form.elements;
  return {
    webdav: {url: f.webdav_url.value, username: f.webdav_username.value, password: f.webdav_password.value, prefix: f.webdav_prefix.value},
    s3: {endpoint: f.s3_endpoint.value, region: f.s3_region.value, bucket: f.s3_bucket.value, access_key_id: f.s3_access_key_id.value, secret_access_key: f.s3_secret_access_key.value, prefix: f.s3_prefix.value, path_style: f.s3_path_style.checked},
    schedule: {enabled: f.schedule_enabled.checked, frequency: f.schedule_frequency.value, time: f.schedule_time.value, weekday: Number(f.schedule_weekday.value), targets: $$('[name=schedule_targets]:checked', form).map(input => input.value)},
    keep: Number(f.keep.value),
  };
}

async function backupTask(fn) {
  if (backupUI.busy) return;
  backupUI.busy = true;
  const root = $('#backup-settings');
  root?.setAttribute('aria-busy', 'true');
  const controls = root ? $$('button,input,select', root) : [];
  controls.forEach(control => { control.disabled = true; });
  try {
    backupUI.error = '';
    if ($('#backup-error')) $('#backup-error').textContent = '';
    await fn();
  } catch (error) {
    backupUI.error = error.message;
    if ($('#backup-error')) $('#backup-error').textContent = error.message;
    toast(error.message, true);
  } finally {
    backupUI.busy = false;
    root?.removeAttribute('aria-busy');
    controls.forEach(control => { control.disabled = false; });
  }
}

async function saveBackupForm() {
  backupUI.conf = await api('/backups/config', 'PUT', backupFormBody());
}

async function reloadBackups() {
  backupUI.conf = await api('/backups/config');
  await loadBackupList();
  renderBackupSettings();
}

async function afterBackupRestore(result) {
  ++backupUI.request;
  backupUI.conf = null;
  state.user = null;
  state.csrf = '';
  state.conf = null;
  state.ai = {busy: false, conversation: [], plan: null, selected: new Set(), error: ''};
  $$('dialog[open]').forEach(dialog => dialog.close());
  await showAuth();
  toast(`账本已恢复，请使用 Vanora 账号重新登录。恢复前副本：${result.safety_copy}`);
}

document.addEventListener('submit', async event => {
  if (event.target.id !== 'backup-config-form') return;
  event.preventDefault();
  await backupTask(async () => {
    await saveBackupForm();
    renderBackupSettings();
    toast('备份设置已保存');
  });
});

document.addEventListener('change', async event => {
  if (event.target.name === 'schedule_frequency') {
    const frequency = event.target.value;
    $('[data-backup-time]').hidden = frequency === 'hourly';
    $('[data-backup-weekday]').hidden = frequency !== 'weekly';
  }
  if (event.target.id === 'backup-target') {
    backupUI.target = event.target.value;
    await backupTask(async () => {
      await loadBackupList();
      renderBackupSettings();
    });
  }
});

document.addEventListener('click', async event => {
  const tabBtn = event.target.closest('[data-backup-tab]');
  if (tabBtn) {
    event.preventDefault();
    const next = tabBtn.dataset.backupTab;
    if (next !== backupUI.tab) {
      backupUI.tab = next;
      renderBackupSettings();
    }
    return;
  }
  const button = event.target.closest('[data-backup-create],[data-backup-test],[data-backup-refresh],[data-backup-download],[data-backup-restore],[data-backup-upload]');
  if (!button) return;
  event.preventDefault();
  if (backupUI.busy) return;
  if (button.hasAttribute('data-backup-restore') || button.hasAttribute('data-backup-upload')) {
    const file = button.hasAttribute('data-backup-upload') ? $('#backup-upload-file')?.files[0] : null;
    if (button.hasAttribute('data-backup-upload') && !file) {
      toast('请先选择备份 ZIP 文件', true);
      return;
    }
    if (file && file.size > backupUI.conf.max_upload_bytes) {
      toast('备份文件超过服务器允许的大小', true);
      return;
    }
    const name = file?.name || button.dataset.backupRestore, target = backupUI.target;
    const ok = await uiConfirm({
      title: '确认恢复备份',
      message: `即将从「${name}」恢复账本。\n\n当前账目与凭证将被覆盖（恢复前自动保存副本），所有登录会话将退出，请重新登录。`,
      confirmLabel: '确认恢复',
      danger: true,
    });
    if (!ok) return;
    await backupTask(async () => {
      let result;
      if (file) {
        const response = await fetch('/api/backups/restore/upload', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {'Content-Type': 'application/zip', 'X-CSRF-Token': state.csrf, 'X-Backup-Confirm': 'restore'},
          body: file,
        });
        result = await response.json();
        if (!response.ok) throw new Error(typeof result.error === 'string' ? result.error : '恢复失败');
      } else {
        result = await api('/backups/restore', 'POST', {target, name, confirm: true});
      }
      await afterBackupRestore(result);
    });
    return;
  }
  await backupTask(async () => {
    if (button.hasAttribute('data-backup-test')) {
      await saveBackupForm();
      await api('/backups/test', 'POST', {target: button.dataset.backupTest});
      renderBackupSettings();
      toast('连接成功，已验证读写权限');
    } else if (button.hasAttribute('data-backup-create')) {
      try {
        await api('/backups', 'POST', {target: backupUI.target});
        await reloadBackups();
        toast('完整备份已保存');
      } catch (error) {
        await reloadBackups();
        throw error;
      }
    } else if (button.hasAttribute('data-backup-download')) {
      const name = button.dataset.backupDownload;
      const url = `/api/backups/download/${encodeURIComponent(name)}?target=${encodeURIComponent(backupUI.target)}`;
      triggerDownload(url, name);
    } else {
      await reloadBackups();
    }
  });
});
