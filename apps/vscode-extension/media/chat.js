/* AI QA Engineer chat panel — webview controller.
 *
 * Renders the live agent timeline, approval cards and the run summary. All DOM
 * construction goes through createElement/textContent, never innerHTML with
 * server data, so nothing arriving over the socket can inject markup.
 */
/* eslint-disable no-undef */
(function () {
  'use strict';

  const vscode = acquireVsCodeApi();

  const els = {
    timeline: document.getElementById('timeline'),
    prompt: document.getElementById('prompt'),
    send: document.getElementById('send'),
    mode: document.getElementById('mode'),
    hint: document.getElementById('hint'),
    banner: document.getElementById('banner'),
    statusDot: document.getElementById('statusDot'),
    statusText: document.getElementById('statusText'),
    tokenMeter: document.getElementById('tokenMeter'),
    costMeter: document.getElementById('costMeter'),
    cancelBtn: document.getElementById('cancelBtn'),
  };

  const state = {
    running: false,
    tokens: 0,
    cost: 0,
    steps: new Map(),
    progressBar: null,
  };

  // ------------------------------------------------------------------ //
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clearWelcome() {
    const welcome = els.timeline.querySelector('.welcome');
    if (welcome) welcome.remove();
  }

  function scroll() {
    els.timeline.scrollTop = els.timeline.scrollHeight;
  }

  function setStatus(kind, text) {
    els.statusDot.className = 'dot' + (kind ? ' ' + kind : '');
    els.statusText.textContent = text;
  }

  function banner(text, kind) {
    if (!text) {
      els.banner.hidden = true;
      return;
    }
    els.banner.hidden = false;
    els.banner.className = 'banner' + (kind ? ' ' + kind : '');
    els.banner.textContent = text;
  }

  function updateMeters() {
    els.tokenMeter.textContent = state.tokens.toLocaleString() + ' tok';
    els.costMeter.textContent = '$' + state.cost.toFixed(4);
  }

  function setProgress(value) {
    if (value === null || value === undefined) return;
    if (!state.progressBar) {
      const wrap = el('div', 'progress');
      const inner = el('div');
      wrap.appendChild(inner);
      els.hint.parentElement.appendChild(wrap);
      state.progressBar = inner;
    }
    state.progressBar.style.width = Math.max(0, Math.min(100, value * 100)) + '%';
  }

  // ------------------------------------------------------------------ //
  function stepFor(agent) {
    if (!agent) return null;
    if (state.steps.has(agent)) return state.steps.get(agent);

    const wrap = el('div', 'step active');
    const head = el('div', 'step-head');
    head.appendChild(el('span', null, agent.replace(/_/g, ' ')));
    const meta = el('span', 'step-meta muted', 'working…');
    head.appendChild(meta);
    const body = el('div', 'step-body');
    wrap.appendChild(head);
    wrap.appendChild(body);
    els.timeline.appendChild(wrap);

    const record = { wrap, head, meta, body };
    state.steps.set(agent, record);
    scroll();
    return record;
  }

  function addLine(agent, text, level) {
    const step = stepFor(agent);
    const line = el('div', 'line' + (level && level !== 'info' ? ' ' + level : ''), text);
    (step ? step.body : els.timeline).appendChild(line);
    scroll();
  }

  // ------------------------------------------------------------------ //
  function renderDiff(container, diffText) {
    const pre = el('pre');
    const lines = String(diffText || '').split('\n').slice(0, 400);
    lines.forEach(function (line) {
      let cls = null;
      if (line.startsWith('+') && !line.startsWith('+++')) cls = 'diff-add';
      else if (line.startsWith('-') && !line.startsWith('---')) cls = 'diff-del';
      else if (line.startsWith('@@') || line.startsWith('---') || line.startsWith('+++')) cls = 'diff-hdr';
      pre.appendChild(el('div', cls, line));
    });
    if (String(diffText || '').split('\n').length > 400) {
      pre.appendChild(el('div', 'muted', '… truncated — open the full diff in the editor'));
    }
    container.appendChild(pre);
  }

  /**
   * The suite the design agent decided on.
   *
   * "test_design finished" tells the engineer nothing they can act on. What
   * they need is the scenarios: which risks were covered, which were skipped,
   * and whether the priorities match their own judgement — before any code is
   * written against them.
   */
  function renderPlan(data) {
    clearWelcome();
    const card = el('div', 'card plan');
    card.appendChild(el('div', 'card-title', data.title || 'Test plan'));
    if (data.strategy) {
      card.appendChild(el('div', 'muted strategy', data.strategy));
    }

    (data.features || []).forEach(function (feature) {
      const group = el('div', 'feature');
      const head = el('div', 'feature-head');
      head.appendChild(el('span', 'feature-name', feature.name || 'Feature'));
      if (feature.file) {
        head.appendChild(el('span', 'muted', feature.file));
      }
      group.appendChild(head);

      (feature.scenarios || []).forEach(function (scenario) {
        const row = el('div', 'scenario');
        const title = el('div', 'scenario-title');
        title.appendChild(el('span', 'scenario-id', scenario.id || ''));
        title.appendChild(el('span', null, scenario.name || ''));
        if (scenario.negative) {
          title.appendChild(el('span', 'tag negative', 'negative'));
        }
        (scenario.tags || []).slice(0, 3).forEach(function (tag) {
          title.appendChild(el('span', 'tag', tag));
        });
        row.appendChild(title);

        // The steps are collapsed: a dozen scenarios' worth of Gherkin at once
        // buries the shape of the suite, which is what this view is for.
        const steps = el('details', 'steps');
        const summary = document.createElement('summary');
        summary.textContent = (scenario.steps || []).length + ' steps';
        steps.appendChild(summary);
        (scenario.steps || []).forEach(function (step) {
          steps.appendChild(el('div', 'step-line', step));
        });
        row.appendChild(steps);
        group.appendChild(row);
      });
      card.appendChild(group);
    });

    const reuse = [];
    if ((data.reused_pages || []).length) {
      reuse.push('reusing ' + data.reused_pages.join(', '));
    }
    if ((data.new_pages || []).length) {
      reuse.push('new: ' + data.new_pages.join(', '));
    }
    if (reuse.length) {
      card.appendChild(el('div', 'muted', reuse.join('  ·  ')));
    }

    els.timeline.appendChild(card);
    scroll();
  }

  /**
   * Every file the agents wrote, with its diff and a way into the editor.
   *
   * This is the review surface. A webview is a fine place to *notice* what
   * changed and a poor place to edit it, so each card opens the real file.
   */
  function renderFiles(data, applied) {
    clearWelcome();
    const files = data.files || [];
    if (!files.length) {
      return;
    }
    const card = el('div', 'card files');
    const todos = files.reduce(function (sum, f) { return sum + (f.todos || 0); }, 0);
    card.appendChild(
      el(
        'div',
        'card-title',
        files.length + (applied ? ' file(s) written to disk' : ' file(s) generated'),
      ),
    );
    if (todos) {
      card.appendChild(
        el(
          'div',
          'muted',
          todos + ' TODO(aiqa)/fixme marker(s) — places the agents refused to guess',
        ),
      );
    }

    files.forEach(function (file) {
      const row = el('div', 'file');
      const head = el('div', 'file-head');

      const open = el('button', 'linkish', file.path);
      open.title = 'Open in the editor';
      open.addEventListener('click', function () {
        vscode.postMessage({ type: 'openFile', path: file.path });
      });
      head.appendChild(open);

      const meta = [];
      if (file.change_type) meta.push(file.change_type);
      if (file.lines) meta.push(file.lines + ' lines');
      if (file.todos) meta.push(file.todos + ' todo');
      head.appendChild(el('span', 'muted', meta.join(' · ')));
      row.appendChild(head);

      if (file.rationale) {
        row.appendChild(el('div', 'muted rationale', file.rationale));
      }

      if (file.diff) {
        const details = el('details', 'file-diff');
        const summary = document.createElement('summary');
        summary.textContent = 'diff';
        details.appendChild(summary);
        renderDiff(details, file.diff);
        if (file.diff_truncated) {
          details.appendChild(el('div', 'muted', '… open the file for the rest'));
        }
        row.appendChild(details);
      }
      card.appendChild(row);
    });

    els.timeline.appendChild(card);
    scroll();
  }

  function renderApproval(approval) {
    clearWelcome();
    const card = el('div', 'approval');
    card.dataset.approvalId = approval.id;

    const head = el('h3');
    head.appendChild(el('span', null, approval.title));
    head.appendChild(document.createTextNode(' '));
    head.appendChild(el('span', 'risk', approval.risk));
    card.appendChild(head);

    if (approval.description) {
      const desc = el('div', 'line muted');
      desc.style.whiteSpace = 'pre-wrap';
      desc.textContent = approval.description;
      card.appendChild(desc);
    }

    const files = (approval.payload && approval.payload.files) || [];
    if (files.length) {
      const list = el('div', 'files');
      files.forEach(function (file) {
        const button = el('button', 'file-link', file.path + (file.bytes ? '  (' + file.bytes + ' B)' : ''));
        button.addEventListener('click', function () {
          vscode.postMessage({ command: 'openFile', path: file.path });
        });
        list.appendChild(button);
      });
      card.appendChild(list);
    }

    if (approval.diff_preview) {
      renderDiff(card, approval.diff_preview);
    }

    const actions = el('div', 'actions');
    const approve = el('button', 'primary', 'Approve');
    const reject = el('button', 'danger', 'Reject');
    const openDiff = el('button', 'ghost', 'Open in diff editor');
    const comment = el('input', 'comment');
    comment.type = 'text';
    comment.placeholder = 'Optional comment (recorded in the audit log)';

    approve.addEventListener('click', function () {
      vscode.postMessage({ command: 'approve', approvalId: approval.id, comment: comment.value });
      approve.disabled = reject.disabled = true;
      approve.textContent = 'Approving…';
    });
    reject.addEventListener('click', function () {
      vscode.postMessage({ command: 'reject', approvalId: approval.id, comment: comment.value });
      approve.disabled = reject.disabled = true;
      reject.textContent = 'Rejecting…';
    });
    openDiff.addEventListener('click', function () {
      vscode.postMessage({ command: 'viewDiff', approvalId: approval.id });
    });

    actions.appendChild(approve);
    actions.appendChild(reject);
    actions.appendChild(openDiff);
    actions.appendChild(comment);
    card.appendChild(actions);

    els.timeline.appendChild(card);
    scroll();
  }

  function renderSummary(run) {
    const report = run.report || {};
    const failed = (run.tests_failed || 0) > 0;
    const defects = (report.product_defects || []).length > 0;
    const card = el('div', 'summary ' + (failed ? 'fail' : defects ? 'warn' : 'pass'));

    card.appendChild(el('h3', null, report.headline || run.status));

    const stats = el('div', 'stats');
    [
      [run.scenarios || 0, 'scenarios'],
      [run.files_changed || 0, 'files'],
      [(run.tests_passed || 0) + '/' + (run.tests_total || 0), 'tests passing'],
      ['$' + Number(run.total_cost_usd || 0).toFixed(4), 'cost'],
      [Number(run.total_tokens || 0).toLocaleString(), 'tokens'],
      [Number(run.duration_s || 0).toFixed(1) + 's', 'duration'],
    ].forEach(function (pair) {
      const stat = el('div', 'stat');
      stat.appendChild(el('span', 'v', pair[0]));
      stat.appendChild(el('span', 'k', pair[1]));
      stats.appendChild(stat);
    });
    card.appendChild(stats);

    if ((report.product_defects || []).length) {
      card.appendChild(el('div', 'line error', 'Suspected product defects (not auto-repaired):'));
      const list = el('ul');
      report.product_defects.forEach(function (defect) {
        list.appendChild(el('li', null, defect));
      });
      card.appendChild(list);
    }

    if ((report.next_actions || []).length) {
      card.appendChild(el('div', 'line', 'Next actions:'));
      const list = el('ul');
      report.next_actions.forEach(function (action) {
        list.appendChild(el('li', null, action));
      });
      card.appendChild(list);
    }

    const actions = el('div', 'actions');
    const reportBtn = el('button', 'ghost', 'Open full report');
    reportBtn.addEventListener('click', function () {
      vscode.postMessage({ command: 'showReport' });
    });
    actions.appendChild(reportBtn);
    card.appendChild(actions);

    els.timeline.appendChild(card);
    scroll();
  }

  // ------------------------------------------------------------------ //
  function handleEvent(event) {
    const agent = event.agent || '';

    switch (event.type) {
      case 'run_started':
        setStatus('connected', 'running');
        banner('');
        break;

      case 'plan_ready':
        renderPlan(event.data || {});
        break;

      case 'files_generated':
        renderFiles(event.data || {}, false);
        break;

      case 'files_applied':
        addLine(agent, event.message || 'files applied', 'info');
        break;

      case 'agent_started':
        stepFor(agent);
        break;

      case 'agent_finished': {
        const step = stepFor(agent);
        if (step) {
          step.wrap.className = 'step done';
          const data = event.data || {};
          const parts = [];
          if (data.tokens) parts.push(Number(data.tokens).toLocaleString() + ' tok');
          if (data.cost_usd) parts.push('$' + Number(data.cost_usd).toFixed(5));
          if (data.latency_ms) parts.push(data.latency_ms + ' ms');
          step.meta.textContent = parts.join(' · ') || 'done';
          if (event.message) step.body.appendChild(el('div', 'line', event.message));
        }
        break;
      }

      case 'agent_suspended': {
        const step = stepFor(agent);
        if (step) {
          step.wrap.className = 'step suspended';
          step.meta.textContent = 'waiting for you';
        }
        break;
      }

      case 'agent_failed': {
        const step = stepFor(agent);
        if (step) {
          step.wrap.className = 'step failed';
          step.meta.textContent = 'failed';
          step.body.appendChild(el('div', 'line error', event.message || 'failed'));
        }
        break;
      }

      case 'agent_skipped':
        addLine(agent || 'orchestrator', event.message, 'info');
        break;

      case 'log':
        addLine(agent, event.message, event.level);
        break;

      case 'tool_call': {
        const data = event.data || {};
        addLine(agent, (data.tool || 'tool') + '  ' + (data.args || ''), 'tool');
        break;
      }

      case 'llm_call': {
        const data = event.data || {};
        state.tokens += Number(data.prompt_tokens || 0) + Number(data.completion_tokens || 0);
        state.cost += Number(data.cost_usd || 0);
        updateMeters();
        break;
      }

      case 'approval_required':
        setStatus('', 'waiting for your approval');
        break;

      case 'approval_resolved':
        addLine('orchestrator', event.message, 'info');
        break;

      case 'audit':
        addLine(agent, event.message, 'tool');
        break;

      case 'run_finished':
        state.running = false;
        setStatus('connected', 'finished');
        els.cancelBtn.hidden = true;
        setProgress(1);
        break;

      case 'run_failed':
        state.running = false;
        setStatus('error', 'failed');
        els.cancelBtn.hidden = true;
        banner(event.message || 'the run failed', 'error');
        break;

      default:
        break;
    }

    if (event.progress !== undefined && event.progress !== null) {
      setProgress(event.progress);
    }
  }

  // ------------------------------------------------------------------ //
  window.addEventListener('message', function (messageEvent) {
    const message = messageEvent.data;

    switch (message.type) {
      case 'context':
        if (!message.connected) {
          setStatus('error', 'not connected');
          banner('Cannot reach the control plane. Start it with `aiqa serve`. ' + (message.error || ''), 'error');
          return;
        }
        setStatus('connected', 'v' + message.version);
        if (message.offline) {
          banner(
            'No LLM provider is reachable — running in deterministic offline mode. ' +
              'Plans and code are still produced, but without model reasoning. ' +
              'Start Ollama or set OPENROUTER_API_KEY for full quality.',
          );
        }
        if (message.defaultMode) els.mode.value = message.defaultMode;
        if (!message.projectId) {
          els.hint.textContent = 'This workspace is not registered yet — it will be on your first run.';
        } else {
          const project = (message.projects || []).find(function (p) { return p.id === message.projectId; });
          els.hint.textContent = project
            ? project.name + (project.indexed ? ' · indexed' : ' · not indexed yet') +
              (project.base_url ? ' · ' + project.base_url : ' · no app URL configured')
            : '';
        }
        break;

      case 'runStarting':
        clearWelcome();
        state.steps.clear();
        state.tokens = 0;
        state.cost = 0;
        state.running = true;
        updateMeters();
        els.cancelBtn.hidden = false;
        els.timeline.appendChild(el('div', 'turn', message.instruction));
        setStatus('connected', 'starting ' + message.mode);
        scroll();
        break;

      case 'runCreated':
        els.hint.textContent = 'run ' + message.run.id;
        break;

      case 'runSnapshot':
        clearWelcome();
        state.tokens = Number(message.run.total_tokens || 0);
        state.cost = Number(message.run.total_cost_usd || 0);
        updateMeters();
        if (!els.timeline.querySelector('.turn')) {
          els.timeline.appendChild(el('div', 'turn', message.run.instruction));
        }
        break;

      case 'event':
        handleEvent(message.event);
        break;

      case 'approval':
        renderApproval(message.approval);
        break;

      case 'approvalResolved': {
        const card = els.timeline.querySelector('[data-approval-id="' + message.approvalId + '"]');
        if (card) {
          card.className = 'approval resolved';
          card.appendChild(el('div', 'line', message.approved ? 'Approved — continuing.' : 'Rejected — run stopped.'));
        }
        break;
      }

      case 'runFinished':
        renderSummary(message.run);
        state.running = false;
        els.cancelBtn.hidden = true;
        break;

      case 'streamClosed':
        if (state.running) {
          setStatus('', 'stream closed (' + message.reason + ')');
        }
        break;

      case 'error':
        banner(message.message, 'error');
        break;

      default:
        break;
    }
  });

  // ------------------------------------------------------------------ //
  function submit() {
    const text = els.prompt.value.trim();
    if (!text) return;
    vscode.postMessage({ command: 'submit', text: text, mode: els.mode.value });
    els.prompt.value = '';
  }

  els.send.addEventListener('click', submit);

  els.prompt.addEventListener('keydown', function (event) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  });

  els.mode.addEventListener('change', function () {
    vscode.postMessage({ command: 'setMode', mode: els.mode.value });
  });

  els.cancelBtn.addEventListener('click', function () {
    vscode.postMessage({ command: 'cancel' });
  });

  document.querySelectorAll('.example').forEach(function (button) {
    button.addEventListener('click', function () {
      els.prompt.value = button.textContent;
      els.prompt.focus();
    });
  });

  vscode.postMessage({ command: 'refresh' });
})();
