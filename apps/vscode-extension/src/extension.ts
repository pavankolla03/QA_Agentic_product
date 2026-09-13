/**
 * AI QA Engineer — VS Code extension entry point.
 *
 * Everything the QA engineer does goes through here: starting runs, reviewing
 * diffs, approving, inspecting traces and cost. The extension holds no QA logic
 * of its own — it is a client of the control plane, so the CLI, CI and the
 * dashboard all behave identically.
 */

import * as path from 'path';
import * as vscode from 'vscode';
import { ApiClient, ApiError, Approval, RunMode } from './client/apiClient';
import { ChatPanel, showDiffDocument } from './panels/chatPanel';
import { AgentsProvider, ApprovalItem, ApprovalsProvider, AgentsProvider as _A, RunsProvider, UsageProvider } from './views/trees';

let api: ApiClient;
let statusBar: vscode.StatusBarItem;
let approvalsProvider: ApprovalsProvider;
let runsProvider: RunsProvider;
let agentsProvider: AgentsProvider;
let usageProvider: UsageProvider;
let output: vscode.LogOutputChannel;
let pollTimer: NodeJS.Timeout | undefined;

const config = () => vscode.workspace.getConfiguration('aiqa');
const projectId = () => config().get<string>('projectId', '');

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  output = vscode.window.createOutputChannel('AI QA Engineer', { log: true });
  api = new ApiClient(context);

  // -- sidebar ------------------------------------------------------- //
  approvalsProvider = new ApprovalsProvider(api);
  runsProvider = new RunsProvider(api, projectId);
  agentsProvider = new AgentsProvider(api);
  usageProvider = new UsageProvider(api);

  context.subscriptions.push(
    vscode.window.registerTreeDataProvider('aiqa.approvals', approvalsProvider),
    vscode.window.registerTreeDataProvider('aiqa.runs', runsProvider),
    vscode.window.registerTreeDataProvider('aiqa.agents', agentsProvider),
    vscode.window.registerTreeDataProvider('aiqa.usage', usageProvider),
    output,
  );

  // -- status bar ---------------------------------------------------- //
  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusBar.command = 'aiqa.openChat';
  statusBar.text = '$(beaker) AI QA';
  statusBar.tooltip = 'AI QA Engineer — click to open chat';
  statusBar.show();
  context.subscriptions.push(statusBar);

  // -- commands ------------------------------------------------------ //
  const register = (id: string, handler: (...args: any[]) => unknown) =>
    context.subscriptions.push(vscode.commands.registerCommand(id, wrap(id, handler)));

  register('aiqa.openChat', () => openChat(context));
  register('aiqa.automate', () => promptAndRun(context, config().get<RunMode>('defaultMode', 'full')));
  register('aiqa.planOnly', () => promptAndRun(context, 'plan_only'));
  register('aiqa.generateOnly', () => promptAndRun(context, 'generate'));
  register('aiqa.runTests', () => quickRun(context, 'execute_only', 'Run the existing test suite'));
  register('aiqa.healFailures', () => quickRun(context, 'heal_only', 'Diagnose and repair failing tests'));
  register('aiqa.automateFromJira', () => automateFromJira(context));
  register('aiqa.registerProject', () => registerProject());
  register('aiqa.indexRepository', () => indexRepository());
  register('aiqa.lintStandards', () => lintStandards());
  register('aiqa.showApprovals', () => showApprovals());
  register('aiqa.reviewDiff', (item?: ApprovalItem) => reviewDiff(item));
  register('aiqa.approve', (item?: ApprovalItem) => respond(item, true));
  register('aiqa.reject', (item?: ApprovalItem) => respond(item, false));
  register('aiqa.openRun', (runId?: string) => openRun(context, runId));
  register('aiqa.cancelRun', (item?: { run?: { id: string } }) => cancelRun(item));
  register('aiqa.showReport', (runId?: string) => showReport(runId));
  register('aiqa.showTrace', (item?: { run?: { id: string } } | string) => showTrace(item));
  register('aiqa.refresh', () => refreshAll());
  register('aiqa.openDashboard', () => vscode.env.openExternal(vscode.Uri.parse(api.baseUrl)));
  register('aiqa.doctor', () => doctor());

  // Re-read trees when the binding changes.
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration('aiqa.projectId') || event.affectsConfiguration('aiqa.serverUrl')) {
        refreshAll();
      }
    }),
  );

  // Poll for approvals raised by other clients (CLI, CI, a teammate).
  pollTimer = setInterval(() => {
    approvalsProvider.refresh();
    void updateStatusBar();
  }, 15000);
  context.subscriptions.push({ dispose: () => pollTimer && clearInterval(pollTimer) });

  await connectBanner();
  refreshAll();
  output.info(`AI QA Engineer activated against ${api.baseUrl}`);
}

