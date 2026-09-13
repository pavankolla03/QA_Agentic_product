/**
 * The chat panel — the primary surface.
 *
 * A QA engineer types what to automate; the panel streams every agent step,
 * tool call, token and dollar as it happens, and renders approval cards inline
 * so review never requires leaving the editor.
 */

import * as vscode from 'vscode';
import { ApiClient, Approval, RunEvent, RunMode } from '../client/apiClient';

export class ChatPanel {
  public static current: ChatPanel | undefined;
  private static readonly viewType = 'aiqa.chat';

  private disposables: vscode.Disposable[] = [];
  private stopStream: (() => void) | undefined;
  private activeRunId: string | undefined;

  private constructor(
    private readonly panel: vscode.WebviewPanel,
    private readonly context: vscode.ExtensionContext,
    private readonly api: ApiClient,
    private readonly onChange: () => void,
  ) {
    this.panel.webview.html = this.html();
    this.panel.onDidDispose(() => this.dispose(), null, this.disposables);
    this.panel.webview.onDidReceiveMessage(
      (message) => void this.handleMessage(message),
      null,
      this.disposables,
    );
    void this.sendContext();
  }

  static show(context: vscode.ExtensionContext, api: ApiClient, onChange: () => void): ChatPanel {
    const column = vscode.window.activeTextEditor?.viewColumn ?? vscode.ViewColumn.One;
    if (ChatPanel.current) {
      ChatPanel.current.panel.reveal(column);
      return ChatPanel.current;
    }
    const panel = vscode.window.createWebviewPanel(
      ChatPanel.viewType,
      'AI QA Engineer',
      column,
      { enableScripts: true, retainContextWhenHidden: true },
    );
    ChatPanel.current = new ChatPanel(panel, context, api, onChange);
    return ChatPanel.current;
  }

  dispose(): void {
    this.stopStream?.();
    ChatPanel.current = undefined;
    this.panel.dispose();
    this.disposables.forEach((d) => d.dispose());
    this.disposables = [];
  }

  // ------------------------------------------------------------------ //
  private post(message: Record<string, unknown>): void {
    void this.panel.webview.postMessage(message);
  }

  private async sendContext(): Promise<void> {
    try {
      const [health, projects] = await Promise.all([this.api.health(), this.api.listProjects()]);
      const projectId = vscode.workspace.getConfiguration('aiqa').get<string>('projectId', '');
      const offline = Object.values(health.active_routes).every(
        (route) => !route || route.provider === 'mock' || route.provider === 'hashing',
      );
      this.post({
        type: 'context',
        connected: true,
        version: health.version,
        offline,
        routes: health.active_routes,
        cost: health.cost,
        projects,
        projectId,
        defaultMode: vscode.workspace.getConfiguration('aiqa').get<string>('defaultMode', 'full'),
      });
    } catch (err) {
      this.post({ type: 'context', connected: false, error: String((err as Error).message) });
    }
  }

  /** Start a run and stream it into the panel. */
  async startRun(instruction: string, mode: RunMode, extra: Record<string, unknown> = {}): Promise<void> {
    const config = vscode.workspace.getConfiguration('aiqa');
    let projectId = config.get<string>('projectId', '');

    if (!projectId) {
      const chosen = await vscode.commands.executeCommand<string | undefined>('aiqa.registerProject');
      projectId = chosen ?? config.get<string>('projectId', '');
      if (!projectId) {
        this.post({ type: 'error', message: 'No project is bound to this workspace.' });
        return;
      }
    }

    this.post({ type: 'runStarting', instruction, mode });
    try {
      const run = await this.api.createRun({
        project_id: projectId,
        instruction,
        mode,
        auto_approve: config.get<boolean>('autoApprove', false),
        max_cost_usd: config.get<number>('maxCostUsd', 2.0),
        start: true,
        ...extra,
      });
      this.activeRunId = run.id;
      this.post({ type: 'runCreated', run });
      await this.attach(run.id);
    } catch (err) {
      this.post({ type: 'error', message: String((err as Error).message) });
    }
  }

  /** Attach the panel to an existing run's event stream. */
  async attach(runId: string): Promise<void> {
    this.stopStream?.();
    this.activeRunId = runId;

    try {
      const detail = await this.api.getRun(runId);
      this.post({ type: 'runSnapshot', run: detail });
    } catch {
      /* the stream replay will fill in */
    }

    this.stopStream = await this.api.streamRun(
      runId,
      (event) => void this.onEvent(runId, event),
      (reason) => {
        this.post({ type: 'streamClosed', reason });
        void this.finish(runId);
      },
    );
  }

  private async onEvent(runId: string, event: RunEvent): Promise<void> {
    this.post({ type: 'event', event });

    if (event.type === 'approval_required') {
      const approvalId = String((event.data ?? {}).approval_id ?? '');
      if (approvalId) {
        await this.presentApproval(approvalId);
      }
      this.onChange();
    }
    if (event.type === 'run_finished' || event.type === 'run_failed') {
      await this.finish(runId);
      this.onChange();
    }
  }

  private async presentApproval(approvalId: string): Promise<void> {
    try {
      const approvals = await this.api.listApprovals();
      const approval = approvals.find((a) => a.id === approvalId);
      if (!approval) {
        return;
      }
      this.post({ type: 'approval', approval });
      if (vscode.workspace.getConfiguration('aiqa').get<boolean>('openDiffOnApproval', true)) {
        await showDiffDocument(approval);
      }
    } catch {
      /* non-fatal */
    }
  }

