/**
 * The chat, docked in the sidebar.
 *
 * It used to be a webview *panel*: an editor tab you had to summon with a
 * command, competing for space with the code you were reviewing. Clicking the
 * extension's icon gave you a tree of links instead of somewhere to type, so
 * the first thing anyone wanted to do took two steps and a memorised command
 * name.
 *
 * Copilot and Claude Code both dock their chat as a `WebviewView` in the
 * sidebar, and they are right: the conversation is the primary surface, so it
 * should be the thing that appears when you click the icon, and it should stay
 * put while you read the files it produced.
 *
 * `retainContextWhenHidden` matters more here than it usually does. A run takes
 * minutes on free models, and a QA engineer will switch to another view while
 * it works. Rebuilding the webview would drop the transcript and the live
 * stream along with it.
 */

import * as vscode from 'vscode';
import { ApiClient, Approval, RunEvent, RunMode } from '../client/apiClient';
import { showDiffDocument } from './chatPanel';

export class ChatViewProvider implements vscode.WebviewViewProvider {
  public static readonly viewType = 'aiqa.chatView';

  private view: vscode.WebviewView | undefined;
  private stopStream: (() => void) | undefined;
  private activeRunId: string | undefined;

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly api: ApiClient,
    private readonly onChange: () => void,
  ) {}

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.context.extensionUri, 'media')],
    };
    view.description = 'QA automation';
    view.webview.html = this.html(view.webview);
    view.webview.onDidReceiveMessage((message) => void this.handleMessage(message));
    view.onDidDispose(() => {
      this.stopStream?.();
      this.view = undefined;
    });
    void this.sendContext();
  }

  /** Bring the chat forward, creating it if the sidebar has not opened yet. */
  async reveal(): Promise<void> {
    if (this.view) {
      this.view.show?.(true);
      return;
    }
    // Nothing has resolved the view yet, so there is no handle to show. The
    // generated `<viewId>.focus` command opens the container and builds it.
    await vscode.commands.executeCommand(`${ChatViewProvider.viewType}.focus`);
  }

  // ------------------------------------------------------------------ //
  private post(message: Record<string, unknown>): void {
    void this.view?.webview.postMessage(message);
  }

  private async sendContext(): Promise<void> {
    try {
      const [health, projects] = await Promise.all([this.api.health(), this.api.listProjects()]);
      const config = vscode.workspace.getConfiguration('aiqa');
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
        projectId: config.get<string>('projectId', ''),
        defaultMode: config.get<string>('defaultMode', 'full'),
      });
    } catch (err) {
      this.post({ type: 'context', connected: false, error: String((err as Error).message) });
    }
  }

  /**
   * Decide what a typed message means before spending five minutes on it.
   *
   * Every message used to go straight to `startRun`, so "Hi" became a
   * ten-agent pipeline — a crawl, code generation, the compile gate — and
   * produced no answer at all. Most messages are answered in milliseconds and
   * never touch a model; the rest become runs, as they always did.
   *
   * A mode the user picked deliberately in the dropdown always wins. They have
   * said what they want; asking the server to reclassify it would be rude.
   */
  private async submit(text: string, chosenMode?: RunMode): Promise<void> {
    if (!text) {
      return;
    }
    this.post({ type: 'userMessage', text });

    const config = vscode.workspace.getConfiguration('aiqa');
    const projectId = config.get<string>('projectId', '');
    const explicit = chosenMode && chosenMode !== config.get<string>('defaultMode', 'full');

    if (!explicit) {
      this.post({ type: 'thinking' });
      try {
        const reply = await this.api.chat(projectId, text);
        if (reply.kind === 'reply') {
          this.post({ type: 'assistantMessage', text: reply.text, suggestions: reply.suggestions });
          return;
        }
        await this.startRun(text, reply.mode ?? chosenMode ?? 'full', {}, { echo: false });
        return;
      } catch {
        // The classifier is a convenience, not a gate. If the control plane
        // cannot be reached the message still becomes a run, which is what it
        // would have done before any of this existed.
        this.post({ type: 'thinkingDone' });
      }
    }

    await this.startRun(text, chosenMode ?? 'full', {}, { echo: false });
  }

  async startRun(
    instruction: string,
    mode: RunMode,
    extra: Record<string, unknown> = {},
    options: { echo?: boolean } = {},
  ): Promise<void> {
    await this.reveal();
    if (options.echo !== false) {
      this.post({ type: 'userMessage', text: instruction });
    }
    const config = vscode.workspace.getConfiguration('aiqa');
    let projectId = config.get<string>('projectId', '');

    if (!projectId) {
      // Typing a prompt is a clear statement of intent, so walk the setup
      // rather than refusing and leaving the user to work out what is missing.
      await vscode.commands.executeCommand('aiqa.setup');
      projectId = config.get<string>('projectId', '');
      if (!projectId) {
        this.post({
          type: 'error',
          message: 'No project is bound to this workspace. Run "AI QA: Set Up" to finish configuring.',
        });
        return;
      }
    }

    this.post({ type: 'runStarting', instruction, mode, echoed: options.echo === false });
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

  async attach(runId: string): Promise<void> {
    this.stopStream?.();
    this.activeRunId = runId;

    try {
      this.post({ type: 'runSnapshot', run: await this.api.getRun(runId) });
    } catch {
      /* the stream replay will fill it in */
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
      const approval = approvals.find((a: Approval) => a.id === approvalId);
      if (!approval) {
        return;
      }
      this.post({ type: 'approval', approval });
      // The sidebar is for deciding; the diff belongs in a real editor.
      if (vscode.workspace.getConfiguration('aiqa').get<boolean>('openDiffOnApproval', true)) {
        await showDiffDocument(approval);
      }
    } catch {
      /* non-fatal */
    }
  }

  private async finish(runId: string): Promise<void> {
    try {
      this.post({ type: 'runFinished', run: await this.api.getRun(runId) });
    } catch {
      /* ignore */
    }
  }

  // ------------------------------------------------------------------ //
  private async handleMessage(message: Record<string, any>): Promise<void> {
    switch (message.command) {
      case 'submit':
        await this.submit(String(message.text ?? '').trim(), message.mode as RunMode | undefined);
        break;

      case 'approve':
      case 'reject': {
        const approved = message.command === 'approve';
        try {
          await this.api.respondApproval(
            String(message.approvalId),
            approved,
            String(message.comment ?? ''),
          );
          this.post({ type: 'approvalResolved', approvalId: message.approvalId, approved });
          this.onChange();
        } catch (err) {
          this.post({ type: 'error', message: String((err as Error).message) });
        }
        break;
      }

      case 'viewDiff': {
        const approvals = await this.api.listApprovals();
        const approval = approvals.find((a: Approval) => a.id === message.approvalId);
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
            const document = await vscode.workspace.openTextDocument(uri);
            // Beside, not on top: the point is to read the file while the chat
            // that produced it stays visible.
            await vscode.window.showTextDocument(document, { viewColumn: vscode.ViewColumn.One });
          } catch {
            this.post({
              type: 'error',
              message: `Cannot open ${message.path} — it may not be written yet.`,
            });
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

      case 'runCommand':
        await vscode.commands.executeCommand(String(message.id));
        break;

      case 'refresh':
        await this.sendContext();
        break;

      default:
        break;
    }
  }

  private html(webview: vscode.Webview): string {
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
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}'; font-src ${webview.cspSource};">
<link href="${style}" rel="stylesheet">
<title>AI QA Engineer</title>
</head>
<body class="sidebar">
  <div id="banner" class="banner" hidden></div>

  <div id="timeline" class="timeline">
    <div class="welcome">
      <div class="welcome-title">What should I automate?</div>
      <div class="welcome-sub">Describe a feature in the words your team uses. Naming the
        cases you care about gives a sharper plan than "test the page".</div>
      <div class="suggestions">
        <button class="suggestion" data-fill="Automate the login flow: valid sign-in, wrong password, and empty fields">Login flow</button>
        <button class="suggestion" data-fill="Automate the registration form: valid submission, required-field validation, and duplicate rejection">Registration form</button>
        <button class="suggestion" data-cmd="aiqa.exploreApplication">Explore the app</button>
        <button class="suggestion" data-cmd="aiqa.showCoverage">Coverage gaps</button>
      </div>
    </div>
  </div>

  <div class="composer">
    <div class="composer-meta">
      <span id="statusDot" class="dot"></span>
      <span id="statusText" class="muted">connecting…</span>
      <span class="spacer"></span>
      <span id="tokenMeter" class="muted"></span>
      <span id="costMeter" class="muted"></span>
      <button id="cancelBtn" class="ghost" hidden>Stop</button>
    </div>
    <textarea id="prompt" rows="3" placeholder="Automate the resident registration page…"></textarea>
    <div class="composer-actions">
      <select id="mode" title="How far the run should go">
        <option value="full">Full run</option>
        <option value="plan_only">Plan only</option>
        <option value="generate">Generate code</option>
        <option value="execute_only">Run tests</option>
        <option value="heal_only">Heal failures</option>
      </select>
      <span id="hint" class="muted"></span>
      <button id="send" class="primary">Send</button>
    </div>
  </div>

  <script nonce="${nonce}" src="${script}"></script>
</body>
</html>`;
  }
}
