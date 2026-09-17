/**
 * Starting the control plane so the QA engineer does not have to.
 *
 * Everything the extension does is a call to the control plane, so until it is
 * running the sidebar is a wall of errors. Asking the user to open a terminal
 * and run `aiqa serve` was the only manual step left in onboarding, and it is
 * the step most likely to be got wrong: wrong directory, wrong interpreter, no
 * virtualenv.
 *
 * So the extension starts it. Three rules keep that honest:
 *
 *  1. **Never fight for the port.** If something already answers `/api/health`
 *     at the configured URL — a server the user started, a shared staging
 *     instance, a colleague's tunnel — it is used as-is and nothing is spawned.
 *  2. **Only ever stop what we started.** A server the extension did not spawn
 *     is never killed, including on shutdown.
 *  3. **Fail loudly, not silently.** If the interpreter is missing or the
 *     process dies, the reason goes to the output channel and the user is told
 *     how to start it themselves.
 */

import { ChildProcess, spawn } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import * as vscode from 'vscode';

/** A directory is the platform if it can serve: the entry point has to be there. */
function isPlatformCheckout(dir: string): boolean {
  return !!dir && fs.existsSync(path.join(dir, 'services', 'api_gateway', 'cli.py'));
}

/** How long to wait for a freshly spawned server to answer. */
const STARTUP_TIMEOUT_MS = 60_000;
const POLL_INTERVAL_MS = 400;

export type ServerState = 'external' | 'managed' | 'stopped' | 'failed';

/** Where a working platform checkout was last found, remembered per machine. */
const REMEMBERED_ROOT = 'aiqa.platformRoot';

export class ServerManager implements vscode.Disposable {
  private child: ChildProcess | undefined;
  private state: ServerState = 'stopped';
  private lastError = '';

  constructor(
    private readonly output: vscode.LogOutputChannel,
    private readonly baseUrl: () => string,
    private readonly memento?: vscode.Memento,
  ) {}

  get status(): ServerState {
    return this.state;
  }

  get error(): string {
    return this.lastError;
  }

  /** True when this extension owns the running server process. */
  get isManaged(): boolean {
    return this.state === 'managed' && this.child !== undefined && this.child.exitCode === null;
  }

  /**
   * Make sure a control plane is reachable, starting one if allowed.
   *
   * Returns true when the server answers, whoever started it.
   */
  async ensure(): Promise<boolean> {
    if (await this.reachable()) {
      // Something is already serving. It might be ours from a previous window,
      // a manually started process, or a remote instance — either way, leave it.
      if (this.state !== 'managed') {
        this.state = 'external';
      }
      return true;
    }

    const config = vscode.workspace.getConfiguration('aiqa');
    if (!config.get<boolean>('autoStartServer', true)) {
      this.output.info('control plane not reachable and autoStartServer is off');
      return false;
    }
    if (!this.isLocal()) {
      // A remote URL is someone else's server; spawning a local process would
      // not make that URL answer.
      this.output.warn(`cannot auto-start a remote control plane at ${this.baseUrl()}`);
      return false;
    }
    return this.start();
  }

  /** Spawn the control plane and wait for it to answer. */
  async start(): Promise<boolean> {
    if (this.isManaged) {
      return true;
    }
    const cwd = this.repositoryRoot();
    if (!cwd) {
      this.fail('no workspace folder is open, so there is no repository to serve from');
      return false;
    }
    if (!isPlatformCheckout(cwd)) {
      // The common case, and the one that made the chat look broken: a QA
      // engineer opens the repository they are testing, not this platform's
      // checkout, so nothing starts and every call 404s into a dead sidebar.
      this.fail(
        `${cwd} is not a QAgentic checkout, so the control plane cannot be started ` +
          'from it. Run "QAgentic: Set Up" to point at the platform directory, or start it ' +
          'yourself with `python -m services.api_gateway.cli serve`.',
      );
      return false;
    }
    // Remember it, so the next window -- any window -- starts without asking.
    void this.memento?.update(REMEMBERED_ROOT, cwd);
    const python = this.pythonPath(cwd);
    if (!python) {
      this.fail(
        'no Python interpreter found. Set `aiqa.pythonPath`, or create a virtualenv in the workspace root.',
      );
      return false;
    }

    const port = this.port();
    const args = ['-m', 'services.api_gateway.cli', 'serve', '--port', String(port)];
    this.output.info(`starting control plane: ${python} ${args.join(' ')} (cwd ${cwd})`);

    const child = spawn(python, args, {
      cwd,
      env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' },
      windowsHide: true,
    });
    this.child = child;
    this.state = 'managed';

    child.stdout?.on('data', (chunk: Buffer) => this.log(chunk));
    child.stderr?.on('data', (chunk: Buffer) => this.log(chunk));
    child.on('exit', (code, signal) => {
      // An exit during normal shutdown is expected; an exit while we still
      // believe we are managing a server is a failure worth surfacing.
      if (this.state === 'managed') {
        this.fail(`control plane exited (code ${code ?? 'null'}, signal ${signal ?? 'none'})`);
      }
      this.child = undefined;
    });

    const started = await this.waitForHealth(STARTUP_TIMEOUT_MS);
    if (!started) {
      this.fail(`control plane did not answer on port ${port} within ${STARTUP_TIMEOUT_MS / 1000}s`);
      this.stop();
      return false;
    }
    this.output.info(`control plane ready at ${this.baseUrl()}`);
    return true;
  }

