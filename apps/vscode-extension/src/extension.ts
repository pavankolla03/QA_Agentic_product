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
import { showDiffDocument } from './panels/chatPanel';
import { ChatViewProvider } from './panels/chatView';
import { Onboarding } from './panels/onboarding';
import { ServerManager } from './server/serverManager';
import { AgentsProvider, ApprovalItem, ApprovalsProvider, RunsProvider } from './views/trees';
import {
  ActivityProvider,
  AssistantProvider,
  ConfigurationProvider,
  CostsProvider,
  FailuresProvider,
  HealingProvider,
  KnowledgeProvider,
  ReportsProvider,
  StandardsProvider,
} from './views/panels';

let api: ApiClient;
let statusBar: vscode.StatusBarItem;
let approvalsProvider: ApprovalsProvider;
let runsProvider: RunsProvider;
let agentsProvider: AgentsProvider;
let panels: { refresh(): void }[] = [];
let output: vscode.LogOutputChannel;
let pollTimer: NodeJS.Timeout | undefined;
let server: ServerManager;
let chat: ChatViewProvider;
let onboarding: Onboarding;

const config = () => vscode.workspace.getConfiguration('aiqa');
const projectId = () => config().get<string>('projectId', '');

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  output = vscode.window.createOutputChannel('AI QA Engineer', { log: true });
  api = new ApiClient(context);

  // The extension is useless without a control plane, so bring one up before
  // anything else tries to call it. An already-running server is left alone.
  server = new ServerManager(output, () => api.baseUrl);
  context.subscriptions.push(server);

  // -- sidebar ------------------------------------------------------- //
  // The chat is the primary surface, so it is a docked view rather than a
  // command-summoned editor tab: clicking the extension icon should land on
  // somewhere to type.
  onboarding = new Onboarding(context, api, () => ensureServer());
  chat = new ChatViewProvider(context, api, () => refreshAll());
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(ChatViewProvider.viewType, chat, {
      // A run takes minutes on free models and the engineer will look at other
      // views meanwhile; rebuilding the webview would drop the transcript.
      webviewOptions: { retainContextWhenHidden: true },
    }),
  );

  approvalsProvider = new ApprovalsProvider(api);
  runsProvider = new RunsProvider(api, projectId);
  agentsProvider = new AgentsProvider(api);

  const assistant = new AssistantProvider(api);
  const failures = new FailuresProvider(api);
  const healing = new HealingProvider(api);
  const activity = new ActivityProvider(api);
  const knowledge = new KnowledgeProvider(api);
  const standards = new StandardsProvider(api);
  const costs = new CostsProvider(api);
  const reports = new ReportsProvider(api);
  const configuration = new ConfigurationProvider(api);
  panels = [assistant, failures, healing, activity, knowledge, standards, costs, reports, configuration];

  context.subscriptions.push(
    vscode.window.registerTreeDataProvider('aiqa.assistant', assistant),
    vscode.window.registerTreeDataProvider('aiqa.approvals', approvalsProvider),
    vscode.window.registerTreeDataProvider('aiqa.runs', runsProvider),
    vscode.window.registerTreeDataProvider('aiqa.failures', failures),
    vscode.window.registerTreeDataProvider('aiqa.healing', healing),
    vscode.window.registerTreeDataProvider('aiqa.activity', activity),
    vscode.window.registerTreeDataProvider('aiqa.knowledge', knowledge),
    vscode.window.registerTreeDataProvider('aiqa.standards', standards),
    vscode.window.registerTreeDataProvider('aiqa.costs', costs),
    vscode.window.registerTreeDataProvider('aiqa.agents', agentsProvider),
    vscode.window.registerTreeDataProvider('aiqa.reports', reports),
    vscode.window.registerTreeDataProvider('aiqa.configuration', configuration),
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
  register('aiqa.resetApiKey', () => resetApiKey());
  register('aiqa.setup', () => onboarding.run());
  register('aiqa.startServer', () => startServer());
  register('aiqa.stopServer', () => stopServer());
  register('aiqa.restartServer', () => restartServer());
  register('aiqa.showServerLog', () => output.show(true));

  // ---- v2 commands ------------------------------------------------- //
  register('aiqa.generateAutomation', () => promptAndRun(context, config().get<RunMode>('defaultMode', 'full')));
  register('aiqa.exploreApplication', () => exploreApplication(context));
  register('aiqa.generateGherkin', () => promptAndRun(context, 'plan_only'));
  register('aiqa.generatePageObject', () => generateArtifact(context, 'Page Object'));
  register('aiqa.generateSteps', () => generateArtifact(context, 'step definitions'));
  register('aiqa.analyzeFailure', () => analyzeFailure());
  register('aiqa.selfHeal', () => quickRun(context, 'heal_only', 'Diagnose and repair failing tests'));
  register('aiqa.validateStandards', () => lintStandards());
  register('aiqa.showCost', () => showCost());
  register('aiqa.generateReport', () => showReport());
  register('aiqa.showKnowledge', () => showKnowledge());
  register('aiqa.initStandards', () => initStandards());
  register('aiqa.showPipeline', () => showPipeline());
  register('aiqa.showCoverage', () => showCoverage());
  register('aiqa.showSuiteHealth', () => showSuiteHealth());
  register('aiqa.exploratoryTest', () => exploratoryTest(context));
  register('aiqa.batchFromEpic', () => batchFromEpic());
  register('aiqa.automateInstruction', (instruction?: string) =>
    runInstruction(context, instruction),
  );

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

  await ensureServer();
  await connectBanner();
  refreshAll();
  // An offer, not a takeover — and only while something is actually unset.
  void onboarding.offerOnce();
  output.info(`AI QA Engineer activated against ${api.baseUrl}`);
}

