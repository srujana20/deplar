"""LLM-driven agent loops over the deplar knowledge graph.

Two agents, both built on a small manual tool-use loop (see the Claude API
tool-use docs):

- ImpactAgent: takes a natural-language proposed change, calls the graph tools
  to work out the blast radius, and writes a structured impact report before any
  code is touched.
- Planner + Validator (dual-agent): the planner drafts a coordinated change plan
  across the affected repos (read-only — it does not edit code); the validator
  re-queries the graph to check coverage and runs each repo's tests, then returns
  a verdict.

The Anthropic client is injected so the loop is unit-testable with a stub — no
API key needed for tests. At runtime, `default_client()` builds a real client
(requires the `anthropic` package + ANTHROPIC_API_KEY).
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from deplar.graph.symbol_store import SymbolStore

MODEL = "claude-opus-4-8"


def default_client():
    """Build a real Anthropic client, with a friendly error if unavailable."""
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError(
            "The agent commands need the Anthropic SDK. Install with "
            "`pip install deplar[agent]` (or `pip install anthropic`)."
        ) from e
    try:
        return anthropic.Anthropic()
    except Exception as e:
        raise RuntimeError(
            f"Could not initialize the Anthropic client ({e}). "
            "Set ANTHROPIC_API_KEY (or run `ant auth login`)."
        ) from e


# --- graph tools exposed to the agent ---

def _graph_tools(store: SymbolStore) -> tuple[list[dict], dict]:
    """Return (tool schemas, name -> implementation) for read-only graph access."""
    schemas = [
        {
            "name": "get_dependents",
            "description": "Repos that call `repo` (who breaks if its contract changes).",
            "input_schema": {
                "type": "object",
                "properties": {"repo": {"type": "string"}},
                "required": ["repo"],
            },
        },
        {
            "name": "get_dependencies",
            "description": "Repos/services that `repo` calls (its outbound dependencies).",
            "input_schema": {
                "type": "object",
                "properties": {"repo": {"type": "string"}},
                "required": ["repo"],
            },
        },
        {
            "name": "blast_radius",
            "description": "Transitive set of repos affected by a change to `repo`.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "depth": {"type": "integer"},
                },
                "required": ["repo"],
            },
        },
        {
            "name": "search_symbols",
            "description": "Find classes/functions/methods by name; returns file + line + signature.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "repo": {"type": "string"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_callers",
            "description": "Call sites of a symbol across repos (file + line + enclosing caller).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol_name": {"type": "string"},
                    "repo": {"type": "string"},
                },
                "required": ["symbol_name"],
            },
        },
        {
            "name": "list_repos",
            "description": "List every repo in the knowledge graph.",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]

    impls = {
        "get_dependents": lambda repo: store.get_dependents(repo),
        "get_dependencies": lambda repo: store.get_dependencies(repo),
        "blast_radius": lambda repo, depth=3: store.blast_radius(repo, depth=depth),
        "search_symbols": lambda query, repo="": store.search_symbols(query, repo=repo or None),
        "get_callers": lambda symbol_name, repo="": store.get_callers(symbol_name, repo=repo or None),
        "list_repos": lambda: store.list_repos(),
    }
    return schemas, impls


# --- generic tool-use loop ---

@dataclass
class AgentRun:
    text: str
    iterations: int
    tool_calls: List[str] = field(default_factory=list)
    refused: bool = False


def run_loop(client, system: str, user: str, tools: list[dict],
             impls: dict, model: str = MODEL, effort: str = "high",
             max_iterations: int = 12,
             on_event: Optional[Callable[[str], None]] = None) -> AgentRun:
    """Manual agentic loop: call → run tools → feed results → repeat to end_turn."""
    messages = [{"role": "user", "content": user}]
    tool_calls: List[str] = []

    for i in range(1, max_iterations + 1):
        resp = client.messages.create(
            model=model,
            max_tokens=16000,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            tools=tools,
            messages=messages,
        )

        if resp.stop_reason == "refusal":
            return AgentRun("", i, tool_calls, refused=True)

        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if not tool_uses or resp.stop_reason == "end_turn":
            text = "".join(b.text for b in resp.content
                           if getattr(b, "type", None) == "text")
            return AgentRun(text.strip(), i, tool_calls)

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for tu in tool_uses:
            tool_calls.append(tu.name)
            if on_event:
                on_event(f"{tu.name}({json.dumps(tu.input)})")
            try:
                out = impls[tu.name](**tu.input)
                content = json.dumps(out)
                is_error = False
            except Exception as e:
                content = f"error: {e}"
                is_error = True
            results.append({
                "type": "tool_result", "tool_use_id": tu.id,
                "content": content, "is_error": is_error,
            })
        messages.append({"role": "user", "content": results})

    return AgentRun("(reached iteration limit without finishing)",
                    max_iterations, tool_calls)


# --- impact analysis agent ---

IMPACT_SYSTEM = """You are an impact-analysis agent for a multi-repo codebase.
A developer describes a change they intend to make. BEFORE any code is written,
use the tools to determine exactly what the change will affect.