export function deactivate(): void {
  if (pollTimer) {
    clearInterval(pollTimer);
  }
}

// =========================================================================== //
// Command implementations
// =========================================================================== //
function wrap(id: string, handler: (...args: any[]) => unknown) {
  return async (...args: any[]) => {
    try {
      return await handler(...args);
    } catch (err) {
      const message = err instanceof ApiError ? err.message : String((err as Error)?.message ?? err);
      output.error(`${id}: ${message}`);
      const action = err instanceof ApiError && err.status === 0 ? 'How do I start it?' : undefined;
      const picked = await vscode.window.showErrorMessage(`AI QA: ${message}`, ...(action ? [action] : []));
      if (picked) {
        void vscode.window.showInformationMessage(
          'Start the control plane from the repository root: `aiqa serve` (or `python -m services.api_gateway.cli serve`).',
        );
      }
      return undefined;
    }
  };
}

async function openChat(context: vscode.ExtensionContext): Promise<ChatPanel> {
  return ChatPanel.show(context, api, refreshAll);
}

async function promptAndRun(context: vscode.ExtensionContext, mode: RunMode): Promise<void> {
  const instruction = await vscode.window.showInputBox({
    title: `AI QA — ${modeLabel(mode)}`,
    prompt: 'What should be automated?',
    placeHolder: 'Automate the Resident Registration functionality',
    ignoreFocusOut: true,
    validateInput: (value) => (value.trim().length < 8 ? 'Describe the feature in a little more detail' : undefined),
  });
  if (!instruction) {
    return;
  }
  const panel = await openChat(context);
  await panel.startRun(instruction.trim(), mode);
}

async function quickRun(context: vscode.ExtensionContext, mode: RunMode, instruction: string): Promise<void> {
  const panel = await openChat(context);
  await panel.startRun(instruction, mode);
}

async function automateFromJira(context: vscode.ExtensionContext): Promise<void> {
  const issue = await vscode.window.showInputBox({
    title: 'AI QA — automate from Jira',
    prompt: 'Jira issue key',
    placeHolder: 'PROJ-1234',
    ignoreFocusOut: true,
    validateInput: (value) =>
      /^[A-Za-z][A-Za-z0-9_]{1,9}-\d+$/.test(value.trim()) ? undefined : 'Expected a key like PROJ-1234',
  });
  if (!issue) {
    return;
  }
  const panel = await openChat(context);
  await panel.startRun(`Automate ${issue.trim().toUpperCase()}`, config().get<RunMode>('defaultMode', 'full'), {
    jira_issue: issue.trim().toUpperCase(),
  });
}