export function deactivate(): void {
  if (pollTimer) {
    clearInterval(pollTimer);
  }
  // Only ever stops a server this extension started.
  server?.dispose();
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
      // A status of 0 means the control plane is unreachable. That is fixable
      // from here, so offer the fix rather than instructions for it.
      const unreachable = err instanceof ApiError && err.status === 0;
      const picked = await vscode.window.showErrorMessage(
        `AI QA: ${message}`,
        ...(unreachable ? ['Start the control plane', 'Show log'] : []),
      );
      if (picked === 'Start the control plane') {
        await startServer();
      } else if (picked === 'Show log') {
        output.show(true);
      }
      return undefined;
    }
  };
}

async function openChat(_context: vscode.ExtensionContext): Promise<ChatViewProvider> {
  await chat.reveal();
  return chat;
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

async function exploreApplication(context: vscode.ExtensionContext): Promise<void> {
  const panel = await openChat(context);
  await panel.startRun('Explore the application and refresh the application map', 'plan_only', {
    max_explore_pages: 10,
  });
}

async function generateArtifact(context: vscode.ExtensionContext, artifact: string): Promise<void> {
  const feature = await vscode.window.showInputBox({
    title: `AI QA - generate ${artifact}`,
    prompt: `Which feature or page should the ${artifact} cover?`,
    ignoreFocusOut: true,
  });
  if (!feature) {
    return;
  }
  const panel = await openChat(context);
  await panel.startRun(`Generate ${artifact} for ${feature.trim()}`, 'generate');
}

async function analyzeFailure(): Promise<void> {
  const runs = await api.listRuns(projectId() || undefined, 20);
  const failing = runs.filter((r) => r.tests_failed > 0);
  if (!failing.length) {
    void vscode.window.showInformationMessage('AI QA: no failing runs to analyse.');
    return;
  }
  const picked = await vscode.window.showQuickPick(
    failing.map((r) => ({
      label: r.instruction.slice(0, 70),
      description: `${r.tests_failed} failing`,
      id: r.id,
    })),
    { title: 'Analyse failures from which run?' },
  );
  if (picked) {
    await showTrace(picked.id);
  }
}

async function openMarkdown(lines: string[]): Promise<void> {
  const document = await vscode.workspace.openTextDocument({
    content: lines.join('\n'),
    language: 'markdown',
  });
  await vscode.window.showTextDocument(document, { preview: false });
  await vscode.commands.executeCommand('markdown.showPreviewToSide');
}

async function showCost(): Promise<void> {
  const [cost, savings, management] = await Promise.all([
    api.costMetrics(30),
    api.savingsMetrics(30),
    api.managementMetrics(30),
  ]);
  const totals = cost.totals ?? {};
  const unit = cost.unit_economics ?? {};

  await openMarkdown([
    '# AI QA - cost report (30 days)',
    '',
    '## Totals',
    '',
    '| | |',
    '| --- | --- |',
    `| Runs | ${totals.runs ?? 0} |`,
    `| Scenarios generated | ${totals.scenarios ?? 0} |`,
    `| LLM requests | ${totals.llm_requests ?? 0} |`,
    `| Input tokens | ${Number(totals.input_tokens ?? 0).toLocaleString()} |`,
    `| Output tokens | ${Number(totals.output_tokens ?? 0).toLocaleString()} |`,
    `| Free-model share | ${totals.free_call_share_pct ?? 0}% |`,
    `| **Total cost** | **$${Number(totals.total_cost_usd ?? 0).toFixed(4)}** |`,
    '',
    '## Unit economics',
    '',
    `- Cost per scenario: **$${Number(unit.cost_per_scenario_usd ?? 0).toFixed(5)}**`,
    `- Cost per successful automation: $${Number(unit.cost_per_successful_automation_usd ?? 0).toFixed(5)}`,
    `- Requests per scenario: ${unit.requests_per_scenario ?? 0}`,
    `- Tokens per scenario: ${Number(unit.tokens_per_scenario ?? 0).toLocaleString()}`,
    '',
    '## What the knowledge layer avoided',
    '',
    `- Repository index cache hit rate: ${savings.repository_cache_hit_rate_pct ?? 0}%`,
    `- Application map hit rate: ${savings.application_map_hit_rate_pct ?? 0}%`,
    `- Duplicate scenarios avoided: ${savings.duplicate_scenarios_avoided ?? 0}`,
    `- Context tokens never sent: ${Number(savings.context_tokens_avoided ?? 0).toLocaleString()}`,
    '',
    `_${savings.interpretation ?? ''}_`,
    '',
    '## By model',
    '',
    '| Model | Calls | Tokens | Cost |',
    '| --- | --- | --- | --- |',
    ...Object.entries(cost.by_model ?? {}).map(
      ([model, s]: [string, any]) =>
        `| ${model} | ${s.calls} | ${Number(s.tokens).toLocaleString()} | $${Number(s.cost_usd).toFixed(6)} |`,
    ),
    '',
    '## By agent',
    '',
    '| Agent | Calls | Tokens | Cost |',
    '| --- | --- | --- | --- |',
    ...Object.entries(cost.by_agent ?? {}).map(
      ([agent, s]: [string, any]) =>
        `| ${agent} | ${s.calls} | ${Number(s.tokens).toLocaleString()} | $${Number(s.cost_usd).toFixed(6)} |`,
    ),
    '',
    '## Impact estimate',
    '',
    `- Hours saved (estimate): ${management.estimated_impact?.hours_saved_estimate ?? 0}`,
    `- _${management.estimated_impact?.assumption ?? ''}_`,
    '',
    `> ${cost.baseline_comparison?.note ?? ''}`,
  ]);
}

/** Start a run for an instruction the UI already knows (e.g. a coverage gap). */
async function runInstruction(
  context: vscode.ExtensionContext,
  instruction?: string,
): Promise<void> {
  const mode = config().get<RunMode>('defaultMode', 'full');
  if (!instruction || !instruction.trim()) {
    return promptAndRun(context, mode);
  }
  // Confirm rather than launch silently: the click came from a tree row, and a
  // run costs money.
  const picked = await vscode.window.showInformationMessage(
    `Start a run for: ${instruction}?`,
    { modal: false },
    'Run it',
    'Edit first',
  );
  if (picked === 'Edit first') {
    return promptAndRun(context, mode);
  }
  if (picked !== 'Run it') {
    return;
  }
  const panel = await openChat(context);
  await panel.startRun(instruction.trim(), mode);
}

async function showCoverage(): Promise<void> {
  const id = projectId();
  if (!id) {
    void vscode.window.showWarningMessage('AI QA: register this workspace as a project first.');
    return;
  }
  const report = await api.projectCoverage(id);
  const routes = report.routes ?? {};
  const endpoints = report.endpoints ?? {};
  const requirements = report.requirements ?? {};
  const gaps = (report.gaps ?? []) as Record<string, any>[];

  const lines: string[] = [
    '# AI QA - coverage gaps',
    '',
    String(report.summary ?? ''),
    '',
    '| what | covered | total | % |',
    '| --- | ---: | ---: | ---: |',
    `| routes | ${routes.covered ?? 0} | ${routes.total ?? 0} | ${routes.pct ?? 0}% |`,
    `| endpoints | ${endpoints.covered ?? 0} | ${endpoints.total ?? 0} | ${endpoints.pct ?? 0}% |`,
    `| requirements | ${requirements.covered ?? 0} | ${requirements.total ?? 0} | ${requirements.pct ?? 0}% |`,
    '',
  ];
  if (gaps.length) {
    lines.push('## Gaps, worst first', '', '| severity | kind | what | run this to close it |', '| --- | --- | --- | --- |');
    for (const gap of gaps) {
      lines.push(`| ${gap.severity} | ${gap.kind} | \`${gap.label}\` | ${gap.suggested_instruction} |`);
    }
  } else {
    lines.push('No gaps found.');
  }
  lines.push(
    '',
    '> A route counted as covered has a test touching it. That is not the same as',
    '> being well tested, and this report does not claim otherwise.',
  );

  const document = await vscode.workspace.openTextDocument({
    content: lines.join('\n'),
    language: 'markdown',
  });
  await vscode.window.showTextDocument(document, { preview: true });
}

async function showSuiteHealth(): Promise<void> {
  const id = projectId();
  if (!id) {
    void vscode.window.showWarningMessage('AI QA: register this workspace as a project first.');
    return;
  }
  const report = await api.suiteHealth(id);
  const tests = (report.tests ?? []) as Record<string, any>[];
  if (!tests.length) {
    void vscode.window.showInformationMessage(
      'AI QA: no test has been executed yet, so there is nothing to judge.',
    );
    return;
  }

  const lines: string[] = [
    `# AI QA - suite health (${report.health_score}%)`,
    '',
    String(report.summary ?? ''),
    '',
    '| verdict | test | runs | fail | flake | what to do |',
    '| --- | --- | ---: | ---: | ---: | --- |',
    ...tests.map(
      (t) =>
        `| ${t.verdict} | ${t.test_name || t.test_id} | ${t.runs} | ${t.failures} | ${t.flakes} | ${t.recommended_action} |`,
    ),
    '',
    '> A test that never passes is NOT flaky. It is reporting something, and it is',
    '> never quarantined automatically.',
  ];

  const document = await vscode.workspace.openTextDocument({
    content: lines.join('\n'),
    language: 'markdown',
  });
  await vscode.window.showTextDocument(document, { preview: true });

  const candidates = (report.quarantine_candidates ?? []) as Record<string, any>[];
  if (!candidates.length) {
    return;
  }
  const picked = await vscode.window.showInformationMessage(
    `Quarantine ${candidates.length} intermittent test(s)? Consistently failing tests are left alone.`,
    'Quarantine them',
    'Not now',
  );
  if (picked === 'Quarantine them') {
    const result = await api.quarantine(id, { apply: true });
    void vscode.window.showInformationMessage(`AI QA: ${result.summary}`);
    refreshAll();
  }
}

async function exploratoryTest(context: vscode.ExtensionContext): Promise<void> {
  const id = projectId();
  if (!id) {
    void vscode.window.showWarningMessage('AI QA: register this workspace as a project first.');
    return;
  }
  const report = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: 'AI QA: probing the application' },
    () => api.exploratory(id),
  );
  const findings = (report.findings ?? []) as Record<string, any>[];

  const lines: string[] = [
    '# AI QA - exploratory pass',
    '',
    String(report.summary ?? ''),
    '',
  ];
  if (findings.length) {
    lines.push('| severity | kind | where | what | confidence |', '| --- | --- | --- | --- | --- |');
    for (const f of findings) {
      lines.push(`| ${f.severity} | ${f.kind} | \`${f.route}\` | ${f.title} | ${f.confidence} |`);
    }
    lines.push('', '## Evidence', '');
    for (const f of findings) {
      lines.push(`### ${f.title}`, '', '```', String(f.evidence ?? ''), '```', '');
      const steps = (f.reproduction ?? []) as string[];
      if (steps.length) {
        lines.push('Reproduce:', ...steps.map((s) => `1. ${s}`), '');
      }
    }
  } else {
    lines.push('Nothing self-evidently broken was found.');
  }
  lines.push(
    '',
    '> Findings are limited to failures that need no specification to recognise:',
    '> crashes, error pages, dead links, absent validation. A clean pass is not a',
    '> claim that the application is correct.',
  );

  const document = await vscode.workspace.openTextDocument({
    content: lines.join('\n'),
    language: 'markdown',
  });
  await vscode.window.showTextDocument(document, { preview: true });

  const next = (report.next_steps ?? []) as Record<string, any>[];
  if (!next.length) {
    return;
  }
  const choice = await vscode.window.showQuickPick(
    next.map((n) => ({ label: String(n.finding), detail: String(n.instruction) })),
    { title: 'Turn a finding into a permanent test?', placeHolder: 'Pick one, or press Escape' },
  );
  if (choice) {
    await runInstruction(context, choice.detail);
  }
}