Work systematically:
1. Identify the target repo and any specific symbols named in the change.
2. Use search_symbols / get_callers to find where those symbols live and who
   calls them across repos (with file + line numbers).
3. Use get_dependents and blast_radius to find repos that must be updated
   together.

Then produce a concise structured impact report in markdown with these sections:
`## Directly affected`, `## Transitive blast radius`, `## Cross-repo call sites`
(with file:line), `## Risk` (one line), and `## Recommended next step` (a
`deplar workspace <repo>` command). Do not propose code edits — only impact."""


class ImpactAgent:
    def __init__(self, store: SymbolStore, client=None, model: str = MODEL):
        self.store = store
        self.client = client or default_client()
        self.model = model

    def run(self, change: str, on_event=None) -> AgentRun:
        tools, impls = _graph_tools(self.store)
        return run_loop(
            self.client, IMPACT_SYSTEM,
            f"Proposed change:\n{change}",
            tools, impls, model=self.model, on_event=on_event,
        )


# --- planner + validator (dual-agent) ---

PLANNER_SYSTEM = """You are the PLANNER in a planner/validator pair working on a
multi-repo change. Use the tools to understand the blast radius, then produce a
COORDINATED CHANGE PLAN in markdown: for each affected repo, list the specific
files/symbols to change and what the change is, in the order they should be
applied. Do NOT write code — produce the plan only. End with an explicit list of
every repo that must be edited."""

VALIDATOR_SYSTEM = """You are the VALIDATOR in a planner/validator pair. A change
plan has been executed in a git-worktree workspace. Your job:
1. Use affected_repos / list_repos to confirm every repo that should have been
   touched is present in the workspace (coverage check).
2. Call run_tests to run each repo's test suite across the workspace.
Then return a verdict in markdown: `## Coverage` (any missing repos?),
`## Tests` (per-repo pass/fail), and `## Verdict` (APPROVED or CHANGES NEEDED,
with the reason)."""


def _validator_tools(store: SymbolStore, workspace: Path,
                     test_cmd: Optional[str]) -> tuple[list[dict], dict]:
    from deplar.validator import WorkspaceValidator
    from deplar.worktree import WorktreeManager

    schemas = [
        {
            "name": "affected_repos",
            "description": "Repos that must be edited together for a change to `target`.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "transitive": {"type": "boolean"},
                },
                "required": ["target"],
            },
        },
        {
            "name": "list_repos",
            "description": "List every repo in the knowledge graph.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "run_tests",
            "description": "Run every repo's test suite across the coordinated workspace.",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]

    def run_tests():
        result = WorkspaceValidator().validate(workspace, test_cmd=test_cmd)
        return [{"repo": r.repo, "passed": r.passed, "skipped": r.skipped,
                 "detail": r.detail} for r in result.repos]

    impls = {
        "affected_repos": lambda target, transitive=False:
            WorktreeManager(store).affected_repos(target, transitive=transitive),
        "list_repos": lambda: store.list_repos(),
        "run_tests": run_tests,
    }
    return schemas, impls


@dataclass
class ValidationRun:
    plan: str
    verdict: str
    planner_iterations: int
    validator_iterations: int


class PlannerValidator:
    def __init__(self, store: SymbolStore, client=None, model: str = MODEL):
        self.store = store
        self.client = client or default_client()
        self.model = model

    def plan(self, change: str, on_event=None) -> AgentRun:
        tools, impls = _graph_tools(self.store)
        return run_loop(self.client, PLANNER_SYSTEM,
                        f"Change to plan:\n{change}", tools, impls,
                        model=self.model, on_event=on_event)

    def validate(self, change: str, workspace: Path,
                 test_cmd: Optional[str] = None, on_event=None) -> AgentRun:
        tools, impls = _validator_tools(self.store, workspace, test_cmd)
        return run_loop(
            self.client, VALIDATOR_SYSTEM,
            f"The following change was executed in the workspace at "
            f"{workspace}:\n{change}\n\nValidate it.",
            tools, impls, model=self.model, on_event=on_event,
        )

    def run(self, change: str, workspace: Path,
            test_cmd: Optional[str] = None, on_event=None) -> ValidationRun:
        planned = self.plan(change, on_event=on_event)
        validated = self.validate(change, workspace, test_cmd=test_cmd,
                                  on_event=on_event)
        return ValidationRun(planned.text, validated.text,
                             planned.iterations, validated.iterations)
