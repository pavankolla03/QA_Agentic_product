# Documentation

- **[Getting started](GETTING_STARTED.md)** — clone to first generated suite in ~10 minutes
- **[Architecture](ARCHITECTURE.md)** — why the system is built this way, and how to extend it
- **[../README.md](../README.md)** — overview, safety model, deployment

## Reference

| Topic | Where |
|---|---|
| Model routing and pricing | `configs/models.yaml` |
| Organization QA standards | `configs/standards.yaml` |
| Security policy (paths, commands, redaction, RBAC) | `configs/security.yaml` |
| Project-level standards override | `<your-repo>/.aiqa/standards.yaml` |
| Environment variables | `.env.example` |
| HTTP API | <http://127.0.0.1:8080/docs> when the server is running |
| CLI | `python -m services.api_gateway.cli --help` |
| Build progress ledger | `../PROGRESS.md` |
