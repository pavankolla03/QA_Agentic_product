/**
 * Typed client for the AI QA control plane.
 *
 * Uses Node's global fetch (VS Code 1.85 ships Node 18+) so the extension has no
 * HTTP dependency. The API key is read from SecretStorage — never from settings
 * JSON, which is frequently committed to dotfiles repositories.
 */

import * as vscode from 'vscode';
import WebSocket from 'ws';

export type RunMode = 'plan_only' | 'generate' | 'full' | 'autonomous' | 'execute_only' | 'heal_only';

export interface Project {
  id: string;
  name: string;
  repository_path: string;
  base_url: string;
  api_base_url: string;
  language: string;
  framework: string;
  indexed: boolean;
  per_run_cost_limit_usd: number;
}

export interface RunSummary {
  id: string;
  project_id: string;
  project_name: string;
  instruction: string;
  mode: string;
  status: string;
  current_agent: string;
  progress: number;
  scenarios: number;
  files_changed: number;
  tests_total: number;
  tests_passed: number;
  tests_failed: number;
  total_cost_usd: number;
  total_tokens: number;
  llm_calls: number;
  duration_s: number;
  error: string;
  pending_approval_id: string;
  created_at: string;
  ended_at: string;
}

export interface RunDetail extends RunSummary {
  requirement?: Record<string, unknown> | null;
  test_plan?: Record<string, unknown> | null;
  code_bundle?: { changes?: FileChange[]; summary?: string } | null;
  standards_report?: Record<string, unknown> | null;
  execution?: Record<string, unknown> | null;
  analyses: Record<string, unknown>[];
  heals: Record<string, unknown>[];
  report?: { headline?: string; markdown?: string; html?: string; next_actions?: string[] } | null;
  traces: AgentTrace[];
  warnings: string[];
  notes: string[];
  visited: string[];
}

export interface FileChange {
  path: string;
  change_type: string;
  kind: string;
  content: string;
  original_content?: string | null;
  diff: string;
  rationale: string;
  bytes: number;
}

export interface AgentTrace {
  id: string;
  agent: string;
  status: string;
  sequence: number;
  provider: string;
  model: string;
  total_tokens: number;
  cost_usd: number;
  latency_ms: number;
  llm_calls: number;
  tool_calls: string[];
  input_summary: string;
  output_summary: string;
  error: string;
}

export interface Approval {
  id: string;
  run_id: string;
  project_id: string;
  kind: string;
  title: string;
  description: string;
  risk: string;
  payload: Record<string, unknown>;
  diff_preview: string;
  status: string;
  created_at: string;
}

export interface RunEvent {
  id?: string;
  type: string;
  agent?: string;
  level?: string;
  message?: string;
  data?: Record<string, unknown>;
  progress?: number | null;
  at?: string;
  events?: RunEvent[];
  status?: string;
}

export interface Health {
  status: string;
  version: string;
  env: string;
  database: string;
  configured_providers: string[];
  active_routes: Record<string, { provider: string; model: string; free: boolean } | null>;
  cost: Record<string, number>;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly detail?: unknown,
  ) {
    super(message);
  }
}

const SECRET_KEY = 'aiqa.apiKey';

export class ApiClient {
  private cachedKey: string | undefined;

  constructor(private readonly context: vscode.ExtensionContext) {}

  // ------------------------------------------------------------------ //
  get baseUrl(): string {
    const url = vscode.workspace.getConfiguration('aiqa').get<string>('serverUrl', 'http://127.0.0.1:8080');
    return url.replace(/\/+$/, '');
  }