  /** Stop the server, but only if we started it. */
  stop(): void {
    const child = this.child;
    this.child = undefined;
    this.state = 'stopped';
    if (!child || child.exitCode !== null) {
      return;
    }
    this.output.info('stopping the control plane this extension started');
    // SIGTERM lets uvicorn close its sockets; Windows has no SIGTERM, so the
    // default kill() (TerminateProcess) is what actually applies there.
    child.kill(process.platform === 'win32' ? undefined : 'SIGTERM');
  }

  async restart(): Promise<boolean> {
    this.stop();
    return this.start();
  }

  dispose(): void {
    this.stop();
  }

  // ----------------------------------------------------------------- //
  // Internals
  // ----------------------------------------------------------------- //
  private log(chunk: Buffer): void {
    for (const line of chunk.toString('utf8').split(/\r?\n/)) {
      if (line.trim()) {
        this.output.debug(`[server] ${line}`);
      }
    }
  }

  private fail(message: string): void {
    this.state = 'failed';
    this.lastError = message;
    this.output.error(message);
  }

  private isLocal(): boolean {
    try {
      const host = new URL(this.baseUrl()).hostname;
      return host === '127.0.0.1' || host === 'localhost' || host === '::1' || host === '0.0.0.0';
    } catch {
      return false;
    }
  }

  private port(): number {
    try {
      const url = new URL(this.baseUrl());
      return Number(url.port) || (url.protocol === 'https:' ? 443 : 80);
    } catch {
      return 8080;
    }
  }

  /**
   * The directory to serve from: the repository that contains the platform.
   *
   * `aiqa.repositoryPath` wins when set, because a QA engineer's workspace is
   * usually their *test* repository, not this platform's checkout.
   */
  private repositoryRoot(): string {
    const configured = vscode.workspace.getConfiguration('aiqa').get<string>('repositoryPath', '');
    if (configured && isPlatformCheckout(configured)) {
      return configured;
    }
    const folders = vscode.workspace.workspaceFolders ?? [];
    for (const folder of folders) {
      // The platform checkout is identifiable: it has the service package.
      if (isPlatformCheckout(folder.uri.fsPath)) {
        return folder.uri.fsPath;
      }
    }
    // Nothing here is the platform, so fall back to wherever it was last found.
    // This is what lets the chat work from the repository under test, which is
    // the only workspace a QA engineer actually has open.
    const remembered = this.memento?.get<string>(REMEMBERED_ROOT, '') ?? '';
    if (remembered && isPlatformCheckout(remembered)) {
      return remembered;
    }
    return folders[0]?.uri.fsPath ?? '';
  }

  /** The directory the extension would serve from, or '' if it cannot find one. */
  get knownPlatformRoot(): string {
    const root = this.repositoryRoot();
    return isPlatformCheckout(root) ? root : '';
  }

  /** Record a directory the user picked, after checking it is really one. */
  rememberPlatformRoot(dir: string): boolean {
    if (!isPlatformCheckout(dir)) {
      return false;
    }
    void this.memento?.update(REMEMBERED_ROOT, dir);
    return true;
  }

  /** Prefer a virtualenv in the repository, then the configured or system Python. */
  private pythonPath(cwd: string): string {
    const configured = vscode.workspace.getConfiguration('aiqa').get<string>('pythonPath', '');
    if (configured) {
      return configured;
    }
    const windows = process.platform === 'win32';
    const candidates = windows
      ? [path.join(cwd, '.venv', 'Scripts', 'python.exe'), path.join(cwd, 'venv', 'Scripts', 'python.exe')]
      : [path.join(cwd, '.venv', 'bin', 'python'), path.join(cwd, 'venv', 'bin', 'python')];

    for (const candidate of candidates) {
      if (fs.existsSync(candidate)) {
        return candidate;
      }
    }
    // Fall back to whatever is on PATH. Not verified here: spawn will report
    // ENOENT, which is a clearer error than a guess made now.
    return windows ? 'python' : 'python3';
  }

  private async reachable(): Promise<boolean> {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 2000);
      const response = await fetch(`${this.baseUrl().replace(/\/$/, '')}/api/health`, {
        signal: controller.signal,
      });
      clearTimeout(timer);
      return response.ok;
    } catch {
      return false;
    }
  }

  private async waitForHealth(timeoutMs: number): Promise<boolean> {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (this.child && this.child.exitCode !== null) {
        return false; // the process died; no point waiting out the timeout
      }
      if (await this.reachable()) {
        return true;
      }
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
    }
    return false;
  }
}
