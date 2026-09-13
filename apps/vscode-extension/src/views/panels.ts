/**
 * The v2 sidebar sections.
 *
 * Each one answers a question a QA engineer actually asks mid-task — what needs
 * my approval, why did that fail, what did the platform repair, what does it
 * already know about this project, and what is it costing me — without leaving
 * the editor.
 */

import * as vscode from 'vscode';
import { ApiClient } from '../client/apiClient';

/** Shared base: refreshable, connection-aware, never throws into the tree. */
abstract class BaseProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
  protected readonly emitter = new vscode.EventEmitter<vscode.TreeItem | undefined>();
  readonly onDidChangeTreeData = this.emitter.event;

  constructor(protected readonly api: ApiClient) {}

  refresh(): void {
    this.emitter.fire(undefined);
  }

  getTreeItem(element: vscode.TreeItem): vscode.TreeItem {
    return element;
  }

  async getChildren(): Promise<vscode.TreeItem[]> {
    try {
      return await this.load();
    } catch (err) {
      return [note(`Not connected — ${String((err as Error).message).slice(0, 70)}`, 'debug-disconnect')];
    }
  }

  protected abstract load(): Promise<vscode.TreeItem[]>;
}

function note(label: string, icon: string, description = ''): vscode.TreeItem {
  const item = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.None);
  item.iconPath = new vscode.ThemeIcon(icon);
  if (description) {
    item.description = description;
  }
  return item;
}

function metric(label: string, value: string | number, icon: string, tooltip = ''): vscode.TreeItem {
  const item = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.None);
  item.description = String(value);
  item.iconPath = new vscode.ThemeIcon(icon);
  if (tooltip) {
    item.tooltip = tooltip;
  }
  return item;
}

function action(label: string, command: string, icon: string, tooltip = ''): vscode.TreeItem {
  const item = new vscode.TreeItem(label, vscode.TreeItemCollapsibleState.None);
  item.iconPath = new vscode.ThemeIcon(icon);
  item.command = { command, title: label };
  if (tooltip) {
    item.tooltip = tooltip;
  }
  return item;
}

const projectId = () => vscode.workspace.getConfiguration('aiqa').get<string>('projectId', '');

// =========================================================================== //
/** Quick actions — the entry point for someone who has not opened the chat. */
export class AssistantProvider extends BaseProvider {
  protected async load(): Promise<vscode.TreeItem[]> {
    const items = [
      action('Ask the AI QA engineer…', 'aiqa.openChat', 'comment-discussion'),
      action('Generate automation', 'aiqa.generateAutomation', 'sparkle'),
      action('Explore the application', 'aiqa.exploreApplication', 'browser'),
      action('Run existing tests', 'aiqa.runTests', 'play'),
      action('Diagnose and self-heal', 'aiqa.selfHeal', 'wrench'),
      action('Validate standards', 'aiqa.validateStandards', 'law'),
    ];
    if (!projectId()) {
      items.unshift(
        action('Register this workspace', 'aiqa.registerProject', 'add', 'Required before the first run'),
      );
    }
    return items;
  }
}

