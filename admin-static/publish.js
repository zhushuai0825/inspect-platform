(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const form = $('publish-form');
  let snapshot = null;
  let busy = false;
  const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
  const labels = {frontend: '前端', backend: '后端', deploy: '发布', rollback: '回滚'};
  const states = {
    deploying: ['wait', '发布中'],
    succeeded: ['pass', '已成功'],
    failed: ['fail', '未成功'],
    rolled_back: ['back', '已回滚']
  };
  const inspectLabels = {
    disabled: '巡检未启用',
    skipped: '未启动巡检',
    error: '巡检未启动',
    passed: '巡检通过',
    failed: '巡检失败',
    unknown: '巡检结果未知'
  };

  function msg(text, failed) {
    $('message').textContent = text;
    $('message').className = 'notice' + (failed ? ' error' : '');
  }

  async function api(path, body) {
    const opts = {cache: 'no-store'};
    if (body) {
      opts.method = 'POST';
      opts.headers = {'Content-Type': 'application/json', 'X-QA-Admin': '1'};
      opts.body = JSON.stringify(body);
    }
    const response = await fetch('/api' + path, opts);
    const data = await response.json();
    if (!response.ok || !data.ok) {
      const error = new Error(data.error || '操作失败');
      error.payload = data;
      throw error;
    }
    return data.data;
  }

  function stateBadge(state) {
    const item = states[state] || ['unknown', state || '未知'];
    return '<span class="badge ' + item[0] + '">' + esc(item[1]) + '</span>';
  }

  function buildLink(url) {
    if (!/^https?:\/\//.test(url || '')) return '';
    return '<a href="' + esc(url) + '" rel="noopener noreferrer">Jenkins 构建</a>';
  }

  function actions(row) {
    const service = snapshot.services[row.service] || {};
    const buttons = [];
    if (row.state === 'deploying') {
      buttons.push('<button class="link-button" data-refresh="' + esc(row.id) + '">刷新结果</button>');
      if (row.can_abandon) {
        buttons.push('<button class="link-button" data-abandon="' + esc(row.id) + '">标记未确认</button>');
      }
    }
    if (service.current_id === row.id && service.can_rollback) {
      buttons.push('<button class="link-button" data-rollback="' + esc(row.id) + '">回滚到上一成功发布</button>');
    }
    return buttons.join('') || '<span class="muted">—</span>';
  }

  function render() {
    const host = snapshot.host_valid ? snapshot.host : '主机配置无效';
    $('host-label').textContent = '目标 ' + host;
    $('lead').textContent = '记录前端或后端发到 ' + (snapshot.host || '本服务器') + ' 的结果。这里不读取代码仓库的推送或拉取请求。';
    $('summary').innerHTML = ['frontend', 'backend'].map(key => {
      const item = snapshot.services[key];
      const current = (snapshot.records || []).find(row => row.id === item.current_id);
      const detail = current
        ? (current.version || current.commit_sha || current.artifact || current.id)
        : '尚无生效发布';
      const wired = item.wired ? '发布任务已配置' : '发布任务未配置';
      const lock = item.locked ? ' · 发布进行中' : '';
      return '<article><span class="muted">' + esc(item.label) + ' · ' + esc(wired) + esc(lock) + '</span><strong>' + esc(detail) + '</strong></article>';
    }).join('');
    const rows = snapshot.records || [];
    $('count').textContent = rows.length ? ('共 ' + rows.length + ' 条') : '尚无记录';
    $('records').innerHTML = rows.length ? rows.map(row => {
      const when = (row.created_at || '').replace('T', ' ').replace(/\.\d+/, '');
      const identity = [row.version, row.commit_sha, row.artifact].filter(Boolean).map(esc).join('<br>') || '—';
      const inspectLabel = inspectLabels[row.inspect_status] || row.inspect_status || '';
      const inspect = row.inspect_run_id
        ? ('巡检 ' + esc(row.inspect_run_id) + (inspectLabel ? ' · ' + esc(inspectLabel) : ''))
        : esc(inspectLabel);
      return '<tr><td class="timestamp">' + esc(when) + '<div class="row-sub">' + esc(labels[row.action] || row.action) + '</div></td>'
        + '<td><span class="service-tag">' + esc(labels[row.service] || row.service) + '</span></td>'
        + '<td>' + esc(row.actor) + '</td>'
        + '<td class="cell-wrap mono">' + identity + '</td>'
        + '<td>' + stateBadge(row.state) + '</td>'
        + '<td class="cell-wrap">' + esc(row.result_message || '') + '<div class="row-sub">' + buildLink(row.jenkins_build_url) + (inspect ? ' ' + inspect : '') + '</div></td>'
        + '<td><div class="row-actions">' + actions(row) + '</div></td></tr>';
    }).join('') : '<tr><td colspan="7" class="empty">尚无发布到本服务器的记录。</td></tr>';
    const selected = form.elements.service.value;
    const locked = snapshot.services[selected] && snapshot.services[selected].locked;
    $('publish').disabled = busy || locked;
  }

  async function load() {
    snapshot = await api('/releases');
    render();
  }

  $('reload').addEventListener('click', () => {
    load().catch(error => msg(error.message, true));
  });
  form.elements.service.addEventListener('change', () => {
    if (snapshot) render();
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    busy = true;
    $('publish').disabled = true;
    try {
      await api('/releases', {
        service: form.elements.service.value,
        actor: form.elements.actor.value,
        version: form.elements.version.value,
        commit: form.elements.commit.value,
        artifact: form.elements.artifact.value
      });
      msg('发布任务已提交。在 Jenkins 给出结果前，状态保持为发布中。');
      await load();
    } catch (error) {
      msg(error.message, true);
      try { await load(); } catch (loadError) { msg(loadError.message, true); }
    } finally {
      busy = false;
      if (snapshot) render();
    }
  });
  $('records').addEventListener('click', async event => {
    const refresh = event.target.closest('[data-refresh]');
    const rollback = event.target.closest('[data-rollback]');
    const abandon = event.target.closest('[data-abandon]');
    if (!refresh && !rollback && !abandon || busy) return;
    const id = (refresh || rollback || abandon).dataset.refresh
      || (refresh || rollback || abandon).dataset.rollback
      || (refresh || rollback || abandon).dataset.abandon;
    if (rollback && !window.confirm('确认回滚该服务到上一次成功发布？')) return;
    if (abandon && !window.confirm('只有在 Jenkins 没有对应构建时才应标记未确认。确认继续？')) return;
    busy = true;
    try {
      if (refresh) await api('/releases/' + id + '/refresh', {});
      if (rollback) await api('/releases/' + id + '/rollback', {actor: form.elements.actor.value});
      if (abandon) await api('/releases/' + id + '/abandon', {actor: form.elements.actor.value});
      msg(refresh ? '已读取 Jenkins 结果。' : (rollback ? '回滚任务已提交。' : '已标记为未确认。'));
      await load();
    } catch (error) {
      msg(error.message, true);
      try { await load(); } catch (loadError) { /* keep the action error */ }
    } finally {
      busy = false;
      if (snapshot) render();
    }
  });
  load().catch(error => {
    $('records').innerHTML = '<tr><td colspan="7" class="empty">没有可展示的发布记录。</td></tr>';
    msg(error.message, true);
  });
})();