async function registerProject(): Promise<string | undefined> {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    void vscode.window.showWarningMessage('AI QA: open a QA automation repository first.');
    return undefined;
  }

  // Offer an existing registration for this path before creating a duplicate.
  const existing = await api.listProjects();
  const match = existing.find(
    (p) => path.resolve(p.repository_path).toLowerCase() === path.resolve(folder.uri.fsPath).toLowerCase(),
  );
  if (match) {
    await config().update('projectId', match.id, vscode.ConfigurationTarget.Workspace);
    void vscode.window.showInformationMessage(`AI QA: bound this workspace to the existing project "${match.name}".`);
    refreshAll();
    return match.id;
  }

  const name = await vscode.window.showInputBox({
    title: 'AI QA — register project',
    prompt: 'Project name',
    value: folder.name,
    ignoreFocusOut: true,
  });
  if (!name) {
    return undefined;
  }
  const baseUrl = await vscode.window.showInputBox({
    title: 'AI QA — application under test',
    prompt: 'URL of the running application (enables live locator discovery). Leave blank to skip.',
    placeHolder: 'http://localhost:3000',
    ignoreFocusOut: true,
  });
  const apiBaseUrl = await vscode.window.showInputBox({
    title: 'AI QA — API base URL (optional)',
    prompt: 'Base URL of the application API, for API-level checks.',
    placeHolder: 'http://localhost:3000/api',
    ignoreFocusOut: true,
  });

  const project = await api.createProject({
    name: name.trim(),
    repository_path: folder.uri.fsPath,
    base_url: (baseUrl ?? '').trim() || null,
    api_base_url: (apiBaseUrl ?? '').trim() || null,
  });
  await config().update('projectId', project.id, vscode.ConfigurationTarget.Workspace);

  const indexNow = await vscode.window.showInformationMessage(
    `AI QA: registered "${project.name}". Index the repository now so generated tests match your conventions?`,
    'Index now',
    'Later',
  );
  if (indexNow === 'Index now') {
    await indexRepository();
  }
  refreshAll();
  return project.id;
}

async function indexRepository(): Promise<void> {
  const id = projectId() || (await registerProject());
  if (!id) {
    return;
  }
  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: 'AI QA: indexing repository…', cancellable: false },
    async () => {
      const result = await api.indexProject(id);
      output.info(`indexed ${result.files} files, ${result.symbols} symbols, ${result.chunks} chunks`);
      const detail = [
        `${result.files} files · ${result.symbols} symbols · ${result.chunks} chunks`,
        `runner: ${result.test_runner}${result.bdd ? ' + BDD' : ''}`,
        result.page_objects?.length ? `page objects: ${result.page_objects.slice(0, 6).join(', ')}` : '',
      ]
        .filter(Boolean)
        .join(' — ');
      const picked = await vscode.window.showInformationMessage(`AI QA: ${detail}`, 'Show conventions');
      if (picked) {
        const document = await vscode.workspace.openTextDocument({
          content: String(result.conventions_summary ?? ''),
          language: 'markdown',
        });
        await vscode.window.showTextDocument(document, { preview: true });
      }
    },
  );
  refreshAll();
}

async function lintStandards(): Promise<void> {
  const id = projectId();
  if (!id) {
    void vscode.window.showWarningMessage('AI QA: register this workspace as a project first.');
    return;
  }
  const collection = vscode.languages.createDiagnosticCollection('aiqa-standards');
  const report = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: 'AI QA: checking QA standards…' },
    () => api.lintProject(id),
  );

  const folder = vscode.workspace.workspaceFolders?.[0];
  const byFile = new Map<string, vscode.Diagnostic[]>();
  for (const violation of (report.violations ?? []) as any[]) {
    const line = Math.max(0, Number(violation.line ?? 1) - 1);
    const diagnostic = new vscode.Diagnostic(
      new vscode.Range(line, 0, line, 500),
      `[${violation.rule_id}] ${violation.message}`,
      violation.severity === 'error' || violation.severity === 'critical'
        ? vscode.DiagnosticSeverity.Error
        : vscode.DiagnosticSeverity.Warning,
    );
    diagnostic.source = 'AI QA standards';
    diagnostic.code = violation.rule_id;
    const list = byFile.get(violation.file_path) ?? [];
    list.push(diagnostic);
    byFile.set(violation.file_path, list);
  }
  if (folder) {
    for (const [file, diagnostics] of byFile) {
      collection.set(vscode.Uri.joinPath(folder.uri, file), diagnostics);
    }
  }

  const message =
    `AI QA standards: ${report.errors} error(s), ${report.warnings} warning(s) ` +
    `across ${report.files_checked} file(s) against ${report.rules_applied} rule(s).`;
  if (report.errors > 0) {
    void vscode.window.showWarningMessage(message, 'Open Problems').then((picked) => {
      if (picked) {
        void vscode.commands.executeCommand('workbench.actions.view.problems');
      }
    });
  } else {
    void vscode.window.showInformationMessage(message);
  }
}