// =========================================================================== //
/** Failures from the most recent runs, with their diagnosed root cause. */
export class FailuresProvider extends BaseProvider {
  protected async load(): Promise<vscode.TreeItem[]> {
    const runs = await this.api.listRuns(projectId() || undefined, 10);
    const withFailures = runs.filter((r) => r.tests_failed > 0);
    if (withFailures.length === 0) {
      return [note('No failing tests', 'pass-filled')];
    }

    const items: vscode.TreeItem[] = [];
    for (const run of withFailures.slice(0, 5)) {
      const detail = await this.api.getRun(run.id);
      for (const raw of detail.analyses ?? []) {
        const analysis = raw as Record<string, any>;
        const defect = Boolean(analysis.is_product_defect);
        const item = new vscode.TreeItem(
          String(analysis.test_id || analysis.test_name || 'unknown'),
          vscode.TreeItemCollapsibleState.None,
        );
        item.description = `${analysis.category}${defect ? ' · PRODUCT DEFECT' : ''}`;
        item.iconPath = new vscode.ThemeIcon(
          defect ? 'bug' : analysis.healable ? 'wrench' : 'error',
          new vscode.ThemeColor(defect ? 'charts.red' : analysis.healable ? 'charts.yellow' : 'charts.red'),
        );
        item.tooltip = new vscode.MarkdownString(
          [
            `**${analysis.test_id}**`,
            '',
            `Category: \`${analysis.category}\` (confidence ${Number(analysis.confidence ?? 0).toFixed(2)})`,
            '',
            String(analysis.root_cause ?? ''),
            '',
            analysis.recommended_action ? `_${analysis.recommended_action}_` : '',
          ].join('\n'),
        );
        item.command = { command: 'aiqa.showTrace', title: 'Trace', arguments: [run.id] };
        items.push(item);
      }
    }
    return items.length ? items : [note('Failures recorded, but not yet analysed', 'question')];
  }
}

// =========================================================================== //
/** Every repair the platform proposed, and whether it survived verification. */
export class HealingProvider extends BaseProvider {
  protected async load(): Promise<vscode.TreeItem[]> {
    const metrics = await this.api.metrics(30);
    const healing = (metrics.self_healing ?? {}) as Record<string, any>;
    const items: vscode.TreeItem[] = healing.proposed
      ? [
          metric('Proposed', healing.proposed ?? 0, 'lightbulb'),
          metric('Applied', healing.applied ?? 0, 'check'),
          metric('Verified', healing.verified ?? 0, 'pass-filled', 'Re-ran and passed'),
          metric('Reverted', healing.reverted ?? 0, 'discard', 'Did not fix the failure; rolled back'),
          metric('Success rate', `${healing.success_rate_pct ?? 0}%`, 'graph'),
          ...Object.entries((healing.by_strategy ?? {}) as Record<string, number>).map(([strategy, count]) =>
            metric(strategy.replace(/_/g, ' '), count, 'circle-small'),
          ),
        ]
      : [note('No repairs proposed yet', 'wrench')];

    items.push(...(await this.health()));
    return items;
  }

  /**
   * How trustworthy the suite is.
   *
   * A consistently failing test is shown as `broken`, never as a quarantine
   * candidate: it is reporting something, and hiding it is how a defect ships
   * behind a green pipeline.
   */
  private async health(): Promise<vscode.TreeItem[]> {
    const id = projectId();
    if (!id) {
      return [];
    }
    let report: Record<string, any>;
    try {
      report = await this.api.suiteHealth(id);
    } catch {
      return [];
    }
    if (!report.tests_tracked) {
      return [];
    }

    const byVerdict = (report.by_verdict ?? {}) as Record<string, number>;
    const items: vscode.TreeItem[] = [
      metric('Suite health', `${report.health_score ?? 0}%`, 'pulse', report.summary ?? ''),
    ];
    if (byVerdict.broken) {
      items.push(
        metric(
          'Never passing',
          byVerdict.broken,
          'error',
          'These are reporting something. They are never quarantined automatically.',
        ),
      );
    }
    const candidates = (report.quarantine_candidates ?? []) as Record<string, any>[];
    if (candidates.length) {
      const item = new vscode.TreeItem(
        `${candidates.length} quarantine candidate${candidates.length === 1 ? '' : 's'}`,
        vscode.TreeItemCollapsibleState.None,
      );
      item.iconPath = new vscode.ThemeIcon('warning');
      item.description = 'intermittent';
      item.tooltip = candidates.map((c) => `${c.test_name || c.test_id}: ${c.reason}`).join('\n');
      item.command = { command: 'aiqa.showSuiteHealth', title: 'Review suite health' };
      items.push(item);
    }
    items.push(action('Show suite health', 'aiqa.showSuiteHealth', 'checklist'));
    return items;
  }
}

