"""Agent permissions — least privilege, per agent.

No agent gets unrestricted access. The Repository Agent can read files; only the
Code Generation and Self-Healing agents can write them; only the Execution Agent
can run tests. This is enforced at the tool-registry boundary, so an agent that
tries to use a tool it was not granted is refused and audited — it does not
matter whether the attempt came from a bug, a bad prompt, or an injection buried
in a test fixture the agent was reading.

The grant is a capability set, not a tool list, so adding a tool to a category
does not silently widen anyone's access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Capability(str, Enum):
    """What an agent is allowed to do."""

    READ_FILES = "read_files"
    WRITE_FILES = "write_files"
    DELETE_FILES = "delete_files"
    SEARCH_FILES = "search_files"
    READ_GIT = "read_git"
    CREATE_BRANCH = "create_branch"
    COMMIT = "commit"
    PUSH = "push"
    RUN_TESTS = "run_tests"
    RUN_COMMANDS = "run_commands"
    EXPLORE_APP = "explore_app"
    CALL_API = "call_api"
    QUERY_DB = "query_db"
    MOBILE = "mobile"
    READ_ISSUES = "read_issues"
    WRITE_ISSUES = "write_issues"
    NOTIFY = "notify"


#: tool name -> capability required to invoke it.
TOOL_CAPABILITY: dict[str, Capability] = {
    "fs.read_file": Capability.READ_FILES,
    "fs.exists": Capability.READ_FILES,
    "fs.list_dir": Capability.READ_FILES,
    "fs.search": Capability.SEARCH_FILES,
    "fs.write_file": Capability.WRITE_FILES,
    "fs.apply_changes": Capability.WRITE_FILES,
    "fs.delete_file": Capability.DELETE_FILES,
    "git.status": Capability.READ_GIT,
    "git.diff": Capability.READ_GIT,
    "git.log": Capability.READ_GIT,
    "git.branch": Capability.CREATE_BRANCH,
    "git.commit": Capability.COMMIT,
    "git.push": Capability.PUSH,
    "git.checkout_file": Capability.WRITE_FILES,
    "playwright.explore": Capability.EXPLORE_APP,
    "playwright.run_tests": Capability.RUN_TESTS,
    "playwright.install": Capability.RUN_COMMANDS,
    "playwright.check_setup": Capability.READ_FILES,
    "shell.run": Capability.RUN_COMMANDS,
    "shell.which": Capability.READ_FILES,
    "api.request": Capability.CALL_API,
    "api.health": Capability.CALL_API,
    "api.import_openapi": Capability.CALL_API,
    "db.query": Capability.QUERY_DB,
    "db.row_exists": Capability.QUERY_DB,
    "db.schema": Capability.QUERY_DB,
    "mobile.capabilities": Capability.MOBILE,
    "mobile.probe": Capability.MOBILE,
    "mobile.scaffold": Capability.MOBILE,
    "jira.fetch_issue": Capability.READ_ISSUES,
    "jira.create_defect": Capability.WRITE_ISSUES,
    "slack.notify": Capability.NOTIFY,
    "teams.notify": Capability.NOTIFY,
}


@dataclass(frozen=True)
class AgentPermissions:
    agent: str
    capabilities: frozenset[Capability] = field(default_factory=frozenset)

    def allows(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def allows_tool(self, tool_name: str) -> bool:
        required = TOOL_CAPABILITY.get(tool_name)
        if required is None:
            # Unknown tool: deny. New capabilities must be granted deliberately.
            return False
        return self.allows(required)

    def tool_names(self) -> list[str]:
        return sorted(name for name, cap in TOOL_CAPABILITY.items() if cap in self.capabilities)


def _perm(agent: str, *capabilities: Capability) -> AgentPermissions:
    return AgentPermissions(agent=agent, capabilities=frozenset(capabilities))


#: The grant table. Deliberately narrow — widen it only with a reason.
AGENT_PERMISSIONS: dict[str, AgentPermissions] = {
    "orchestrator": _perm("orchestrator", Capability.READ_FILES),
    # Requirements may come from a tracker, but the agent cannot write to one.
    "requirement": _perm("requirement", Capability.READ_FILES, Capability.READ_ISSUES),
    # Reads the repository; must never modify it.
    "repository": _perm("repository", Capability.READ_FILES, Capability.SEARCH_FILES, Capability.READ_GIT),
    # Drives a browser and probes the app; no filesystem writes.
    "exploration": _perm("exploration", Capability.READ_FILES, Capability.EXPLORE_APP, Capability.CALL_API),
    # Pure reasoning over knowledge already gathered.
    "test_design": _perm("test_design", Capability.READ_FILES, Capability.SEARCH_FILES, Capability.QUERY_DB),
    # Produces a change set in memory; the Execution Agent applies it.
    "code_generation": _perm("code_generation", Capability.READ_FILES, Capability.SEARCH_FILES),
    "standards": _perm("standards", Capability.READ_FILES, Capability.SEARCH_FILES, Capability.RUN_COMMANDS),
    # The only agent that may write the workspace, branch, commit and run tests.
    "execution": _perm(
        "execution",
        Capability.READ_FILES, Capability.WRITE_FILES, Capability.RUN_TESTS,
        Capability.RUN_COMMANDS, Capability.READ_GIT, Capability.CREATE_BRANCH, Capability.COMMIT,
    ),
    # Reads evidence only. Raising a defect is a separate, approved action.
    "failure_analysis": _perm(
        "failure_analysis", Capability.READ_FILES, Capability.SEARCH_FILES, Capability.READ_GIT
    ),
    # May edit and re-run, but never commit or push its own repairs.
    "self_healing": _perm(
        "self_healing",
        Capability.READ_FILES, Capability.WRITE_FILES, Capability.RUN_TESTS, Capability.READ_GIT,
    ),
    "reporting": _perm("reporting", Capability.READ_FILES, Capability.NOTIFY, Capability.WRITE_ISSUES),
}

#: `push` is granted to nobody by default — it requires an explicit human action.
assert not any(
    Capability.PUSH in permission.capabilities for permission in AGENT_PERMISSIONS.values()
), "no agent may hold PUSH by default"


def permissions_for(agent: str) -> AgentPermissions:
    return AGENT_PERMISSIONS.get(agent, _perm(agent, Capability.READ_FILES))


def describe() -> list[dict[str, object]]:
    """Permission matrix for the dashboard and docs."""
    return [
        {
            "agent": name,
            "capabilities": sorted(c.value for c in permission.capabilities),
            "tools": permission.tool_names(),
        }
        for name, permission in sorted(AGENT_PERMISSIONS.items())
    ]