async function showApprovals(): Promise<void> {
  const approvals = await api.listApprovals();
  if (approvals.length === 0) {
    void vscode.window.showInformationMessage('AI QA: nothing is waiting for review.');
    return;
  }
  const picked = await vscode.window.showQuickPick(
    approvals.map((a) => ({
      label: `$(${a.risk === 'high' || a.risk === 'critical' ? 'flame' : 'warning'}) ${a.title}`,
      description: `${a.kind} · risk ${a.risk}`,
      detail: a.description.slice(0, 200),
      approval: a,
    })),
    { title: 'Pending approvals', placeHolder: 'Select one to review' },
  );
  if (picked) {
    await reviewApproval(picked.approval);
  }
}

async function reviewDiff(item?: ApprovalItem): Promise<void> {
  const approval = item?.approval ?? approvalsProvider.pending[0];
  if (!approval) {
    void vscode.window.showInformationMessage('AI QA: nothing is waiting for review.');
    return;
  }
  await reviewApproval(approval);
}

/** Show the change set, then ask for the decision. Diff first, always. */
async function reviewApproval(approval: Approval): Promise<void> {
  await showDiffDocument(approval);

  const choice = await vscode.window.showInformationMessage(
    approval.title,
    { modal: true, detail: approval.description.slice(0, 900) },
    'Approve',
    'Reject',
  );
  if (!choice) {
    return;
  }
  if (choice === 'Approve') {
    await api.respondApproval(approval.id, true, 'approved in VS Code');
    void vscode.window.showInformationMessage(`AI QA: approved — the run is continuing.`);
  } else {
    const comment = await vscode.window.showInputBox({
      title: 'Reason for rejection',
      prompt: 'This is recorded in the audit log and shown to the agent.',
      ignoreFocusOut: true,
    });
    await api.respondApproval(approval.id, false, comment ?? '');
    void vscode.window.showInformationMessage('AI QA: rejected — the run was stopped.');
  }
  refreshAll();
}

async function respond(item: ApprovalItem | undefined, approved: boolean): Promise<void> {
  const approval = item?.approval ?? approvalsProvider.pending[0];
  if (!approval) {
    return;
  }
  let comment = '';
  if (!approved) {
    comment =
      (await vscode.window.showInputBox({ title: 'Reason for rejection', ignoreFocusOut: true })) ?? '';
  }
  await api.respondApproval(approval.id, approved, comment);
  void vscode.window.showInformationMessage(`AI QA: ${approved ? 'approved' : 'rejected'} ${approval.kind}.`);
  refreshAll();
}

async function openRun(context: vscode.ExtensionContext, runId?: string): Promise<void> {
  let id = runId;
  if (!id) {
    const runs = await api.listRuns(projectId() || undefined, 30);
    const picked = await vscode.window.showQuickPick(
      runs.map((r) => ({ label: r.instruction.slice(0, 70), description: `${r.status} · $${r.total_cost_usd.toFixed(4)}`, id: r.id })),
      { title: 'Open run' },
    );
    id = picked?.id;
  }
  if (!id) {
    return;
  }
  const panel = await openChat(context);
  await panel.attach(id);
}