  /**
   * Resolve the API key. Precedence: cached → SecretStorage → settings (migrated
   * into SecretStorage on first read) → prompt the user.
   */
  async getApiKey(promptIfMissing = true): Promise<string | undefined> {
    if (this.cachedKey) {
      return this.cachedKey;
    }
    let key = await this.context.secrets.get(SECRET_KEY);

    if (!key) {
      const fromSettings = vscode.workspace.getConfiguration('aiqa').get<string>('apiKey', '').trim();
      if (fromSettings) {
        await this.context.secrets.store(SECRET_KEY, fromSettings);
        await vscode.workspace
          .getConfiguration('aiqa')
          .update('apiKey', '', vscode.ConfigurationTarget.Global);
        vscode.window.showInformationMessage(
          'AI QA: your API key was moved from settings into the OS keychain.',
        );
        key = fromSettings;
      }
    }

    if (!key && promptIfMissing) {
      key = await vscode.window.showInputBox({
        title: 'AI QA Engineer — API key',
        prompt: 'Paste the control plane API key (from `aiqa init`). Stored in the OS keychain.',
        password: true,
        ignoreFocusOut: true,
        placeHolder: 'aiqa_...',
      });
      if (key) {
        await this.context.secrets.store(SECRET_KEY, key.trim());
      }
    }

    this.cachedKey = key?.trim() || undefined;
    return this.cachedKey;
  }

  async clearApiKey(): Promise<void> {
    this.cachedKey = undefined;
    await this.context.secrets.delete(SECRET_KEY);
  }