async function batchFromEpic(): Promise<void> {
  const id = projectId();
  if (!id) {
    void vscode.window.showWarningMessage('AI QA: register this workspace as a project first.');
    return;
  }
  const epic = await vscode.window.showInputBox({
    title: 'AI QA - automate a whole epic',
    prompt: 'Jira epic key',
    placeHolder: 'QA-100',
    ignoreFocusOut: true,
    validateInput: (v) => (/^[A-Z][A-Z0-9]+-\d+$/.test(v.trim().toUpperCase()) ? undefined : 'Expected something like QA-100'),
  });
  if (!epic) {
    return;
  }
  // A batch is the most expensive thing the platform can do, so the extension
  // shows the queue and its price and then hands off to the terminal, where the
  // engineer can watch it and stop it.
  const terminal = vscode.window.createTerminal({ name: 'AI QA batch' });
  terminal.show();
  terminal.sendText(`aiqa batch ${id} --epic ${epic.trim().toUpperCase()}`);
  void vscode.window.showInformationMessage(
    'AI QA: the batch will show you the queue and its estimated cost before it starts anything.',
  );
}

async function showKnowledge(): Promise<void> {
  const id = projectId();
  if (!id) {
    void vscode.window.showWarningMessage('AI QA: register this workspace as a project first.');
    return;
  }
  const knowledge = await api.projectKnowledge(id);
  const repo = knowledge.repository_map;
  const app = knowledge.application_map;

  const lines: string[] = [
    `# AI QA - what the platform knows about ${knowledge.project}`,
    '',
    'This is the knowledge that makes later runs cheap. Nothing here is re-derived',
    'unless it has genuinely changed.',
    '',
    '## Repository map',
    '',
  ];
  if (repo) {
    lines.push(
      `- Files indexed: ${repo.files}`,
      `- Page objects: ${repo.pages} - fixtures: ${repo.fixtures} - steps: ${repo.steps}`,
      `- Indexed at commit: \`${repo.git_commit}\``,
    );
  } else {
    lines.push('_Not indexed yet - run **AI QA: Index Repository**._');
  }

  lines.push('', '## Application map', '');
  if (app) {
    lines.push(
      `- Routes known: ${app.pages}`,
      `- Locators: ${app.trusted_locators} trusted of ${app.locators} (avg confidence ${app.avg_confidence})`,
      `- Components detected: ${app.components}`,
      '',
      '### Routes',
      ...((knowledge.known_routes ?? []) as string[]).map((r) => `- \`${r}\``),
      '',
      '### Reusable components',
      ...((knowledge.components ?? []) as string[]).map((c) => `- ${c}`),
    );
  } else {
    lines.push('_Not explored yet - run **AI QA: Explore Application**._');
  }

  lines.push(
    '',
    '## Test knowledge',
    '',
    `- Tests remembered: ${knowledge.test_knowledge?.tests_known ?? 0}`,
    `- Features: ${knowledge.test_knowledge?.features ?? 0}`,
    `- Page objects referenced: ${knowledge.test_knowledge?.page_objects ?? 0}`,
    '',
    '## QA knowledge graph',
    '',
    `- Nodes: ${knowledge.knowledge_graph?.nodes ?? 0} - edges: ${knowledge.knowledge_graph?.edges ?? 0}`,
    `- Requirement coverage: ${knowledge.knowledge_graph?.coverage_pct ?? 0}%`,
  );
  await openMarkdown(lines);
}