// =========================================================================== //
/** Live agent activity for whatever is running now. */
export class ActivityProvider extends BaseProvider {
  protected async load(): Promise<vscode.TreeItem[]> {
    const runs = await this.api.listRuns(projectId() || undefined, 5);
    const active = runs.find((r) => r.status === 'running' || r.status === 'waiting_approval');
    const target = active ?? runs[0];
    if (!target) {
      return [note('No activity yet', 'pulse')];
    }

    const detail = await this.api.getRun(target.id);
    const header = new vscode.TreeItem(
      detail.instruction.slice(0, 60),
      vscode.TreeItemCollapsibleState.None,
    );
    header.description = detail.status;
    header.iconPath = new vscode.ThemeIcon(detail.status === 'running' ? 'sync~spin' : 'history');

    const steps = detail.traces.map((trace) => {
      const item = new vscode.TreeItem(trace.agent.replace(/_/g, ' '), vscode.TreeItemCollapsibleState.None);
      item.description = [
        trace.status,
        trace.model || '',
        trace.total_tokens ? `${trace.total_tokens.toLocaleString()} tok` : '',
        trace.cost_usd ? `$${trace.cost_usd.toFixed(5)}` : '',
        `${trace.latency_ms} ms`,
      ]
        .filter(Boolean)
        .join(' · ');
      item.iconPath = new vscode.ThemeIcon(
        trace.status === 'succeeded' ? 'pass' : trace.status === 'failed' ? 'error' : 'circle-outline',
      );
      item.tooltip = trace.output_summary || trace.error || trace.input_summary;
      return item;
    });
    return [header, ...steps];
  }
}

// =========================================================================== //
/** What the platform knows — the thing that makes later runs cheap. */
export class KnowledgeProvider extends BaseProvider {
  protected async load(): Promise<vscode.TreeItem[]> {
    const id = projectId();
    if (!id) {
      return [action('Register this workspace first', 'aiqa.registerProject', 'add')];
    }
    const knowledge = await this.api.projectKnowledge(id);
    const repo = knowledge.repository_map as Record<string, any> | null;
    const app = knowledge.application_map as Record<string, any> | null;
    const tests = (knowledge.test_knowledge ?? {}) as Record<string, any>;
    const graph = (knowledge.knowledge_graph ?? {}) as Record<string, any>;

    const items: vscode.TreeItem[] = [];
    if (repo) {
      items.push(
        metric('Repository files', repo.files ?? 0, 'files', `indexed at commit ${repo.git_commit ?? ''}`),
        metric('Page objects', repo.pages ?? 0, 'symbol-class'),
        metric('Fixtures', repo.fixtures ?? 0, 'symbol-method'),
        metric('Step definitions', repo.steps ?? 0, 'symbol-event'),
      );
    } else {
      items.push(action('Repository not indexed — index now', 'aiqa.indexRepository', 'warning'));
    }

    if (app) {
      items.push(
        metric('Application routes', app.pages ?? 0, 'browser'),
        metric(
          'Trusted locators',
          `${app.trusted_locators ?? 0} of ${app.locators ?? 0}`,
          'target',
          `average confidence ${app.avg_confidence ?? 0}`,
        ),
        metric('Components', app.components ?? 0, 'symbol-structure'),
      );
    } else {
      items.push(action('Application not explored — explore now', 'aiqa.exploreApplication', 'browser'));
    }

    items.push(
      metric('Tests remembered', tests.tests_known ?? 0, 'beaker', 'Reused instead of regenerated'),
      metric('Knowledge graph', `${graph.nodes ?? 0} nodes`, 'type-hierarchy'),
      metric('Requirement coverage', `${graph.coverage_pct ?? 0}%`, 'checklist'),
    );

    const routes = (knowledge.known_routes ?? []) as string[];
    if (routes.length) {
      const item = new vscode.TreeItem('Known routes', vscode.TreeItemCollapsibleState.None);
      item.description = routes.slice(0, 4).join(', ') + (routes.length > 4 ? '…' : '');
      item.iconPath = new vscode.ThemeIcon('list-tree');
      item.tooltip = routes.join('\n');
      items.push(item);
    }
    items.push(...(await this.coverage(id)));
    items.push(action('Show full knowledge report', 'aiqa.showKnowledge', 'output'));
    return items;
  }