  // ------------------------------------------------------------------ //
  private async request<T>(
    path: string,
    init: RequestInit & { raw?: boolean } = {},
  ): Promise<T> {
    const key = await this.getApiKey();
    if (!key) {
      throw new ApiError(401, 'No API key configured. Run "AI QA: Check Connection and Providers".');
    }

    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}${path}`, {
        ...init,
        headers: {
          'Content-Type': 'application/json',
          'X-API-Key': key,
          ...(init.headers ?? {}),
        },
      });
    } catch (err) {
      throw new ApiError(
        0,
        `Cannot reach the AI QA control plane at ${this.baseUrl}. Start it with \`aiqa serve\`. (${String(err)})`,
      );
    }

    if (response.status === 401) {
      // A rotated key should not leave the extension permanently broken.
      await this.clearApiKey();
      throw new ApiError(401, 'API key was rejected. You will be asked for a new one on the next action.');
    }
    if (!response.ok) {
      let detail: unknown;
      let message = `${response.status} ${response.statusText}`;
      try {
        detail = await response.json();
        const d = (detail as { detail?: unknown }).detail;
        if (typeof d === 'string') {
          message = d;
        } else if (d) {
          message = JSON.stringify(d);
        }
      } catch {
        message = (await response.text()).slice(0, 400) || message;
      }
      throw new ApiError(response.status, message, detail);
    }

    if (init.raw) {
      return (await response.text()) as unknown as T;
    }
    if (response.status === 204) {
      return undefined as unknown as T;
    }
    return (await response.json()) as T;
  }

  // -- system --------------------------------------------------------- //
  health(): Promise<Health> {
    return this.request<Health>('/api/health');
  }

  agents(): Promise<{ agents: { name: string; capability: string; description: string }[]; modes: Record<string, string[]> }> {
    return this.request('/api/agents');
  }

  providers(): Promise<Record<string, unknown>> {
    return this.request('/api/providers');
  }

  metrics(days = 30): Promise<Record<string, any>> {
    return this.request(`/api/metrics?days=${days}`);
  }

  // -- projects ------------------------------------------------------- //
  listProjects(): Promise<Project[]> {
    return this.request<Project[]>('/api/projects');
  }

  createProject(body: Record<string, unknown>): Promise<Project> {
    return this.request<Project>('/api/projects', { method: 'POST', body: JSON.stringify(body) });
  }

  indexProject(projectId: string): Promise<Record<string, any>> {
    return this.request(`/api/projects/${projectId}/index`, { method: 'POST' });
  }

  lintProject(projectId: string): Promise<Record<string, any>> {
    return this.request(`/api/projects/${projectId}/lint`, { method: 'POST', body: JSON.stringify({ project_id: projectId }) });
  }

  projectStandards(projectId: string): Promise<Record<string, any>> {
    return this.request(`/api/projects/${projectId}/standards`);
  }

  // -- runs ----------------------------------------------------------- //
  createRun(body: Record<string, unknown>): Promise<RunSummary> {
    return this.request<RunSummary>('/api/runs', { method: 'POST', body: JSON.stringify(body) });
  }

  listRuns(projectId?: string, limit = 30): Promise<RunSummary[]> {
    const query = new URLSearchParams({ limit: String(limit) });
    if (projectId) {
      query.set('project_id', projectId);
    }
    return this.request<RunSummary[]>(`/api/runs?${query.toString()}`);
  }

  getRun(runId: string): Promise<RunDetail> {
    return this.request<RunDetail>(`/api/runs/${runId}`);
  }

  runTrace(runId: string): Promise<Record<string, any>> {
    return this.request(`/api/runs/${runId}/trace`);
  }

  runDiff(runId: string): Promise<string> {
    return this.request<string>(`/api/runs/${runId}/diff`, { raw: true });
  }

  runReport(runId: string, format: 'json' | 'markdown' | 'html' = 'markdown'): Promise<string> {
    return this.request<string>(`/api/runs/${runId}/report?format=${format}`, { raw: format !== 'json' });
  }

  cancelRun(runId: string): Promise<Record<string, string>> {
    return this.request(`/api/runs/${runId}/cancel`, { method: 'POST' });
  }

  resumeRun(runId: string): Promise<RunSummary> {
    return this.request<RunSummary>(`/api/runs/${runId}/resume`, { method: 'POST' });
  }

  // -- approvals ------------------------------------------------------ //
  listApprovals(runId?: string): Promise<Approval[]> {
    const query = runId ? `?run_id=${encodeURIComponent(runId)}` : '';
    return this.request<Approval[]>(`/api/approvals${query}`);
  }

  respondApproval(
    approvalId: string,
    approved: boolean,
    comment = '',
    changesRequested = false,
  ): Promise<Record<string, unknown>> {
    return this.request(`/api/approvals/${approvalId}`, {
      method: 'POST',
      body: JSON.stringify({ approved, comment, changes_requested: changesRequested }),
    });
  }

  // -- live stream ---------------------------------------------------- //
  /**
   * Subscribe to a run's live event stream. Reconnects with backoff while the
   * run is still in flight, so a laptop sleeping mid-run does not lose the tail.
   */
  async streamRun(
    runId: string,
    onEvent: (event: RunEvent) => void,
    onClose?: (reason: string) => void,
  ): Promise<() => void> {
    const key = await this.getApiKey();
    if (!key) {
      throw new ApiError(401, 'No API key configured.');
    }

    const wsUrl = `${this.baseUrl.replace(/^http/, 'ws')}/ws/runs/${runId}?api_key=${encodeURIComponent(key)}`;
    let socket: WebSocket | undefined;
    let closed = false;
    let attempt = 0;
    let timer: NodeJS.Timeout | undefined;

    const connect = () => {
      if (closed) {
        return;
      }
      socket = new WebSocket(wsUrl);

      socket.on('message', (raw: WebSocket.RawData) => {
        try {
          const event = JSON.parse(raw.toString()) as RunEvent;
          if (event.type === '__ping__') {
            return;
          }
          if (event.type === '__replay__') {
            (event.events ?? []).forEach(onEvent);
            return;
          }
          if (event.type === '__closed__') {
            closed = true;
            onClose?.(event.status ?? 'closed');
            socket?.close();
            return;
          }
          onEvent(event);
        } catch {
          /* ignore malformed frames */
        }
      });

      socket.on('open', () => {
        attempt = 0;
      });

      socket.on('close', () => {
        if (closed) {
          return;
        }
        attempt += 1;
        if (attempt > 5) {
          onClose?.('disconnected');
          return;
        }
        timer = setTimeout(connect, Math.min(1000 * 2 ** attempt, 15000));
      });

      socket.on('error', () => {
        /* handled by 'close' */
      });
    };

    connect();

    return () => {
      closed = true;
      if (timer) {
        clearTimeout(timer);
      }
      socket?.close();
    };
  }
}
