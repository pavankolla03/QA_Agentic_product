/* AI QA Engineer — control plane dashboard.
 *
 * Vanilla JS, no build step, no CDN. All DOM is built with createElement and
 * textContent so nothing from the API can inject markup.
 */
(function () {
  'use strict';

  const KEY_STORAGE = 'aiqa.apiKey';
  const state = {
    key: localStorage.getItem(KEY_STORAGE) || '',
    tab: 'overview',
    projects: [],
    runs: [],
    approvals: [],
    metrics: null,
    openRunId: null,
    timer: null,
  };

  // ---------------------------------------------------------------- //
  const $ = (id) => document.getElementById(id);

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  function alertBox(message, kind) {
    const box = $('alert');
    if (!message) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    box.className = 'alert' + (kind ? ' ' + kind : '');
    clear(box).appendChild(document.createTextNode(message));
  }

  function money(value) {
    return '$' + Number(value || 0).toFixed(4);
  }

  function ago(iso) {
    if (!iso) return '';
    const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (seconds < 60) return Math.round(seconds) + 's ago';
    if (seconds < 3600) return Math.round(seconds / 60) + 'm ago';
    if (seconds < 86400) return Math.round(seconds / 3600) + 'h ago';
    return Math.round(seconds / 86400) + 'd ago';
  }

  // ---------------------------------------------------------------- //
  async function api(path, options) {
    if (!state.key) {
      promptForKey();
      throw new Error('no API key');
    }
    const response = await fetch(path, {
      ...(options || {}),
      headers: { 'Content-Type': 'application/json', 'X-API-Key': state.key, ...((options || {}).headers || {}) },
    });
    if (response.status === 401) {
      localStorage.removeItem(KEY_STORAGE);
      state.key = '';
      promptForKey();
      throw new Error('API key rejected');
    }
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = await response.json();
        detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail || body);
      } catch { /* keep statusText */ }
      throw new Error(detail);
    }
    const type = response.headers.get('content-type') || '';
    return type.includes('json') ? response.json() : response.text();
  }

  function promptForKey() {
    const dialog = $('keyDialog');
    $('keyInput').value = state.key;
    dialog.showModal();
  }

  $('keyDialog').addEventListener('close', function () {
    if ($('keyDialog').returnValue === 'save') {
      const value = $('keyInput').value.trim();
      if (value) {
        state.key = value;
        localStorage.setItem(KEY_STORAGE, value);
        alertBox('');
        void refresh();
      }
    }
  });

  $('keyBtn').addEventListener('click', promptForKey);
  $('refreshBtn').addEventListener('click', () => void refresh());

  // ---------------------------------------------------------------- //
  $('tabs').addEventListener('click', function (event) {
    const button = event.target.closest('button[data-tab]');
    if (!button) return;
    state.tab = button.dataset.tab;
    document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('active', b === button));
    document.querySelectorAll('[data-panel]').forEach((p) => {
      p.hidden = p.dataset.panel !== state.tab;
    });
    void refresh();
  });

  // ---------------------------------------------------------------- //
  async function refresh() {
    try {
      const health = await api('/api/health');
      $('envLabel').textContent =
        'v' + health.version + ' · ' + health.env + ' · ' + health.database;

      const cost = health.cost || {};
      const pill = $('spendPill');
      pill.textContent = money(cost.spent_today_usd) + ' / ' + money(cost.daily_limit_usd);
      const used = Number(cost.daily_used_pct || 0);
      pill.className = 'pill' + (used >= 100 ? ' over' : used >= 75 ? ' warn' : '');

      const offline = Object.values(health.active_routes || {}).every(
        (r) => !r || r.provider === 'mock' || r.provider === 'hashing',
      );
      alertBox(
        offline
          ? 'No LLM provider is reachable — the platform is running in deterministic offline mode. ' +
            'Start Ollama (ollama serve) or set OPENROUTER_API_KEY for full reasoning quality.'
          : '',
      );

      renderRoutes(health.active_routes || {});
    } catch (err) {
      alertBox('Cannot reach the control plane: ' + err.message, 'error');
      return;
    }

    try {
      const [projects, runs, approvals] = await Promise.all([
        api('/api/projects'),
        api('/api/runs?limit=60'),
        api('/api/approvals'),
      ]);
      state.projects = projects;
      state.runs = runs;
      state.approvals = approvals;

      const badge = $('approvalBadge');
      badge.hidden = approvals.length === 0;
      badge.textContent = String(approvals.length);

      renderOverview();
      renderApprovals();
      renderRuns();
      renderProjects();
      if (state.tab === 'cost') await renderCost();
      if (state.tab === 'audit') await renderAudit();
      if (state.tab === 'overview') await renderAgents();
    } catch (err) {
      alertBox(err.message, 'error');
    }

    scheduleNextRefresh();
  }

  function scheduleNextRefresh() {
    if (state.timer) clearTimeout(state.timer);
    const active = state.runs.some((r) => r.status === 'running' || r.status === 'queued');
    state.timer = setTimeout(() => void refresh(), active ? 3000 : 20000);
  }

  // ---------------------------------------------------------------- //
  function statCard(value, label, sub, kind) {
    const card = el('div', 'stat-card' + (kind ? ' ' + kind : ''));
    card.appendChild(el('div', 'v', value));
    card.appendChild(el('div', 'k', label));
    if (sub) card.appendChild(el('div', 'sub', sub));
    return card;
  }

  function renderOverview() {
    const container = clear($('overviewCards'));
    const active = state.runs.filter((r) => r.status === 'running').length;
    const waiting = state.approvals.length;
    const succeeded = state.runs.filter((r) => r.status === 'succeeded').length;
    const scenarios = state.runs.reduce((sum, r) => sum + (r.scenarios || 0), 0);
    const files = state.runs.reduce((sum, r) => sum + (r.files_changed || 0), 0);
    const tests = state.runs.reduce((sum, r) => sum + (r.tests_total || 0), 0);
    const passed = state.runs.reduce((sum, r) => sum + (r.tests_passed || 0), 0);
    const spend = state.runs.reduce((sum, r) => sum + (r.total_cost_usd || 0), 0);

    container.appendChild(statCard(String(state.projects.length), 'projects', null));
    container.appendChild(statCard(String(active), 'running now', null, active ? 'warn' : null));
    container.appendChild(
      statCard(String(waiting), 'awaiting approval', waiting ? 'review needed' : 'all clear', waiting ? 'warn' : 'good'),
    );
    container.appendChild(statCard(String(succeeded) + '/' + state.runs.length, 'runs succeeded', null));
    container.appendChild(statCard(String(scenarios), 'scenarios generated', files + ' files written'));
    container.appendChild(
      statCard(
        tests ? Math.round((100 * passed) / tests) + '%' : '—',
        'test pass rate',
        tests ? passed + ' of ' + tests : 'nothing executed yet',
        tests && passed === tests ? 'good' : tests ? 'warn' : null,
      ),
    );
    container.appendChild(statCard(money(spend), 'spend (listed runs)', null));

    const recent = clear($('recentRuns'));
    const rows = state.runs.slice(0, 8);
    if (!rows.length) {
      recent.appendChild(el('div', 'empty', 'No runs yet. Start one from VS Code (Ctrl+Alt+Q) or `aiqa run start`.'));
      return;
    }
    rows.forEach((run) => recent.appendChild(runRow(run)));
  }

  function renderRoutes(routes) {
    const body = clear($('routesTable').querySelector('tbody'));
    const header = document.createElement('tr');
    ['capability', 'provider / model', 'cost'].forEach((label) => {
      const th = el('th', null, label);
      header.appendChild(th);
    });
    body.appendChild(header);

    Object.entries(routes).forEach(([capability, route]) => {
      if (capability === 'fast') {
        return; // v1 alias of `cheap`; listing both is noise
      }
      const tr = document.createElement('tr');
      tr.appendChild(el('td', null, capability));
      tr.appendChild(el('td', null, route ? route.provider + ' / ' + route.model : 'none available'));
      tr.appendChild(el('td', null, route ? (route.free ? 'free' : 'paid') : '—'));
      body.appendChild(tr);
    });
  }

  async function renderAgents() {
    const container = clear($('agentList'));
    try {
      const data = await api('/api/agents');
      data.agents.forEach((agent) => {
        const row = el('div', 'agent-row');
        row.appendChild(el('span', 'n', agent.name.replace(/_/g, ' ')));
        row.appendChild(el('span', 'c', agent.capability));
        row.appendChild(el('span', 'd', agent.description));
        container.appendChild(row);
      });
    } catch {
      container.appendChild(el('div', 'muted small', 'unavailable'));
    }
  }

  // ---------------------------------------------------------------- //
  function renderDiffInto(container, diffText) {
    const pre = el('pre', 'diff');
    String(diffText || '')
      .split('\n')
      .slice(0, 500)
      .forEach((line) => {
        let cls = null;
        if (line.startsWith('+') && !line.startsWith('+++')) cls = 'add';
        else if (line.startsWith('-') && !line.startsWith('---')) cls = 'del';
        else if (line.startsWith('@@') || line.startsWith('---') || line.startsWith('+++')) cls = 'hdr';
        pre.appendChild(el('div', cls, line));
      });
    container.appendChild(pre);
  }

  function renderApprovals() {
    const container = clear($('approvalList'));
    if (!state.approvals.length) {
      container.appendChild(el('div', 'empty', 'Nothing is waiting for a human decision.'));
      return;
    }

    state.approvals.forEach((approval) => {
      const card = el('div', 'card approval-card');

      const head = el('h3');
      head.appendChild(document.createTextNode(approval.title));
      head.appendChild(document.createTextNode(' '));
      head.appendChild(el('span', 'tag risk-' + approval.risk, 'risk ' + approval.risk));
      head.appendChild(document.createTextNode(' '));
      head.appendChild(el('span', 'tag', approval.kind));
      card.appendChild(head);

      card.appendChild(el('div', 'muted small', 'run ' + approval.run_id + ' · ' + ago(approval.created_at)));
      if (approval.description) card.appendChild(el('div', 'approval-desc', approval.description));

      const files = (approval.payload && approval.payload.files) || [];
      if (files.length) {
        const table = document.createElement('table');
        const tbody = document.createElement('tbody');
        const header = document.createElement('tr');
        ['file', 'kind', 'bytes'].forEach((label) => header.appendChild(el('th', null, label)));
        tbody.appendChild(header);
        files.forEach((file) => {
          const tr = document.createElement('tr');
          tr.appendChild(el('td', null, file.path));
          tr.appendChild(el('td', null, file.kind || ''));
          tr.appendChild(el('td', 'num', file.bytes || 0));
          tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        card.appendChild(table);
      }

      if (approval.diff_preview) renderDiffInto(card, approval.diff_preview);

      const actions = el('div', 'approval-actions');
      const comment = document.createElement('input');
      comment.type = 'text';
      comment.placeholder = 'Optional comment (recorded in the audit log)';

      const approve = el('button', 'primary', 'Approve');
      const reject = el('button', 'danger', 'Reject');

      approve.addEventListener('click', async function () {
        approve.disabled = reject.disabled = true;
        approve.textContent = 'Approving…';
        try {
          await api('/api/approvals/' + approval.id, {
            method: 'POST',
            body: JSON.stringify({ approved: true, comment: comment.value }),
          });
          await refresh();
        } catch (err) {
          alertBox(err.message, 'error');
          approve.disabled = reject.disabled = false;
          approve.textContent = 'Approve';
        }
      });

      reject.addEventListener('click', async function () {
        approve.disabled = reject.disabled = true;
        try {
          await api('/api/approvals/' + approval.id, {
            method: 'POST',
            body: JSON.stringify({ approved: false, comment: comment.value }),
          });
          await refresh();
        } catch (err) {
          alertBox(err.message, 'error');
          approve.disabled = reject.disabled = false;
        }
      });

      actions.appendChild(approve);
      actions.appendChild(reject);
      actions.appendChild(comment);
      card.appendChild(actions);
      container.appendChild(card);
    });
  }

  // ---------------------------------------------------------------- //
  function runRow(run) {
    const row = el('div', 'row');
    row.appendChild(el('span', 'dot ' + run.status));

    const grow = el('div', 'grow');
    grow.appendChild(el('div', 'title', run.instruction || run.id));
    const meta = [
      run.status + (run.status === 'running' && run.current_agent ? ' · ' + run.current_agent : ''),
      run.mode,
      run.scenarios ? run.scenarios + ' scenarios' : null,
      run.tests_total ? run.tests_passed + '/' + run.tests_total + ' passing' : null,
      run.files_changed ? run.files_changed + ' files' : null,
      money(run.total_cost_usd),
      run.duration_s ? run.duration_s.toFixed(1) + 's' : null,
      ago(run.created_at),
    ]
      .filter(Boolean)
      .join(' · ');
    grow.appendChild(el('div', 'meta', meta));
    if (run.error) grow.appendChild(el('div', 'meta', run.error.slice(0, 160)));
    row.appendChild(grow);

    const actions = el('div', 'actions');
    if (run.pending_approval_id) {
      const review = el('button', 'ghost', 'Review');
      review.addEventListener('click', function () {
        document.querySelector('#tabs button[data-tab="approvals"]').click();
      });
      actions.appendChild(review);
    }
    const open = el('button', 'ghost', 'Details');
    open.addEventListener('click', () => void openRun(run.id));
    actions.appendChild(open);

    if (run.status === 'running' || run.status === 'queued') {
      const cancel = el('button', 'ghost', 'Cancel');
      cancel.addEventListener('click', async function () {
        cancel.disabled = true;
        try {
          await api('/api/runs/' + run.id + '/cancel', { method: 'POST' });
          await refresh();
        } catch (err) {
          alertBox(err.message, 'error');
        }
      });
      actions.appendChild(cancel);
    }
    row.appendChild(actions);
    return row;
  }

  function renderRuns() {
    const projectFilter = $('runProjectFilter');
    if (projectFilter.options.length <= 1) {
      state.projects.forEach((project) => {
        const option = document.createElement('option');
        option.value = project.id;
        option.textContent = project.name;
        projectFilter.appendChild(option);
      });
    }

    const wantedProject = projectFilter.value;
    const wantedStatus = $('runStatusFilter').value;
    const container = clear($('runList'));
    const rows = state.runs.filter(
      (run) => (!wantedProject || run.project_id === wantedProject) && (!wantedStatus || run.status === wantedStatus),
    );
    if (!rows.length) {
      container.appendChild(el('div', 'empty', 'No runs match this filter.'));
      return;
    }
    rows.forEach((run) => container.appendChild(runRow(run)));
  }

  $('runProjectFilter').addEventListener('change', renderRuns);
  $('runStatusFilter').addEventListener('change', renderRuns);

  async function openRun(runId) {
    state.openRunId = runId;
    document.querySelector('#tabs button[data-tab="runs"]').click();
    const container = clear($('runDetail'));
    container.appendChild(el('div', 'muted small', 'loading…'));

    try {
      const [run, trace] = await Promise.all([api('/api/runs/' + runId), api('/api/runs/' + runId + '/trace')]);
      clear(container);

      const card = el('div', 'card detail');
      card.appendChild(el('h2', null, run.instruction));
      card.appendChild(
        el(
          'div',
          'muted small',
          [run.status, run.mode, money(run.total_cost_usd), run.total_tokens.toLocaleString() + ' tokens',
           run.llm_calls + ' LLM calls', run.duration_s.toFixed(1) + 's'].join(' · '),
        ),
      );

      if ((run.visited || []).length) {
        const path = el('div', 'path');
        path.appendChild(el('span', null, 'path:'));
        run.visited.forEach((node) => path.appendChild(el('span', 'node', node)));
        card.appendChild(path);
      }

      if (run.report && run.report.headline) {
        card.appendChild(el('h2', null, 'Outcome'));
        card.appendChild(el('div', null, run.report.headline));
        (run.report.next_actions || []).forEach((action) =>
          card.appendChild(el('div', 'muted small', '• ' + action)),
        );
      }

      if ((run.traces || []).length) {
        card.appendChild(el('h2', null, 'Agent trace'));
        run.traces.forEach((step) => {
          const row = el('div', 'trace-step');
          row.appendChild(el('span', 'agent', step.agent.replace(/_/g, ' ')));
          row.appendChild(el('span', 'out', step.output_summary || step.error || step.status));
          row.appendChild(
            el(
              'span',
              'nums',
              [step.model || '—', step.total_tokens.toLocaleString() + ' tok',
               '$' + step.cost_usd.toFixed(6), step.latency_ms + ' ms'].join('  '),
            ),
          );
          card.appendChild(row);
        });
      }

      const changes = (run.code_bundle && run.code_bundle.changes) || [];
      if (changes.length) {
        card.appendChild(el('h2', null, 'Generated files (' + changes.length + ')'));
        changes.forEach((change) => {
          const row = el('div', 'trace-step');
          row.appendChild(el('span', 'agent', change.kind));
          row.appendChild(el('span', 'out', change.path));
          row.appendChild(el('span', 'nums', change.bytes + ' B'));
          card.appendChild(row);
        });
      }

      if ((run.analyses || []).length) {
        card.appendChild(el('h2', null, 'Failure analysis'));
        run.analyses.forEach((analysis) => {
          const row = el('div', 'trace-step');
          row.appendChild(el('span', 'agent', analysis.category));
          row.appendChild(el('span', 'out', (analysis.test_id || '') + ' — ' + (analysis.root_cause || '')));
          row.appendChild(
            el('span', 'nums', (analysis.is_product_defect ? 'PRODUCT DEFECT  ' : '') +
              'conf ' + Number(analysis.confidence || 0).toFixed(2)),
          );
          card.appendChild(row);
        });
      }

      if ((run.heals || []).length) {
        card.appendChild(el('h2', null, 'Self-healing'));
        run.heals.forEach((heal) => {
          const row = el('div', 'trace-step');
          row.appendChild(el('span', 'agent', heal.strategy));
          row.appendChild(el('span', 'out', heal.test_id + ' — ' + (heal.explanation || '')));
          row.appendChild(
            el('span', 'nums', heal.verified ? 'verified' : heal.reverted ? 'reverted' : 'unverified'),
          );
          card.appendChild(row);
        });
      }

      const toolCalls = trace.tool_calls || [];
      if (toolCalls.length) {
        card.appendChild(el('h2', null, 'Tool calls (' + toolCalls.length + ')'));
        const table = document.createElement('table');
        const tbody = document.createElement('tbody');
        const header = document.createElement('tr');
        ['agent', 'tool', 'status', 'ms', 'arguments'].forEach((label) => header.appendChild(el('th', null, label)));
        tbody.appendChild(header);
        toolCalls.slice(0, 60).forEach((call) => {
          const tr = document.createElement('tr');
          tr.appendChild(el('td', null, call.agent || ''));
          tr.appendChild(el('td', null, call.tool));
          tr.appendChild(el('td', null, call.status));
          tr.appendChild(el('td', 'num', call.latency_ms));
          tr.appendChild(el('td', null, String(call.arguments || '').slice(0, 90)));
          tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        card.appendChild(table);
      }

      if ((run.warnings || []).length) {
        card.appendChild(el('h2', null, 'Warnings'));
        run.warnings.forEach((warning) => card.appendChild(el('div', 'muted small', '• ' + warning)));
      }

      const links = el('div', 'approval-actions');
      const diffBtn = el('button', 'ghost', 'View diff');
      diffBtn.addEventListener('click', async function () {
        const diff = await api('/api/runs/' + runId + '/diff');
        const holder = el('div', 'card');
        holder.appendChild(el('h2', null, 'Proposed changes'));
        renderDiffInto(holder, diff);
        container.appendChild(holder);
      });
      links.appendChild(diffBtn);

      if (run.report) {
        const reportLink = el('button', 'ghost', 'Open report');
        reportLink.addEventListener('click', function () {
          window.open('/api/runs/' + runId + '/report?format=html', '_blank');
        });
        links.appendChild(reportLink);
      }
      card.appendChild(links);
      container.appendChild(card);
    } catch (err) {
      clear(container).appendChild(el('div', 'alert error', err.message));
    }
  }

  // ---------------------------------------------------------------- //
  function renderProjects() {
    const container = clear($('projectList'));
    if (!state.projects.length) {
      container.appendChild(
        el('div', 'empty', 'No projects registered. Use `aiqa project add <name> <path>` or the VS Code extension.'),
      );
      return;
    }

    state.projects.forEach((project) => {
      const row = el('div', 'row');
      row.appendChild(el('span', 'dot ' + (project.indexed ? 'succeeded' : 'waiting_approval')));
      const grow = el('div', 'grow');
      grow.appendChild(el('div', 'title', project.name));
      grow.appendChild(
        el(
          'div',
          'meta',
          [
            project.repository_path,
            project.language + ' / ' + project.framework,
            project.base_url || 'no app URL',
            project.indexed ? 'indexed' : 'not indexed',
            'limit ' + money(project.per_run_cost_limit_usd) + '/run',
          ].join(' · '),
        ),
      );
      row.appendChild(grow);

      const actions = el('div', 'actions');
      const indexBtn = el('button', 'ghost', project.indexed ? 'Re-index' : 'Index');
      indexBtn.addEventListener('click', async function () {
        indexBtn.disabled = true;
        indexBtn.textContent = 'Indexing…';
        try {
          const result = await api('/api/projects/' + project.id + '/index', { method: 'POST' });
          alertBox(
            project.name + ': indexed ' + result.files + ' files, ' + result.symbols + ' symbols, ' +
              result.chunks + ' chunks (' + result.test_runner + (result.bdd ? ' + BDD' : '') + ')',
          );
          await refresh();
        } catch (err) {
          alertBox(err.message, 'error');
          indexBtn.disabled = false;
          indexBtn.textContent = 'Index';
        }
      });
      actions.appendChild(indexBtn);

      const lintBtn = el('button', 'ghost', 'Check standards');
      lintBtn.addEventListener('click', async function () {
        lintBtn.disabled = true;
        try {
          const report = await api('/api/projects/' + project.id + '/lint', { method: 'POST' });
          alertBox(
            project.name + ': ' + report.errors + ' error(s), ' + report.warnings + ' warning(s) across ' +
              report.files_checked + ' file(s)',
            report.errors ? 'error' : null,
          );
        } catch (err) {
          alertBox(err.message, 'error');
        }
        lintBtn.disabled = false;
      });
      actions.appendChild(lintBtn);
      row.appendChild(actions);
      container.appendChild(row);
    });
  }

  // ---------------------------------------------------------------- //
  async function renderCost() {
    const days = Number($('costWindow').value || 30);
    let metrics;
    try {
      metrics = await api('/api/metrics?days=' + days);
    } catch (err) {
      alertBox(err.message, 'error');
      return;
    }
    state.metrics = metrics;

    const cost = metrics.cost || {};
    const governance = cost.governance || {};
    const tests = metrics.tests || {};
    const healing = metrics.self_healing || {};

    const cards = clear($('costCards'));
    cards.appendChild(statCard(money(cost.total_usd), 'total spend', days + ' days'));
    cards.appendChild(statCard(Number(cost.total_tokens || 0).toLocaleString(), 'tokens', cost.llm_calls + ' calls'));
    cards.appendChild(statCard(money(cost.avg_per_run_usd), 'average per run', null));
    cards.appendChild(
      statCard(
        money(governance.spent_today_usd),
        'today',
        'of ' + money(governance.daily_limit_usd) + ' ceiling',
        Number(governance.daily_used_pct || 0) >= 75 ? 'warn' : 'good',
      ),
    );
    cards.appendChild(statCard(String(tests.scenarios_generated || 0), 'scenarios generated', (tests.files_generated || 0) + ' files'));
    cards.appendChild(
      statCard(
        (tests.pass_rate_pct || 0) + '%',
        'test pass rate',
        (tests.passed || 0) + ' of ' + (tests.total || 0),
        (tests.pass_rate_pct || 0) >= 90 ? 'good' : 'warn',
      ),
    );

    // daily bar chart
    const chart = clear($('costChart'));
    const byDay = cost.by_day || {};
    const entries = Object.entries(byDay);
    if (!entries.length) {
      chart.appendChild(el('div', 'muted small', 'no spend recorded in this window'));
    } else {
      const max = Math.max(...entries.map(([, value]) => value), 0.000001);
      entries.forEach(([day, value]) => {
        const bar = el('div', 'bar');
        bar.style.height = Math.max(2, (value / max) * 100) + '%';
        bar.appendChild(el('span', null, day + ': ' + money(value)));
        chart.appendChild(bar);
      });
    }

    tableFrom($('modelTable'), ['model', 'cost'], Object.entries(cost.by_model || {}).sort((a, b) => b[1] - a[1]),
      (row) => [row[0] || '—', money(row[1])]);

    tableFrom(
      $('agentCostTable'),
      ['agent', 'calls', 'tokens', 'cost', 'avg ms', 'failures'],
      metrics.agents || [],
      (agent) => [
        agent.agent, agent.invocations, Number(agent.tokens).toLocaleString(),
        money(agent.cost_usd), agent.avg_latency_ms, agent.failures,
      ],
    );

    const healingBox = clear($('healingStats'));
    if (!healing.proposed) {
      healingBox.appendChild(el('div', 'muted small', 'no repairs have been proposed yet'));
    } else {
      const grid = el('div', 'cards');
      grid.appendChild(statCard(String(healing.proposed), 'proposed'));
      grid.appendChild(statCard(String(healing.applied), 'applied'));
      grid.appendChild(statCard(String(healing.verified), 'verified', null, 'good'));
      grid.appendChild(statCard(String(healing.reverted), 'reverted', 'did not fix the failure', healing.reverted ? 'warn' : null));
      grid.appendChild(statCard(healing.success_rate_pct + '%', 'success rate'));
      healingBox.appendChild(grid);
      Object.entries(healing.by_strategy || {}).forEach(([strategy, count]) =>
        healingBox.appendChild(el('div', 'muted small', strategy + ': ' + count)),
      );
    }

    const flakyBox = clear($('flakyList'));
    const flaky = metrics.flaky_tests || [];
    if (!flaky.length) {
      flakyBox.appendChild(el('div', 'muted small', 'no flakiness detected'));
    } else {
      flaky.forEach((test) => {
        const row = el('div', 'row');
        const grow = el('div', 'grow');
        grow.appendChild(el('div', 'title', test.test_id + ' — ' + (test.name || '')));
        grow.appendChild(
          el('div', 'meta', test.file + ' · ' + test.runs + ' runs · ' + test.flakes + ' flakes · ' +
            test.flake_rate_pct + '% flake rate'),
        );
        row.appendChild(grow);
        if (test.quarantined) row.appendChild(el('span', 'tag risk-high', 'quarantined'));
        flakyBox.appendChild(row);
      });
    }
  }

  $('costWindow').addEventListener('change', () => void renderCost());

  function tableFrom(table, headers, rows, mapper) {
    const body = clear(table.querySelector('tbody'));
    const header = document.createElement('tr');
    headers.forEach((label, index) => header.appendChild(el('th', index ? 'num' : null, label)));
    body.appendChild(header);
    if (!rows.length) {
      const tr = document.createElement('tr');
      const td = el('td', 'muted small', 'no data');
      td.colSpan = headers.length;
      tr.appendChild(td);
      body.appendChild(tr);
      return;
    }
    rows.forEach((row) => {
      const tr = document.createElement('tr');
      mapper(row).forEach((cell, index) => tr.appendChild(el('td', index ? 'num' : null, cell)));
      body.appendChild(tr);
    });
  }

  // ---------------------------------------------------------------- //
  async function renderAudit() {
    const container = clear($('auditList'));
    try {
      const data = await api('/api/audit?limit=200');
      if (!data.count) {
        container.appendChild(el('div', 'empty', 'No audit entries yet.'));
        return;
      }
      const table = document.createElement('table');
      const tbody = document.createElement('tbody');
      const header = document.createElement('tr');
      ['when', 'action', 'outcome', 'resource', 'detail'].forEach((label) => header.appendChild(el('th', null, label)));
      tbody.appendChild(header);
      data.entries.forEach((entry) => {
        const tr = document.createElement('tr');
        tr.appendChild(el('td', null, ago(entry.at)));
        tr.appendChild(el('td', null, entry.action));
        const outcome = el('td');
        outcome.appendChild(el('span', 'tag ' + (entry.outcome === 'allowed' ? 'risk-low' : 'risk-high'), entry.outcome));
        tr.appendChild(outcome);
        tr.appendChild(el('td', null, String(entry.resource || '').slice(0, 70)));
        tr.appendChild(el('td', null, String(entry.detail || '').slice(0, 90)));
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      container.appendChild(table);
    } catch (err) {
      container.appendChild(el('div', 'alert error', err.message));
    }
  }

  // ---------------------------------------------------------------- //
  if (!state.key) {
    promptForKey();
  } else {
    void refresh();
  }
})();
