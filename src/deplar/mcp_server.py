"""deplar MCP server.

Exposes the dependency + symbol knowledge graph to agents over MCP so they can
ask "what calls this?", "what breaks if I change it?", and "where is this
symbol defined?" before touching any code.

Run with:  deplar mcp --db deplar.db
The DB path can also be set via the DEPLAR_DB environment variable.
"""
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from deplar.graph.symbol_store import SymbolStore
from deplar.impact import ImpactAnalyzer
from deplar.skill import read_skill, read_skill_index
from deplar.worktree import WorktreeManager

mcp = FastMCP("deplar")


def _store() -> SymbolStore:
    db = os.environ.get("DEPLAR_DB", "deplar.db")
    return SymbolStore(Path(db))


def _skills_dir() -> Path:
    return Path(os.environ.get("DEPLAR_SKILLS", "skillhub"))


@mcp.tool()
def get_dependencies(repo: str) -> list[dict]:
    """List the services/repos that `repo` calls (its outbound dependencies)."""
    store = _store()
    try:
        return store.get_dependencies(repo)
    finally:
        store.close()


@mcp.tool()
def get_dependents(repo: str) -> list[dict]:
    """List the repos that call `repo` (who breaks if its contract changes)."""
    store = _store()
    try:
        return store.get_dependents(repo)
    finally:
        store.close()


@mcp.tool()
def blast_radius(repo: str, depth: int = 3) -> list[str]:
    """Transitive set of repos affected by a change to `repo`."""
    store = _store()
    try:
        return store.blast_radius(repo, depth=depth)
    finally:
        store.close()


@mcp.tool()
def search_symbols(query: str, repo: str = "", limit: int = 50) -> list[dict]:
    """Find classes/functions/methods whose name matches `query`.

    Returns each match with its file, kind, signature and line range.
    """
    store = _store()
    try:
        return store.search_symbols(query, repo=repo or None, limit=limit)
    finally:
        store.close()


@mcp.tool()
def get_callers(symbol_name: str, repo: str = "", limit: int = 100) -> list[dict]:
    """Find call sites of `symbol_name` (file + line + enclosing caller)."""
    store = _store()
    try:
        return store.get_callers(symbol_name, repo=repo or None, limit=limit)
    finally:
        store.close()


@mcp.tool()
def list_repos() -> list[dict]:
    """List every repo in the knowledge graph with its local path."""
    store = _store()
    try:
        return store.list_repos()
    finally:
        store.close()


@mcp.tool()
def affected_repos(target: str, transitive: bool = False,
                   include_dependencies: bool = False) -> list[str]:
    """Repos that must be edited together for a change to `target`.

    The target plus its dependents; set `transitive` for the full blast radius.
    Use this to decide which repos to pull into a coordinated workspace.
    """
    store = _store()
    try:
        return WorktreeManager(store).affected_repos(
            target, transitive=transitive,
            include_dependencies=include_dependencies,
        )
    finally:
        store.close()


@mcp.tool()
def impact_report(target: str, symbol: str = "", depth: int = 3) -> dict:
    """Structured impact report for a change to `target` (optionally a symbol).

    Returns dependents, transitive blast radius, emitted events, and cross-repo
    call sites — run this before editing to know what a change will ripple into.
    """
    store = _store()
    try:
        report = ImpactAnalyzer(store).analyze(target, symbol=symbol or None,
                                               depth=depth)
        return report.to_dict()
    finally:
        store.close()


@mcp.tool()
def remember(repo: str, note: str, kind: str = "note") -> dict:
    """Persist a learned pattern/convention/gotcha about `repo` across sessions.

    kind is one of: pattern, convention, gotcha, note.
    """
    store = _store()
    try:
        mem_id = store.remember(repo, note, kind=kind)
        return {"id": mem_id, "repo": repo, "kind": kind, "note": note}
    finally:
        store.close()


@mcp.tool()
def recall(repo: str, kind: str = "") -> list[dict]:
    """Recall everything learned about `repo` (optionally filtered by kind)."""
    store = _store()
    try:
        return store.recall(repo, kind=kind or None)
    finally:
        store.close()


@mcp.tool()
def list_skills() -> list[dict]:
    """List the skills available in the skillhub registry."""
    return read_skill_index(_skills_dir())


@mcp.tool()
def get_skill(repo: str) -> str:
    """Return the SKILL.md for `repo` — load it before working in that repo."""
    content = read_skill(_skills_dir(), repo)
    return content or f"No skill found for '{repo}'. Run `deplar skillhub` first."


def main():
    mcp.run()


if __name__ == "__main__":
    main()