  /**
   * Untested surface, as actionable rows.
   *
   * Each gap carries the instruction that would close it, so clicking one
   * starts that run rather than leaving the engineer to translate a percentage
   * into a plan.
   */
  private async coverage(id: string): Promise<vscode.TreeItem[]> {
    let report: Record<string, any>;
    try {
      report = await this.api.projectCoverage(id);
    } catch {
      // Coverage is a nice-to-have on this panel; a control plane that does not
      // serve it yet must not blank out everything above.
      return [];
    }
    const gaps = (report.gaps ?? []) as Record<string, any>[];
    const routes = (report.routes ?? {}) as Record<string, any>;
    const items: vscode.TreeItem[] = [
      metric(
        'Routes with a test',
        `${routes.covered ?? 0} of ${routes.total ?? 0}`,
        'checklist',
        'A route with a test touching it — not a claim that it is well tested.',
      ),
    ];
    if (!gaps.length) {
      return items;
    }

    const high = gaps.filter((g) => g.severity === 'high').length;
    const header = new vscode.TreeItem(
      `${gaps.length} coverage gap${gaps.length === 1 ? '' : 's'}`,
      vscode.TreeItemCollapsibleState.None,
    );
    header.description = high ? `${high} high severity` : '';
    header.iconPath = new vscode.ThemeIcon(high ? 'warning' : 'info');
    header.tooltip = report.summary ?? '';
    items.push(header);

    for (const gap of gaps.slice(0, 6)) {
      const item = new vscode.TreeItem(String(gap.label ?? gap.key), vscode.TreeItemCollapsibleState.None);
      item.description = `${gap.severity} · ${gap.kind}`;
      item.iconPath = new vscode.ThemeIcon(gap.severity === 'high' ? 'error' : 'circle-outline');
      item.tooltip = `${gap.reason}\n\nClick to run: ${gap.suggested_instruction}`;
      item.command = {
        command: 'aiqa.automateInstruction',
        title: 'Close this gap',
        arguments: [gap.suggested_instruction],
      };
      items.push(item);
    }
    return items;
  }
}

// =========================================================================== //
/** The layered standards actually in force for this repository. */
export class StandardsProvider extends BaseProvider {
  protected async load(): Promise<vscode.TreeItem[]> {
    const id = projectId();
    if (!id) {
      return [action('Register this workspace first', 'aiqa.registerProject', 'add')];
    }
    const standards = await this.api.projectStandards(id);
    const rules = (standards.standards?.rules ?? []) as Record<string, any>[];
    const errors = rules.filter((r) => r.severity === 'error' || r.severity === 'critical');

    return [
      metric('Organization', String(standards.organization || 'default'), 'organization'),
      metric('Rules in force', rules.length, 'law'),
      metric('Blocking rules', errors.length, 'error'),
      metric('Source', String(standards.source ?? '').split(/[\\/]/).slice(-2).join('/'), 'file-symlink-file'),
      action('Validate the suite against these', 'aiqa.validateStandards', 'check-all'),
      action('Initialise .aiqa standards', 'aiqa.initStandards', 'new-folder'),
    ];
  }
}