async function cancelRun(item?: { run?: { id: string } }): Promise<void> {
  const id = item?.run?.id;
  if (!id) {
    return;
  }
  const confirmed = await vscode.window.showWarningMessage(
    'Cancel this run? Work already written to the workspace is left in place.',
    { modal: true },
    'Cancel run',
  );
  if (confirmed) {
    await api.cancelRun(id);
    refreshAll();
  }
}

async function showReport(runId?: string): Promise<void> {
  let id = runId;
  if (!id) {
    const runs = await api.listRuns(projectId() || undefined, 20);
    const withReports = runs.filter((r) => r.status === 'succeeded' || r.status === 'failed');
    const picked = await vscode.window.showQuickPick(
      withReports.map((r) => ({ label: r.instruction.slice(0, 70), description: r.status, id: r.id })),
      { title: 'Show report for run' },
    );
    id = picked?.id;
  }
  if (!id) {
    return;
  }
  const markdown = await api.runReport(id, 'markdown');
  const document = await vscode.workspace.openTextDocument({ content: markdown, language: 'markdown' });
  await vscode.window.showTextDocument(document, { preview: false });
  await vscode.commands.executeCommand('markdown.showPreviewToSide');
}

async function showTrace(item?: { run?: { id: string } } | string): Promise<void> {
  const id = typeof item === 'string' ? item : item?.run?.id;
  if (!id) {
    return;
  }
  const [detail, trace] = await Promise.all([api.getRun(id), api.runTrace(id)]);

  const lines: string[] = [
    `# Agent trace — ${id}`,
    '',
    `**${detail.instruction}**`,
    '',
    `status \`${detail.status}\` · mode \`${detail.mode}\` · path: ${detail.visited.join(' → ')}`,
    '',
    '## Agents',
    '',
    '| # | agent | status | model | tokens | cost | ms | output |',
    '| --- | --- | --- | --- | --- | --- | --- | --- |',
    ...detail.traces.map(
      (t) =>
        `| ${t.sequence} | ${t.agent} | ${t.status} | ${t.model || '—'} | ${t.total_tokens.toLocaleString()} | ` +
        `$${t.cost_usd.toFixed(6)} | ${t.latency_ms} | ${(t.output_summary || t.error || '').slice(0, 60)} |`,
    ),
    '',
    '## LLM calls',
    '',
    '| agent | provider/model | prompt | completion | cost | ms | status |',
    '| --- | --- | --- | --- | --- | --- | --- |',
    ...((trace.llm_calls ?? []) as any[]).map(
      (c) =>
        `| ${c.agent} | ${c.provider}/${c.model} | ${c.prompt_tokens} | ${c.completion_tokens} | ` +
        `$${Number(c.cost_usd).toFixed(6)} | ${c.latency_ms} | ${c.status}${c.fallback_from ? ` (fell back from ${c.fallback_from})` : ''} |`,
    ),
    '',
    '## Tool calls',
    '',
    '| agent | tool | status | ms | arguments |',
    '| --- | --- | --- | --- | --- |',
    ...((trace.tool_calls ?? []) as any[]).map(
      (t) => `| ${t.agent} | \`${t.tool}\` | ${t.status} | ${t.latency_ms} | ${String(t.arguments ?? '').slice(0, 70)} |`,
    ),
    '',
    '## Audit',
    '',
    ...((trace.audit ?? []) as any[]).map(
      (a) => `- \`${a.action}\` **${a.outcome}** ${a.resource} ${a.detail ? `— ${a.detail.slice(0, 100)}` : ''}`,
    ),
  ];
  if (detail.warnings.length) {
    lines.push('', '## Warnings', '', ...detail.warnings.map((w) => `- ${w}`));
  }

  const document = await vscode.workspace.openTextDocument({ content: lines.join('\n'), language: 'markdown' });
  await vscode.window.showTextDocument(document, { preview: false });
}

