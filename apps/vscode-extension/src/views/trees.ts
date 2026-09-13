/** Sidebar tree views: approvals, runs, agents, usage. */

import * as vscode from 'vscode';
import { ApiClient, Approval, RunSummary } from '../client/apiClient';

const STATUS_ICON: Record<string, string> = {
  queued: 'clock',
  running: 'sync~spin',
  waiting_approval: 'person',
  paused: 'debug-pause',
  succeeded: 'pass-filled',
  failed: 'error',
  cancelled: 'circle-slash',
  budget_exceeded: 'warning',
};

const RISK_ICON: Record<string, string> = {
  low: 'shield',
  medium: 'warning',
  high: 'flame',
  critical: 'flame',
};

export class ApprovalItem extends vscode.TreeItem {
  constructor(readonly approval: Approval) {
    super(approval.title || approval.kind, vscode.TreeItemCollapsibleState.None);
    this.contextValue = 'approval';
    this.id = approval.id;
    this.description = `${approval.kind} · risk ${approval.risk}`;
    this.iconPath = new vscode.ThemeIcon(
      RISK_ICON[approval.risk] ?? 'question',
      approval.risk === 'high' || approval.risk === 'critical'
        ? new vscode.ThemeColor('charts.red')
        : new vscode.ThemeColor('charts.yellow'),
    );
    this.tooltip = new vscode.MarkdownString(
      `**${approval.title}**\n\n${approval.description}\n\n_run ${approval.run_id}_`,
    );
    this.command = { command: 'aiqa.reviewDiff', title: 'Review', arguments: [this] };
  }
}

export class ApprovalsProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
  private readonly emitter = new vscode.EventEmitter<vscode.TreeItem | undefined>();
  readonly onDidChangeTreeData = this.emitter.event;
  private cache: Approval[] = [];

  constructor(private readonly api: ApiClient) {}

  refresh(): void {
    this.emitter.fire(undefined);
  }

  get pending(): Approval[] {
    return this.cache;
  }

  getTreeItem(element: vscode.TreeItem): vscode.TreeItem {
    return element;
  }

  async getChildren(): Promise<vscode.TreeItem[]> {
    try {
      this.cache = await this.api.listApprovals();
    } catch (err) {
      return [placeholder(`Not connected — ${String((err as Error).message).slice(0, 80)}`, 'debug-disconnect')];
    }
    if (this.cache.length === 0) {
      return [placeholder('Nothing waiting for review', 'check-all')];
    }
    return this.cache.map((a) => new ApprovalItem(a));
  }
}

export class RunItem extends vscode.TreeItem {
  constructor(readonly run: RunSummary) {
    super(run.instruction.slice(0, 72) || run.id, vscode.TreeItemCollapsibleState.None);
    this.contextValue = 'run';
    this.id = run.id;

    const bits: string[] = [run.status];
    if (run.status === 'running' && run.current_agent) {
      bits.push(run.current_agent);
    }
    if (run.tests_total) {
      bits.push(`${run.tests_passed}/${run.tests_total} passing`);
    } else if (run.scenarios) {
      bits.push(`${run.scenarios} scenarios`);
    }
    if (run.total_cost_usd > 0) {
      bits.push(`$${run.total_cost_usd.toFixed(4)}`);
    }
    this.description = bits.join(' · ');

    const color =
      run.status === 'succeeded'
        ? 'charts.green'
        : run.status === 'failed' || run.status === 'budget_exceeded'
          ? 'charts.red'
          : run.status === 'waiting_approval'
            ? 'charts.yellow'
            : 'charts.blue';
    this.iconPath = new vscode.ThemeIcon(STATUS_ICON[run.status] ?? 'circle-outline', new vscode.ThemeColor(color));

    this.tooltip = new vscode.MarkdownString(
      [
        `**${run.instruction}**`,
        '',
        `| | |`,
        `|---|---|`,
        `| status | ${run.status} |`,
        `| mode | ${run.mode} |`,
        `| scenarios | ${run.scenarios} |`,
        `| files | ${run.files_changed} |`,
        `| tests | ${run.tests_passed}/${run.tests_total} |`,
        `| cost | $${run.total_cost_usd.toFixed(6)} (${run.total_tokens.toLocaleString()} tokens) |`,
        `| duration | ${run.duration_s.toFixed(1)}s |`,
        run.error ? `| error | ${run.error.slice(0, 160)} |` : '',
      ]
        .filter(Boolean)
        .join('\n'),
    );
    this.command = { command: 'aiqa.openRun', title: 'Open run', arguments: [run.id] };
  }
}

export class RunsProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
  private readonly emitter = new vscode.EventEmitter<vscode.TreeItem | undefined>();
  readonly onDidChangeTreeData = this.emitter.event;

  constructor(
    private readonly api: ApiClient,
    private readonly projectId: () => string,
  ) {}

  refresh(): void {
    this.emitter.fire(undefined);
  }

  getTreeItem(element: vscode.TreeItem): vscode.TreeItem {
    return element;
  }

  async getChildren(): Promise<vscode.TreeItem[]> {
    let runs: RunSummary[];
    try {
      runs = await this.api.listRuns(this.projectId() || undefined, 30);
    } catch (err) {
      return [placeholder(`Not connected — ${String((err as Error).message).slice(0, 80)}`, 'debug-disconnect')];
    }
    if (runs.length === 0) {
      return [placeholder('No runs yet — press Ctrl+Alt+Q to start', 'rocket')];
    }
    return runs.map((run) => new RunItem(run));
  }
}

export class AgentsProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
  private readonly emitter = new vscode.EventEmitter<vscode.TreeItem | undefined>();
  readonly onDidChangeTreeData = this.emitter.event;

  constructor(private readonly api: ApiClient) {}

  refresh(): void {
    this.emitter.fire(undefined);
  }

  getTreeItem(element: vscode.TreeItem): vscode.TreeItem {
    return element;
  }

  async getChildren(): Promise<vscode.TreeItem[]> {
    try {
      const data = await this.api.agents();
      return data.agents.map((agent) => {
        const item = new vscode.TreeItem(agent.name, vscode.TreeItemCollapsibleState.None);
        item.description = agent.capability;
        item.iconPath = new vscode.ThemeIcon('circuit-board');
        item.tooltip = agent.description;
        return item;
      });
    } catch {
      return [placeholder('Not connected', 'debug-disconnect')];
    }
  }
}

function placeholder(text: string, icon: string): vscode.TreeItem {
  const item = new vscode.TreeItem(text, vscode.TreeItemCollapsibleState.None);
  item.iconPath = new vscode.ThemeIcon(icon);
  return item;
}
