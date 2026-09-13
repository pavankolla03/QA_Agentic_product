# Documentation

- **[Getting started](GETTING_STARTED.md)** — clone to first generated suite in ~10 minutes
- **[Cost optimization](cost-optimization.md)** — how spend is controlled, measured and tuned
- **[Architecture](ARCHITECTURE.md)** — why the system is built this way, and how to extend it
- **[Roadmap](ROADMAP.md)** — what comes next, and what will not be automated
- **[../README.md](../README.md)** — overview and safety model

## Reference

| Topic | Where |
|---|---|
| Model tiers, routing and pricing | `configs/models.yaml` |
| Run budgets and loop caps | `configs/models.yaml` → `run_budget` |
| Organization QA standards | `configs/standards.yaml` |
| Security policy (paths, commands, redaction, RBAC) | `configs/security.yaml` |
| Project standards, examples, generated maps | `<your-repo>/.aiqa/` |
| Agent permissions | `packages/agent_protocol/permissions.py`, or `aiqa permissions` |
| Agent pipeline diagram | `aiqa pipeline` (generated from the compiled graph) |
| Environment variables | `.env.example` |
| HTTP API | <http://127.0.0.1:8080/docs> when the server is running |
| CLI | `aiqa --help` |
| Build progress ledger | `../PROGRESS.md` |

## Commands worth knowing

```bash
aiqa doctor                      # providers reachable, how each tier is routed
aiqa standards show              # the merged standards actually in force
aiqa standards parse "..."       # will this prose become an enforced rule?
aiqa knowledge show <project>    # what the platform already knows
aiqa cost report                 # spend, unit economics, what caching avoided
aiqa cost management             # coverage, pass rate, healing accuracy
aiqa permissions                 # least-privilege matrix
aiqa pipeline                    # agent graph as Mermaid
aiqa benchmark --runs 5          # measure the knowledge effect
```