// =========================================================================== //
/** Spend, with the unit economics a lead is asked about. */
export class CostsProvider extends BaseProvider {
  protected async load(): Promise<vscode.TreeItem[]> {
    const [cost, savings] = await Promise.all([this.api.costMetrics(30), this.api.savingsMetrics(30)]);
    const totals = (cost.totals ?? {}) as Record<string, any>;
    const unit = (cost.unit_economics ?? {}) as Record<string, any>;

    return [
      metric('Spend (30d)', `$${Number(totals.total_cost_usd ?? 0).toFixed(4)}`, 'credit-card'),
      metric('Cost per scenario', `$${Number(unit.cost_per_scenario_usd ?? 0).toFixed(5)}`, 'symbol-numeric'),
      metric('Requests per scenario', unit.requests_per_scenario ?? 0, 'arrow-swap'),
      metric('Tokens per scenario', Number(unit.tokens_per_scenario ?? 0).toLocaleString(), 'symbol-string'),
      metric(
        'Free model share',
        `${totals.free_call_share_pct ?? 0}%`,
        'heart',
        'Share of calls served by free or local models',
      ),
      metric(
        'Context avoided',
        Number(savings.context_tokens_avoided ?? 0).toLocaleString(),
        'shield',
        'Tokens never sent, because the knowledge layer already had the answer',
      ),
      metric('Repo cache hits', `${savings.repository_cache_hit_rate_pct ?? 0}%`, 'database'),
      metric('App map hits', `${savings.application_map_hit_rate_pct ?? 0}%`, 'browser'),
      metric('Duplicates avoided', savings.duplicate_scenarios_avoided ?? 0, 'copy'),
      action('Show full cost report', 'aiqa.showCost', 'graph'),
    ];
  }
}

// =========================================================================== //
/** Reports produced by finished runs. */
export class ReportsProvider extends BaseProvider {
  protected async load(): Promise<vscode.TreeItem[]> {
    const runs = await this.api.listRuns(projectId() || undefined, 15);
    const finished = runs.filter((r) => r.status === 'succeeded' || r.status === 'failed');
    if (!finished.length) {
      return [note('No reports yet', 'file-text')];
    }
    return finished.slice(0, 10).map((run) => {
      const item = new vscode.TreeItem(run.instruction.slice(0, 60), vscode.TreeItemCollapsibleState.None);
      item.description = `${run.tests_passed}/${run.tests_total} · $${run.total_cost_usd.toFixed(4)}`;
      item.iconPath = new vscode.ThemeIcon(run.status === 'succeeded' ? 'file-text' : 'warning');
      item.command = { command: 'aiqa.showReport', title: 'Report', arguments: [run.id] };
      return item;
    });
  }
}

// =========================================================================== //
/** Connection, routing and configuration at a glance. */
export class ConfigurationProvider extends BaseProvider {
  protected async load(): Promise<vscode.TreeItem[]> {
    const config = vscode.workspace.getConfiguration('aiqa');
    const health = await this.api.health();
    const routes = health.active_routes ?? {};
    const offline = Object.values(routes).every(
      (route) => !route || route.provider === 'mock' || route.provider === 'hashing',
    );

    const items: vscode.TreeItem[] = [
      metric('Control plane', this.api.baseUrl, offline ? 'warning' : 'server', `v${health.version}`),
      metric('Project', config.get<string>('projectId', '') || 'not bound', 'folder'),
      metric('Default mode', config.get<string>('defaultMode', 'full'), 'settings-gear'),
      metric('Max cost / run', `$${config.get<number>('maxCostUsd', 2)}`, 'credit-card'),
      metric('Auto-approve', config.get<boolean>('autoApprove', false) ? 'ON — no human gates' : 'off', 'person'),
    ];

    for (const [tier, route] of Object.entries(routes)) {
      if (tier === 'fast') {
        continue; // v1 alias of `cheap`
      }
      items.push(
        metric(
          `Route: ${tier}`,
          route ? `${route.provider}/${route.model}${route.free ? ' (free)' : ''}` : 'none',
          route?.free ? 'heart' : 'zap',
        ),
      );
    }
    if (offline) {
      items.push(
        note('Offline mode — no LLM reachable', 'warning', 'Start Ollama or set a provider key'),
      );
    }
    items.push(
      action('Check connection and providers', 'aiqa.doctor', 'debug-disconnect'),
      action('Show agent pipeline', 'aiqa.showPipeline', 'type-hierarchy'),
      action('Open the dashboard', 'aiqa.openDashboard', 'globe'),
    );
    return items;
  }
}
