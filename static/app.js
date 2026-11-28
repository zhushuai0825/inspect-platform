(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = { overview: null, reports: [], page: 1, pageSize: 10, detail: null, running: false, refreshVersion: 0, releases: null, releaseBusy: false, tasks: [], taskEditing: null, taskBusy: false };
  const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const stamp = value => value ? String(value).replace('T',' ').replace(/\+08:00$/, '') : '—';
  const duration = value => value == null ? '—' : Number(value) === 0 ? '< 1 s' : Number(value) < 1000 ? value + ' ms' : (Number(value) / 1000).toFixed(1) + ' s';
  const rate = value => value == null ? '—' : Number(value).toFixed(1).replace(/\.0$/, '') + '%';
  const source = value => ({manual:'手动触发',schedule:'定时调度',jenkins:'Jenkins',push:'代码推送',internal:'内部执行'}[value] || (value ? String(value) : '—'));
  const reportUrl = id => '#/reports/' + encodeURIComponent(id);
  const buildUrl = number => /^\d+$/.test(String(number || '')) ? '/jenkins/job/site-http-smoke/' + number + '/' : null;
  function badge(status) {
    const text = status === 'passed' ? '通过' : status === 'failed' ? '失败' : status === 'skipped' ? '未执行' : '未知';
    const cls = status === 'passed' ? 'pass' : status === 'failed' ? 'fail' : 'unknown';
    return '<span class="badge '+cls+'">'+text+'</span>';
  }
  function publicUrl(value) {
    try { const url = new URL(value, location.origin); return ['http:', 'https:'].includes(url.protocol) ? url.href : ''; }
    catch (_) { return ''; }
  }
  function pathOnly(value) {
    try { return new URL(value, location.origin).pathname; } catch (_) { return value || '—'; }
  }
  function toast(message) {
    $('toast').textContent = message; $('toast').classList.remove('hidden');
    clearTimeout(toast.timer); toast.timer = setTimeout(() => $('toast').classList.add('hidden'), 5000);
  }
  function error(message) {
    $('global-error').textContent = message || ''; $('global-error').classList.toggle('hidden', !message);
  }
  async function api(path, options) {
    const response = await fetch('./api' + path, Object.assign({cache:'no-store', headers:{'Content-Type':'application/json'}}, options || {}));
    const text = await response.text();
    let body; try { body = JSON.parse(text); } catch (_) { throw new Error('服务响应异常，请稍后重试。'); }
    if (!response.ok || body.ok === false) throw new Error(body.error || '请求失败（HTTP ' + response.status + '）');
    return body;
  }
  function currentRoute() {
    const match = location.hash.match(/^#\/(dashboard|tasks|reports|events|releases)(?:\/([^/]+))?$/);
    return match ? {name:match[1],id:match[2]} : {name:'dashboard'};
  }
  function setPage(name) {
    document.querySelectorAll('.page').forEach(node => node.classList.toggle('hidden', node.id !== 'page-' + name));
    document.querySelectorAll('[data-nav]').forEach(node => node.classList.toggle('active', node.dataset.nav === (name === 'report' ? 'reports' : name)));
    document.querySelectorAll('[data-nav]').forEach(node => node.classList.contains('active') ? node.setAttribute('aria-current','page') : node.removeAttribute('aria-current'));
    $('current-section').textContent = ({dashboard:'工作台',tasks:'巡检任务',reports:'报告与日志',report:'报告详情',events:'失败与恢复',releases:'发布记录'})[name] || '工作台';
    document.title = $('current-section').textContent + ' · 巡检平台';
  }
  function renderOverview(data) {
    const latest = data.latest, summary = latest ? latest.summary || {} : {};
    $('stat-status').textContent = latest ? (latest.status === 'passed' ? '全部通过' : latest.status === 'failed' ? '存在失败' : '未执行') : '暂无报告';
    $('stat-status').className = 'stat-value' + (latest && latest.status === 'passed' ? ' success' : latest && latest.status === 'failed' ? ' danger' : '');
    $('stat-when').textContent = latest ? stamp(latest.finished_at) : '等待首次巡检完成';
    $('stat-rate').textContent = rate(summary.pass_rate);
    $('stat-counts').textContent = latest ? '通过 '+summary.passed+' 项 / 共 '+summary.total+' 项' : '—';
    $('stat-failed').textContent = latest ? summary.failed : '—';
    $('stat-failed').className = 'stat-value' + (summary.failed > 0 ? ' danger' : '');
    $('stat-runs').textContent = data.totals ? data.totals.runs : '—';
    $('stat-failed-runs').textContent = data.totals ? '其中失败 '+data.totals.failed_runs+' 次' : '—';
    $('next-run').textContent = stamp(data.next_run_at).slice(0,16);
    $('latest-checks-meta').textContent = latest ? '运行 '+latest.id : '暂无报告';
    $('latest-report-link').href = latest ? reportUrl(latest.id) : '#/reports';
    const checks = latest ? latest.checks || [] : [];
    $('checks-body').innerHTML = checks.length ? checks.map(c => '<tr><td>'+esc(c.name || c.id)+'</td><td class="path-cell">'+esc(pathOnly(c.public_url || c.url))+'</td><td>'+badge(c.ok ? 'passed' : 'failed')+'</td><td class="mono">'+esc(c.status_code == null ? '—' : c.status_code)+'</td><td class="right mono">'+esc(duration(c.duration_ms))+'</td></tr>').join('') : '<tr><td colspan="5" class="empty">尚无检查结果，执行巡检后将在此显示。</td></tr>';
    $('recent-body').innerHTML = (data.recent || []).slice(0,6).map(r => {
      const s = r.summary || {}, build = buildUrl(r.jenkins_build);
      return '<tr><td class="timestamp">'+esc(stamp(r.started_at))+'</td><td>'+badge(r.status)+'</td><td>'+esc(s.passed)+' / '+esc(s.total)+'</td><td>'+esc(s.failed)+'</td><td>'+esc(source(r.trigger))+'</td><td>'+(build ? '<a href="'+build+'" target="_blank" rel="noopener">#'+esc(r.jenkins_build)+'</a>' : '—')+'</td><td class="right"><a href="'+reportUrl(r.id)+'">查看报告</a></td></tr>';
    }).join('') || '<tr><td colspan="7" class="empty">暂无执行记录</td></tr>';
    $('task-latest-time').textContent = latest ? stamp(latest.finished_at) : '尚未执行';
    $('task-latest-status').innerHTML = latest ? badge(latest.status) : '—';
    const configured = data.tasks || [];
    state.tasks = configured;
    $('task-config-count').textContent = '共 '+configured.length+' 项 · 启用 '+configured.filter(t=>t.enabled).length+' 项';
    $('task-checks-body').innerHTML = configured.map(t => '<tr><td>'+esc(t.name)+'<div class="row-sub mono">'+esc(t.id)+(t.builtin?' · 内置':' · 自建')+' · v'+esc(t.revision)+'</div></td><td class="cell-wrap"><span class="mono">'+esc(t.method)+'</span><div class="row-sub">'+esc(t.url)+'</div></td><td class="cell-wrap">HTTP '+t.expected_status.map(esc).join(', ')+(t.contains?'<div class="row-sub">正文包含：'+esc(t.contains)+'</div>':'')+'</td><td>'+esc(t.timeout_sec)+' s</td><td><span class="'+(t.enabled?'task-enabled':'task-disabled')+'">'+(t.enabled?'已启用':'已停用')+'</span></td><td class="right"><div class="row-actions"><button type="button" class="link-button" data-task-edit="'+esc(t.id)+'">编辑</button><button type="button" class="link-button" data-task-toggle="'+esc(t.id)+'">'+(t.enabled?'停用':'启用')+'</button></div></td></tr>').join('') || '<tr><td colspan="6" class="empty">暂无配置</td></tr>';
    $('last-updated').textContent = '数据更新时间 '+stamp(data.server_time)+'（北京时间）';
  }
  function renderReports() {
    const needle = $('report-search').value.trim().toLowerCase(), filter = $('report-filter').value;
    const list = state.reports.filter(r => (!needle || String(r.id).toLowerCase().includes(needle)) && (filter === 'all' || r.status === filter));
    const total = Math.max(1, Math.ceil(list.length/state.pageSize)); state.page = Math.min(Math.max(1, state.page),total);
    $('report-count').textContent = '匹配 '+list.length+' 条'+(state.reports.length >= 200 ? '（最近 200 条）' : '');
    const start = (state.page-1)*state.pageSize;
    $('reports-body').innerHTML = list.slice(start,start+state.pageSize).map(r => {
      const s = r.summary || {};
      return '<tr><td><a class="mono" href="'+reportUrl(r.id)+'">'+esc(r.id)+'</a><div class="row-sub timestamp">'+esc(stamp(r.started_at))+'</div></td><td>'+badge(r.status)+'</td><td>'+esc(s.passed)+' / '+esc(s.total)+'</td><td>'+esc(s.failed)+'</td><td class="mono">'+esc(duration(r.duration_ms))+'</td><td>'+esc(source(r.trigger))+'</td><td class="right"><a href="'+reportUrl(r.id)+'">查看日志</a></td></tr>';
    }).join('') || '<tr><td colspan="7" class="empty">没有匹配的报告，请调整筛选条件。</td></tr>';
    $('page-count').textContent = '第 '+state.page+' / '+total+' 页 · 每页 '+state.pageSize+' 条';
    $('prev-page').disabled = state.page === 1; $('next-page').disabled = state.page === total;
  }
  function renderDetail(data) {
    state.detail = data; const s = data.summary || {};
    $('report-badge').innerHTML = badge(data.status); $('report-id').textContent = data.id;
    $('detail-start').textContent = stamp(data.started_at); $('detail-end').textContent = stamp(data.finished_at);
    $('detail-duration').textContent = duration(data.duration_ms); $('detail-rate').textContent = rate(s.pass_rate);
    $('detail-counts').textContent = [s.passed,s.failed,s.total].map(n => n == null ? '—' : n).join(' / ');
    $('detail-trigger').textContent = source(data.trigger);
    const build = buildUrl(data.jenkins_build); $('report-jenkins-link').classList.toggle('hidden',!build);
    if(build) $('report-jenkins-link').href = build + 'console';
    $('snapshot-content').textContent = data.config_snapshot ? JSON.stringify(data.config_snapshot,null,2) : '该历史报告生成于配置快照功能上线前，原检查明细保持不变。';
    renderChecks();
  }
  function renderChecks() {
    const all = state.detail && state.detail.checks || [], list = $('failures-only').checked ? all.filter(c => !c.ok) : all;
    $('check-list').innerHTML = list.map(c => {
      const url = publicUrl(c.public_url || c.url), failed = !c.ok;
      return '<article class="check-item"><header><div class="check-title"><h3>'+esc(c.name || c.id)+'</h3><span class="mono muted">'+esc(c.id)+'</span></div>'+badge(c.ok ? 'passed' : 'failed')+'</header><div class="check-meta"><span class="mono">'+esc(c.method || 'GET')+' '+(url ? '<a href="'+esc(url)+'" target="_blank" rel="noopener">'+esc(url)+'</a>' : '—')+'</span><span>HTTP <strong>'+esc(c.status_code == null ? '—' : c.status_code)+'</strong></span><span>耗时 '+esc(duration(c.duration_ms))+'</span></div>'+(failed ? '<div class="error-text">失败原因：'+esc(c.error || '检查未通过，请查看执行日志')+'</div>' : '')+'<details '+(failed ? 'open' : '')+'><summary>断言与执行日志</summary><pre>'+esc('期望：'+(c.expected || 'HTTP 200')+'\n\n'+(c.log || c.error || '暂无详细日志'))+'</pre></details></article>';
    }).join('') || '<div class="panel empty">'+($('failures-only').checked ? '本次巡检没有失败项。' : '暂无检查明细。')+'</div>';
  }
  function renderEvents(list, mail) {
    const status = mail || {label:'未配置',reason:'发信服务未配置',counts:{}};
    $('mail-status-label').textContent = '邮件通知：'+status.label;
    $('mail-status-reason').textContent = status.reason || '等待新的状态变化';
    const counts = status.counts || {};
    $('mail-status-counts').textContent = '待发 '+((counts.pending||0)+(counts.sending||0))+' · 已发送 '+(counts.sent||0)+' · 失败 '+(counts.failed||0)+(status.baseline_at ? ' · 启用基线 '+stamp(status.baseline_at)+'（北京时间）' : '');
    $('events-body').innerHTML = list.map(e => {
      const n=e.notification || {status:'unconfigured',label:'未配置',attempts:0,max_attempts:5};
      const cls=n.status==='sent'?'pass':n.status==='failed'?'fail':'unknown';
      const detail = (n.attempts?'尝试 '+n.attempts+' / '+n.max_attempts+' 次':'尚未投递')+(n.next_attempt_at?' · 下次 '+stamp(n.next_attempt_at):'')+(n.sent_at?' · '+stamp(n.sent_at):'');
      return '<tr><td class="timestamp">'+esc(stamp(e.occurred_at))+'</td><td><span class="badge '+(e.kind==='failure'?'fail':'pass')+'">'+(e.kind==='failure'?'失败':'恢复')+'</span></td><td>'+esc(e.task_name)+'<div class="row-sub mono">'+esc(e.task_id)+'</div></td><td class="cell-wrap">HTTP '+esc(e.status_code)+'<div class="row-sub">'+esc(e.message)+'</div></td><td><span class="badge '+cls+'">'+esc(n.label)+'</span>'+(n.failure_event_id?'<div class="row-sub">关联故障 #'+esc(n.failure_event_id)+'</div>':'')+'</td><td class="cell-wrap">'+esc(detail)+(n.reason?'<div class="row-sub">'+esc(n.reason)+'</div>':'')+'</td><td class="right"><a href="'+reportUrl(e.run_id)+'">查看报告</a></td></tr>';
    }).join('') || '<tr><td colspan="7" class="empty">暂无失败或恢复事件。正常通过不产生告警；未配置发信服务时不会发送邮件。</td></tr>';
  }
  const releaseLabels = {frontend:'前端', backend:'后端', deploy:'发布', rollback:'回滚'};
  const releaseStates = {deploying:['wait','发布中'], succeeded:['pass','已成功'], failed:['fail','未成功'], rolled_back:['back','已回滚']};
  const inspectLabels = {disabled:'巡检未启用', skipped:'未启动巡检', error:'巡检未启动', passed:'巡检通过', failed:'巡检失败', unknown:'巡检结果未知'};
  const releaseWhen = value => stamp(value).replace(/\.\d+/, '');
  function releaseMessage(text, failed) {
    const node = $('release-message');
    node.textContent = text || '';
    node.className = 'notice' + (failed ? ' error' : '') + (text ? '' : ' hidden');
  }
  function releaseStateBadge(status) {
    const item = releaseStates[status] || ['unknown', status || '未知'];
    return '<span class="badge '+item[0]+'">'+esc(item[1])+'</span>';
  }
  function renderReleases() {
    const snapshot = state.releases || {services:{}, records:[], unavailable:true};
    const services = snapshot.services || {};
    $('release-host').textContent = snapshot.host_valid ? ('目标 ' + snapshot.host) : (snapshot.unavailable ? '发布记录暂时无法读取' : '主机配置无效');
    $('release-lead').textContent = '记录前端或后端发到 ' + (snapshot.host || '本服务器') + ' 的结果。查看不需要登录；发布、回滚、刷新结果和标记未确认需要发布口令。';
    $('release-summary').innerHTML = ['frontend','backend'].map(key => {
      const item = services[key] || {label:releaseLabels[key], wired:false, locked:false, current_id:''};
      const current = (snapshot.records || []).find(row => row.id === item.current_id);
      const detail = current ? (current.version || current.commit_sha || current.artifact || current.id) : '尚无生效发布';
      const wired = item.wired ? '发布任务已配置' : '发布任务未配置';
      return '<article><span class="muted">'+esc(item.label || releaseLabels[key])+' · '+esc(wired)+(item.locked ? ' · 发布进行中' : '')+'</span><strong>'+esc(detail)+'</strong></article>';
    }).join('');
    const rows = snapshot.records || [];
    $('release-count').textContent = snapshot.unavailable ? '暂时无法读取' : (rows.length ? ('共 '+rows.length+' 条') : '尚无记录');
    $('release-body').innerHTML = rows.length ? rows.map(row => {
      const identity = [row.version, row.commit_sha, row.artifact].filter(Boolean).map(esc).join('<br>') || '—';
      const inspectLabel = inspectLabels[row.inspect_status] || row.inspect_status || '';
      const build = /^https?:\/\//.test(row.jenkins_build_url || '') ? '<a href="'+esc(row.jenkins_build_url)+'" rel="noopener noreferrer">Jenkins 构建</a>' : '';
      const inspect = row.inspect_run_id ? ('巡检 '+esc(row.inspect_run_id)+(inspectLabel ? ' · '+esc(inspectLabel) : '')) : esc(inspectLabel);
      const service = services[row.service] || {};
      const buttons = [];
      if (row.state === 'deploying') {
        buttons.push('<button type="button" class="link-button" data-release-refresh="'+esc(row.id)+'">刷新结果</button>');
        if (row.can_abandon) buttons.push('<button type="button" class="link-button" data-release-abandon="'+esc(row.id)+'">标记未确认</button>');
      }
      if (service.current_id === row.id && service.can_rollback) buttons.push('<button type="button" class="link-button" data-release-rollback="'+esc(row.id)+'">回滚到上一成功发布</button>');
      return '<tr><td class="timestamp">'+esc(releaseWhen(row.created_at))+'<div class="row-sub">'+esc(releaseLabels[row.action] || row.action)+'</div></td><td><span class="service-tag">'+esc(releaseLabels[row.service] || row.service)+'</span></td><td>'+esc(row.actor)+'</td><td class="cell-wrap mono">'+identity+'</td><td>'+releaseStateBadge(row.state)+'</td><td class="cell-wrap">'+esc(row.result_message || '')+'<div class="row-sub">'+build+(inspect ? ' '+inspect : '')+'</div></td><td><div class="row-actions">'+(buttons.join('') || '<span class="muted">—</span>')+'</div></td></tr>';
    }).join('') : '<tr><td colspan="7" class="empty">'+(snapshot.unavailable ? '发布记录暂时无法读取。' : '尚无发布到本服务器的记录。')+'</td></tr>';
    const selected = $('release-service').value;
    $('release-submit').disabled = state.releaseBusy || !!(services[selected] && services[selected].locked);
  }
  function askPassword(purpose) {
    if (askPassword.pending) return Promise.resolve(null);
    const gate = $('release-gate'), input = $('release-gate-password'), errorBox = $('release-gate-error');
    $('release-gate-text').textContent = purpose + '口令只发到服务器核对，页面不保存。';
    errorBox.textContent = ''; errorBox.classList.add('hidden'); input.value = '';
    gate.classList.remove('hidden'); gate.setAttribute('aria-hidden','false'); input.focus();
    askPassword.pending = new Promise(resolve => {
      const finish = value => {
        input.value = ''; gate.classList.add('hidden'); gate.setAttribute('aria-hidden','true');
        form.removeEventListener('submit', onSubmit); cancel.removeEventListener('click', onCancel); document.removeEventListener('keydown', onKey);
        askPassword.pending = null; resolve(value);
      };
      const onSubmit = event => {
        event.preventDefault();
        const value = input.value; input.value = '';
        if (!value) { errorBox.textContent = '请输入操作口令'; errorBox.classList.remove('hidden'); return; }
        finish(value);
      };
      const onCancel = () => finish(null);
      const onKey = event => { if (event.key === 'Escape') finish(null); };
      const form = $('release-gate-form'), cancel = $('release-gate-cancel');
      form.addEventListener('submit', onSubmit); cancel.addEventListener('click', onCancel); document.addEventListener('keydown', onKey);
    });
    return askPassword.pending;
  }
  async function releasePost(path, fields, purpose) {
    const password = await askPassword(purpose);
    if (!password) return null;
    const body = Object.assign({}, fields, {password: password});
    try { return await api(path, {method:'POST', body:JSON.stringify(body)}); }
    finally { body.password = ''; }
  }
  async function refresh() {
    const version = ++state.refreshVersion; error('');
    const route = currentRoute(); setPage(route.name === 'reports' && route.id ? 'report' : route.name);
    try {
      if(route.name === 'releases') {
        const data = await api('/releases'); if(version !== state.refreshVersion) return;
        state.releases = data.data; renderReleases();
      } else if(route.name === 'events') {
        const events = await api('/events'); if(version !== state.refreshVersion) return; renderEvents(events.data,events.mail);
      } else if(route.name === 'reports' && route.id) {
        const res = await api('/reports/'+encodeURIComponent(route.id));
        if(version !== state.refreshVersion) return; renderDetail(res.data);
      } else {
        const data = await api('/overview'); if(version !== state.refreshVersion) return;
        state.overview = data.data; renderOverview(state.overview);
        if(route.name === 'reports') { const reports = await api('/reports?limit=200'); if(version !== state.refreshVersion) return; state.reports = reports.data; renderReports(); }
      }
    } catch(err) { error(err.message); }
  }
  async function run() {
    if(state.running) return;
    state.running = true; const buttons = document.querySelectorAll('[data-run]');
    buttons.forEach(node => {node.disabled = true; node.dataset.original = node.textContent; node.textContent = '执行中…';});
    const status = $('run-status'); status.textContent = '正在提交巡检任务…'; status.classList.remove('hidden');
    try {
      // Snapshot IDs from the server immediately before triggering; do not depend on current page state.
      const before = await api('/reports?limit=200'); const ids = new Set(before.data.map(r => r.id));
      const result = await api('/runs', {method:'POST',body:JSON.stringify({trigger:'manual',mode:'jenkins'})});
      if(result.mode === 'jenkins') {
        status.textContent = '任务已提交至 Jenkins，等待巡检报告…';
        const deadline = Date.now()+120000; let found = null;
        while(Date.now()<deadline) {
          await new Promise(resolve => setTimeout(resolve,2500));
          const response = await api('/reports?limit=200');
          found = response.data.find(r => !ids.has(r.id)); if(found) break;
        }
        if(found) {
          toast('巡检完成：'+(found.status === 'passed' ? '通过' : found.status === 'failed' ? '失败' : '未执行')+'，运行 '+found.id);
          location.hash = reportUrl(found.id); await refresh();
        } else { toast('任务已提交，暂未取得新报告。可稍后刷新或在 Jenkins 查看排队状态。'); await refresh(); }
      } else {
        toast(result.warning || '本机巡检已完成');
        const report = result.data && result.data.report;
        if(report) location.hash = reportUrl(report.id); await refresh();
      }
    } catch(err) { toast(err.message); }
    finally { state.running = false; status.classList.add('hidden'); buttons.forEach(node => {node.disabled = false; node.textContent = node.dataset.original || '立即巡检';}); }
  }
  document.querySelectorAll('[data-run]').forEach(node => node.addEventListener('click',run));
  document.querySelectorAll('[data-refresh]').forEach(node => node.addEventListener('click',refresh));
  $('report-search').addEventListener('input', () => {state.page=1;renderReports();});
  $('report-filter').addEventListener('change', () => {state.page=1;renderReports();});
  $('prev-page').addEventListener('click', () => {state.page--;renderReports();});
  $('next-page').addEventListener('click', () => {state.page++;renderReports();});
  function taskMessage(text, failed) {
    const node = $('task-message');
    node.textContent = text || '';
    node.className = 'notice' + (failed ? ' error' : '') + (text ? '' : ' hidden');
  }
  function taskPayload(task, enabled) {
    return {
      name: task.name,
      url: task.url,
      method: task.method,
      expected_status: task.expected_status,
      contains: task.contains,
      timeout_sec: task.timeout_sec,
      enabled: enabled,
      revision: task.revision
    };
  }
  function openTaskEditor(task) {
    state.taskEditing = task || null;
    const form = $('task-form');
    form.reset();
    $('task-name').value = task ? task.name : '';
    $('task-url').value = task ? task.url : '';
    $('task-url').readOnly = !!(task && task.builtin);
    $('task-method').value = task ? task.method : 'GET';
    $('task-codes').value = task ? task.expected_status.join(', ') : '200';
    $('task-timeout').value = task ? task.timeout_sec : 10;
    $('task-contains').value = task ? task.contains : '';
    $('task-enabled').checked = task ? task.enabled : true;
    $('task-editor-title').textContent = task ? ('编辑检查 · ' + task.name) : '新建检查';
    $('task-url-help').textContent = task && task.builtin ? '内置地址固定；如需其他地址请新建检查。' : '只支持公开 HTTP:80 / HTTPS:443，不允许查询参数或凭证。';
    $('task-editor').classList.remove('hidden');
    $('task-name').focus();
  }
  $('failures-only').addEventListener('change',renderChecks);
  $('task-create').addEventListener('click', () => openTaskEditor(null));
  $('task-cancel').addEventListener('click', () => $('task-editor').classList.add('hidden'));
  $('task-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (state.taskBusy) return;
    const codes = $('task-codes').value.split(/[,，]/).map(item => item.trim()).filter(Boolean).map(Number);
    if (!codes.length || codes.some(item => !Number.isInteger(item))) { taskMessage('状态码必须为整数。', true); return; }
    const fields = {
      name: $('task-name').value.trim(),
      url: $('task-url').value.trim(),
      method: $('task-method').value,
      expected_status: codes,
      contains: $('task-contains').value,
      timeout_sec: parseInt($('task-timeout').value, 10),
      enabled: $('task-enabled').checked
    };
    if (state.taskEditing) fields.revision = state.taskEditing.revision;
    const path = '/tasks' + (state.taskEditing ? '/' + encodeURIComponent(state.taskEditing.id) : '');
    state.taskBusy = true; $('task-save').disabled = true;
    try {
      const result = await releasePost(path, fields, state.taskEditing ? '保存这项检查。' : '新建检查。');
      if (!result) return;
      taskMessage('配置已保存。下一轮巡检使用新配置，已有报告保持原快照。', false);
      $('task-editor').classList.add('hidden');
      await refresh();
    } catch (err) { taskMessage(err.message, true); }
    finally { state.taskBusy = false; $('task-save').disabled = false; }
  });
  $('task-checks-body').addEventListener('click', async event => {
    const edit = event.target.closest('[data-task-edit]');
    const toggle = event.target.closest('[data-task-toggle]');
    if (edit) { openTaskEditor(state.tasks.find(task => task.id === edit.getAttribute('data-task-edit'))); return; }
    if (!toggle || state.taskBusy) return;
    const task = state.tasks.find(item => item.id === toggle.getAttribute('data-task-toggle'));
    if (!task) return;
    state.taskBusy = true; toggle.disabled = true;
    try {
      const result = await releasePost('/tasks/' + encodeURIComponent(task.id), taskPayload(task, !task.enabled), task.enabled ? '停用这项检查。' : '启用这项检查。');
      if (!result) return;
      taskMessage('状态已保存，将在下一轮巡检生效。', false);
      await refresh();
    } catch (err) { taskMessage(err.message, true); }
    finally { state.taskBusy = false; }
  });
  $('release-service').addEventListener('change', () => { if (state.releases) renderReleases(); });
  $('release-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (state.releaseBusy) return;
    const fields = {
      service: $('release-service').value,
      actor: $('release-actor').value.trim(),
      version: $('release-version').value.trim(),
      commit: $('release-commit').value.trim(),
      artifact: $('release-artifact').value.trim()
    };
    if (!fields.actor || (!fields.version && !fields.commit)) { releaseMessage('请填写操作者，以及版本或提交 SHA。', true); return; }
    state.releaseBusy = true; if (state.releases) renderReleases();
    try {
      const result = await releasePost('/releases', fields, '发布到本服务器。');
      if (!result) return;
      releaseMessage('发布任务已提交。在 Jenkins 给出结果前，状态保持为发布中。', false);
      await refresh();
    } catch (err) { releaseMessage(err.message, true); try { await refresh(); } catch (loadErr) { error(loadErr.message); } }
    finally { state.releaseBusy = false; if (state.releases) renderReleases(); }
  });
  $('release-body').addEventListener('click', async event => {
    const button = event.target.closest('[data-release-refresh],[data-release-rollback],[data-release-abandon]');
    if (!button || state.releaseBusy) return;
    const id = button.getAttribute('data-release-refresh') || button.getAttribute('data-release-rollback') || button.getAttribute('data-release-abandon');
    const kind = button.hasAttribute('data-release-refresh') ? 'refresh' : button.hasAttribute('data-release-rollback') ? 'rollback' : 'abandon';
    const actor = $('release-actor').value.trim();
    if (kind !== 'refresh' && !actor) { releaseMessage('请先填写操作者。', true); return; }
    if (kind === 'rollback' && !window.confirm('确认回滚该服务到上一次成功发布？')) return;
    if (kind === 'abandon' && !window.confirm('只有在 Jenkins 没有对应构建时才应标记未确认。确认继续？')) return;
    state.releaseBusy = true;
    try {
      const purpose = kind === 'refresh' ? '刷新发布结果。' : kind === 'rollback' ? '回滚到上一成功发布。' : '把未确认的发布标记为未发布。';
      const fields = kind === 'refresh' ? {} : {actor: actor};
      const result = await releasePost('/releases/' + encodeURIComponent(id) + '/' + kind, fields, purpose);
      if (!result) return;
      releaseMessage(kind === 'refresh' ? '已读取 Jenkins 结果。' : kind === 'rollback' ? '回滚任务已提交。' : '已标记为未确认。', false);
      await refresh();
    } catch (err) { releaseMessage(err.message, true); try { await refresh(); } catch (loadErr) { /* keep the action error */ } }
    finally { state.releaseBusy = false; if (state.releases) renderReleases(); }
  });
  window.addEventListener('hashchange', () => {$('failures-only').checked=false;refresh();});
  refresh();
})();