async function doctor(): Promise<void> {
  try {
    const health = await api.health();
    const offline = Object.values(health.active_routes).every(
      (route) => !route || route.provider === 'mock' || route.provider === 'hashing',
    );
    const routes = Object.entries(health.active_routes)
      .map(([capability, route]) => `  ${capability}: ${route ? `${route.provider}/${route.model}` : 'none'}`)
      .join('\n');

    const document = await vscode.workspace.openTextDocument({
      content: [
        '# AI QA Engineer — connection check',
        '',
        `Control plane: ${api.baseUrl}`,
        `Status: ${health.status} (v${health.version}, env ${health.env}, ${health.database})`,
        `Configured providers: ${health.configured_providers.join(', ')}`,
        '',
        '## Model routing',
        routes,
        '',
        offline
          ? '> No real LLM is reachable. The platform will run in deterministic offline mode: it still\n' +
            '> produces a complete plan, Gherkin and page objects, but without model reasoning.\n' +
            '> Start Ollama (`ollama serve && ollama pull qwen2.5-coder:7b`) or set `OPENROUTER_API_KEY`.'
          : '> Model routing is live.',
        '',
        '## Spend',
        `Today: $${health.cost.spent_today_usd ?? 0} of $${health.cost.daily_limit_usd ?? 0}`,
        `Month: $${health.cost.spent_month_usd ?? 0} of $${health.cost.monthly_limit_usd ?? 0}`,
        '',
        `Workspace project id: ${projectId() || '(not bound — run "AI QA: Register This Workspace as a Project")'}`,
      ].join('\n'),
      language: 'markdown',
    });
    await vscode.window.showTextDocument(document, { preview: true });
  } catch (err) {
    void vscode.window.showErrorMessage(
      `AI QA: cannot reach ${api.baseUrl}. Start the control plane with \`aiqa serve\`. (${String((err as Error).message)})`,
    );
  }
}

// =========================================================================== //
async function connectBanner(): Promise<void> {
  try {
    const health = await api.health();
    const offline = Object.values(health.active_routes).every(
      (route) => !route || route.provider === 'mock' || route.provider === 'hashing',
    );
    if (offline) {
      output.warn('no LLM provider reachable — running in deterministic offline mode');
    }
    statusBar.text = `$(beaker) AI QA${offline ? ' (offline)' : ''}`;
  } catch {
    statusBar.text = '$(beaker) AI QA $(debug-disconnect)';
    statusBar.tooltip = `Cannot reach ${api.baseUrl} — run \`aiqa serve\``;
  }
}

async function updateStatusBar(): Promise<void> {
  if (!config().get<boolean>('showCostInStatusBar', true)) {
    statusBar.text = '$(beaker) AI QA';
    return;
  }
  try {
    const runs = await api.listRuns(projectId() || undefined, 5);
    const active = runs.find((r) => r.status === 'running' || r.status === 'waiting_approval');
    if (active) {
      const icon = active.status === 'running' ? '$(sync~spin)' : '$(person)';
      statusBar.text = `${icon} AI QA ${active.current_agent || active.status} · $${active.total_cost_usd.toFixed(4)}`;
      statusBar.tooltip = `${active.instruction}\n${active.total_tokens.toLocaleString()} tokens`;
      return;
    }
    const metrics = await api.metrics(1);
    const today = metrics.cost?.governance?.spent_today_usd ?? 0;
    statusBar.text = `$(beaker) AI QA · $${Number(today).toFixed(4)} today`;
    statusBar.tooltip = 'AI QA Engineer — click to open chat';
  } catch {
    statusBar.text = '$(beaker) AI QA $(debug-disconnect)';
  }
}

function refreshAll(): void {
  approvalsProvider.refresh();
  runsProvider.refresh();
  agentsProvider.refresh();
  usageProvider.refresh();
  void updateStatusBar();
}

function modeLabel(mode: RunMode): string {
  return {
    plan_only: 'design a test plan',
    generate: 'generate tests',
    full: 'automate a feature',
    autonomous: 'automate autonomously',
    execute_only: 'run tests',
    heal_only: 'heal failures',
  }[mode];
}
