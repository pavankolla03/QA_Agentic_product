/**
 * First run.
 *
 * Four things have to be true before a prompt can do anything: the control
 * plane is up, the extension holds a key the server accepts, a project points
 * at a repository, and that project knows the URL of the application to test.
 *
 * Previously each was discovered by failing at it — a 401 here, an empty
 * sidebar there, a "No project is bound to this workspace" when you finally
 * typed something. This walks the four in order and stops at the first one it
 * cannot satisfy, so the answer to "why is nothing happening" is on screen
 * rather than inferred.
 *
 * It is idempotent: everything already satisfied is skipped silently, so it is
 * also the right thing to run when something breaks later.
 */

import * as vscode from 'vscode';
import { ApiClient } from '../client/apiClient';

export interface SetupState {
  serverUp: boolean;
  credentialsOk: boolean;
  projectId: string;
  baseUrl: string;
  ready: boolean;
  blocker: string;
}

const SHOWN_KEY = 'aiqa.onboarding.completed';

export class Onboarding {
  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly api: ApiClient,
    private readonly ensureServer: () => Promise<boolean>,
  ) {}

  /** What still stands between the user and a working run. */
  async inspect(): Promise<SetupState> {
    const state: SetupState = {
      serverUp: false,
      credentialsOk: false,
      projectId: vscode.workspace.getConfiguration('aiqa').get<string>('projectId', ''),
      baseUrl: '',
      ready: false,
      blocker: '',
    };

    try {
      await this.api.health();
      state.serverUp = true;
    } catch {
      state.blocker = 'the control plane is not running';
      return state;
    }

    // `health` is unauthenticated and answers 200 to anything, so it proves
    // the server is up and nothing about the key.
    const credentials = await this.api.verifyCredentials();
    state.credentialsOk = credentials.ok;
    if (!credentials.ok) {
      state.blocker = 'the API key is missing or rejected';
      return state;
    }

    if (!state.projectId) {
      state.blocker = 'this workspace is not registered as a project';
      return state;
    }

    try {
      const projects = await this.api.listProjects();
      const project = projects.find((p) => p.id === state.projectId);
      if (!project) {
        state.blocker = `project ${state.projectId} no longer exists on this server`;
        return state;
      }
      state.baseUrl = project.base_url ?? '';
    } catch {
      state.blocker = 'could not read the project list';
      return state;
    }

    if (!state.baseUrl) {
      // Not fatal: generation works from the repository alone. But without a
      // URL there is nothing to crawl, so locators cannot be verified and the
      // output will be scaffolding. Better said now than discovered later.
      state.blocker = 'the project has no application URL, so nothing can be explored';
    }

    state.ready = true;
    return state;
  }

  /** Walk the unmet steps, in order, stopping at the first refusal. */
  async run(options: { silentIfReady?: boolean } = {}): Promise<SetupState> {
    let state = await this.inspect();
    if (state.ready && state.baseUrl) {
      if (!options.silentIfReady) {
        void vscode.window.showInformationMessage('AI QA: everything is set up.');
      }
      await this.context.globalState.update(SHOWN_KEY, true);
      return state;
    }

    if (!state.serverUp) {
      const started = await this.ensureServer();
      if (!started) {
        void vscode.window.showErrorMessage(
          'AI QA: the control plane could not be started. Run `aiqa serve` from the platform directory.',
        );
        return state;
      }
      state = await this.inspect();
    }

    if (!state.credentialsOk) {
      // Asking clears nothing on its own; a previously stored wrong key has to
      // go first or the prompt never appears.
      await this.api.clearApiKey();
      const key = await this.api.getApiKey();
      if (!key) {
        return this.inspect();
      }
      state = await this.inspect();
      if (!state.credentialsOk) {
        void vscode.window.showErrorMessage(`AI QA: ${state.blocker}.`);
        return state;
      }
    }

    if (!state.projectId) {
      const registered = await vscode.commands.executeCommand<string | undefined>(
        'aiqa.registerProject',
      );
      if (!registered) {
        return this.inspect();
      }
      state = await this.inspect();
    }

    if (state.ready && !state.baseUrl) {
      void vscode.window.showWarningMessage(
        'AI QA: this project has no application URL. Generation will work, but nothing can be ' +
          'explored, so locators cannot be verified.',
      );
    }

    await this.context.globalState.update(SHOWN_KEY, true);
    return state;
  }

  /**
   * Offer setup once, on the first activation that finds things unconfigured.
   *
   * Deliberately an offer and not a takeover: an editor that starts asking
   * questions the moment it opens is worse than one that waits to be asked.
   */
  async offerOnce(): Promise<void> {
    if (this.context.globalState.get<boolean>(SHOWN_KEY)) {
      return;
    }
    const state = await this.inspect();
    if (state.ready && state.baseUrl) {
      await this.context.globalState.update(SHOWN_KEY, true);
      return;
    }
    const picked = await vscode.window.showInformationMessage(
      `AI QA Engineer needs a moment of setup — ${state.blocker}.`,
      'Set it up',
      'Not now',
    );
    if (picked === 'Set it up') {
      await this.run();
    } else if (picked === 'Not now') {
      // Asked and declined is an answer; do not ask again on every window.
      await this.context.globalState.update(SHOWN_KEY, true);
    }
  }
}