async function initStandards(): Promise<void> {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    void vscode.window.showWarningMessage('AI QA: open a repository first.');
    return;
  }
  const confirmed = await vscode.window.showInformationMessage(
    'Create a starter .aiqa/ with config.yaml, standards/*.md and an examples/ folder?',
    {
      modal: true,
      detail:
        'Existing files are left untouched. Drop your own Page Object and feature file into .aiqa/examples/ so the platform can learn your house style.',
    },
    'Create',
  );
  if (confirmed !== 'Create') {
    return;
  }
  const terminal = vscode.window.createTerminal('AI QA');
  terminal.show();
  terminal.sendText('python -m services.api_gateway.cli standards init');
}

async function showPipeline(): Promise<void> {
  const [graph, permissions] = await Promise.all([api.pipeline(), api.agentPermissions()]);
  await openMarkdown([
    '# AI QA - agent pipeline',
    '',
    `Backend: \`${graph.backend}\` (LangGraph available: ${graph.langgraph_available})`,
    '',
    '```mermaid',
    String(graph.mermaid ?? '').trim(),
    '```',
    '',
    '## Agent permissions',
    '',
    'Least privilege, enforced at the tool boundary and audited on refusal.',
    '',
    '| Agent | Capabilities |',
    '| --- | --- |',
    ...((permissions.agents ?? []) as any[]).map(
      (a) => `| ${a.agent} | ${(a.capabilities ?? []).join(', ')} |`,
    ),
  ]);
}