  private async finish(runId: string): Promise<void> {
    try {
      const detail = await this.api.getRun(runId);
      this.post({ type: 'runFinished', run: detail });
    } catch {
      /* ignore */
    }
  }

  // ------------------------------------------------------------------ //
  private async handleMessage(message: Record<string, any>): Promise<void> {
    switch (message.command) {
      case 'submit':
        await this.startRun(String(message.text ?? '').trim(), (message.mode ?? 'full') as RunMode);
        break;

      case 'approve':
      case 'reject': {
        const approved = message.command === 'approve';
        try {
          await this.api.respondApproval(String(message.approvalId), approved, String(message.comment ?? ''));
          this.post({ type: 'approvalResolved', approvalId: message.approvalId, approved });
          this.onChange();
        } catch (err) {
          this.post({ type: 'error', message: String((err as Error).message) });
        }
        break;
      }

      case 'viewDiff': {
        const approvals = await this.api.listApprovals();
        const approval = approvals.find((a) => a.id === message.approvalId);
        if (approval) {
          await showDiffDocument(approval);
        }
        break;
      }

      case 'openFile': {
        const folder = vscode.workspace.workspaceFolders?.[0];
        if (folder && message.path) {
          const uri = vscode.Uri.joinPath(folder.uri, String(message.path));
          try {
            await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(uri));
          } catch {
            this.post({ type: 'error', message: `Cannot open ${message.path} — it may not be written yet.` });
          }
        }
        break;
      }

      case 'showReport':
        if (this.activeRunId) {
          await vscode.commands.executeCommand('aiqa.showReport', this.activeRunId);
        }
        break;

      case 'cancel':
        if (this.activeRunId) {
          await this.api.cancelRun(this.activeRunId);
          this.onChange();
        }
        break;

      case 'setMode':
        await vscode.workspace
          .getConfiguration('aiqa')
          .update('defaultMode', message.mode, vscode.ConfigurationTarget.Workspace);
        break;

      case 'refresh':
        await this.sendContext();
        break;

      default:
        break;
    }
  }

  // ------------------------------------------------------------------ //
  private html(): string {
    const webview = this.panel.webview;
    const nonce = Math.random().toString(36).slice(2);
    const script = webview.asWebviewUri(
      vscode.Uri.joinPath(this.context.extensionUri, 'media', 'chat.js'),
    );
    const style = webview.asWebviewUri(
      vscode.Uri.joinPath(this.context.extensionUri, 'media', 'chat.css'),
    );

    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}'; font-src ${webview.cspSource};">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link href="${style}" rel="stylesheet">
<title>AI QA Engineer</title>
</head>
<body>
  <header id="header">
    <div class="brand">
      <span class="dot" id="statusDot"></span>
      <strong>AI QA Engineer</strong>
      <span class="muted" id="statusText">connecting…</span>
    </div>
    <div class="meters">
      <span class="meter" id="tokenMeter" title="Tokens used by this run">0 tok</span>
      <span class="meter" id="costMeter" title="Cost of this run">$0.0000</span>
      <button id="cancelBtn" class="ghost" hidden>Cancel</button>
    </div>
  </header>

  <div id="banner" class="banner" hidden></div>

  <main id="timeline">
    <div class="welcome">
      <h2>Describe what to automate</h2>
      <p class="muted">
        The platform reads your repository, learns its conventions, explores the application for real
        locators, designs a test plan for you to approve, then writes, runs and repairs the tests.
      </p>
      <div class="examples">
        <button class="example">Automate the Resident Registration functionality</button>
        <button class="example">Add negative and boundary coverage to the login feature</button>
        <button class="example">Automate PROJ-1234</button>
        <button class="example">Diagnose and fix the failing checkout tests</button>
      </div>
    </div>
  </main>

  <footer id="composer">
    <div class="composer-row">
      <select id="mode" title="Autonomy level">
        <option value="plan_only">Plan only</option>
        <option value="generate">Generate</option>
        <option value="full" selected>Full (run + analyse)</option>
        <option value="autonomous">Autonomous (self-heal)</option>
      </select>
      <textarea id="prompt" rows="2" placeholder="e.g. Automate the Resident Registration functionality"></textarea>
      <button id="send" class="primary">Start</button>
    </div>
    <div class="composer-hint muted" id="hint"></div>
  </footer>

<script nonce="${nonce}" src="${script}"></script>
</body>
</html>`;
  }
}

/**
 * Show an approval's changes in VS Code's own diff editor.
 *
 * Reviewing generated code in a real diff view — not a webview approximation —
 * is what makes the approval decision trustworthy.
 */
export async function showDiffDocument(approval: Approval): Promise<void> {
  const files = (approval.payload?.files ?? []) as { path: string }[] | undefined;
  const title = files?.length ? `AI QA: ${files.length} proposed change(s)` : `AI QA: ${approval.kind}`;

  const document = await vscode.workspace.openTextDocument({
    content:
      `# ${approval.title}\n#\n` +
      `# ${approval.description.split('\n').join('\n# ')}\n#\n` +
      `# Approve or reject from the AI QA sidebar or the chat panel.\n\n` +
      (approval.diff_preview || '(no diff available)'),
    language: 'diff',
  });
  await vscode.window.showTextDocument(document, { preview: true, viewColumn: vscode.ViewColumn.Beside });
  void title;
}