/** Forget the stored key so the next action asks for a new one. */
async function resetApiKey(): Promise<void> {
  await api.clearApiKey();
  const picked = await vscode.window.showInformationMessage(
    'AI QA: the stored API key was cleared. Enter the one from `aiqa init` — not your OpenRouter key.',
    'Enter it now',
  );
  if (picked === 'Enter it now') {
    const key = await api.getApiKey();
    if (key) {
      const check = await api.verifyCredentials();
      void vscode.window.showInformationMessage(
        check.ok ? 'AI QA: the key works.' : `AI QA: still refused — ${check.detail}`,
      );
      refreshAll();
    }
  }
}

async function doctor(): Promise<void> {
  try {
    const health = await api.health();
    const offline = Object.values(health.active_routes).every(
      (route) => !route || route.provider === 'mock' || route.provider === 'hashing',
    );
    const credentials = await api.verifyCredentials();
    const routes = Object.entries(health.active_routes)
      .map(([capability, route]) => `  ${capability}: ${route ? `${route.provider}/${route.model}` : 'none'}`)
      .join('\n');

    const document = await vscode.workspace.openTextDocument({
      content: [
        '# AI QA Engineer — connection check',
        '',
        `Control plane: ${api.baseUrl}`,
        // `/api/health` is unauthenticated and answers 200 to anything, so a
        // connection check that stops there passes with a key the server will
        // refuse on the very next call.
        `Credentials: ${credentials.ok ? 'accepted' : `REJECTED — ${credentials.detail}`}`,
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
// =========================================================================== //
// Control plane lifecycle
// =========================================================================== //
async function ensureServer(): Promise<boolean> {
  const started = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Window, title: 'AI QA: connecting to the control plane' },
    () => server.ensure(),
  );
  if (started) {
    if (server.isManaged) {
      output.info('control plane started by the extension');
    }
    return true;
  }

  // Offer the two things that actually help, rather than a bare error.
  const picked = await vscode.window.showWarningMessage(
    `AI QA cannot reach the control plane at ${api.baseUrl}.${server.error ? ` ${server.error}` : ''}`,
    'Start it',
    'Show log',
  );
  if (picked === 'Start it') {
    return startServer();
  }
  if (picked === 'Show log') {
    output.show(true);
  }
  return false;
}

async function startServer(): Promise<boolean> {
  const ok = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: 'AI QA: starting the control plane' },
    () => server.start(),
  );
  if (ok) {
    void vscode.window.showInformationMessage(`AI QA control plane running at ${api.baseUrl}`);
    await connectBanner();
    refreshAll();
  } else {
    const picked = await vscode.window.showErrorMessage(
      `AI QA could not start the control plane. ${server.error}`,
      'Show log',
    );
    if (picked) {
      output.show(true);
    }
  }
  return ok;
}

function stopServer(): void {
  if (!server.isManaged) {
    void vscode.window.showInformationMessage(
      'The control plane was not started by this extension, so it was left running.',
    );
    return;
  }
  server.stop();
  void vscode.window.showInformationMessage('AI QA control plane stopped.');
  void connectBanner();
}

async function restartServer(): Promise<void> {
  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: 'AI QA: restarting the control plane' },
    () => server.restart(),
  );
  await connectBanner();
  refreshAll();
}

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
  panels.forEach((panel) => panel.refresh());
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
