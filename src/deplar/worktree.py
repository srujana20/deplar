"""Multi-repo git-worktree checkout.

Given a target repo, resolve the set of affected repos (the target plus the
repos that depend on it) and check each one out as a git worktree into a single
workspace directory — so an agent can make coordinated, parallel edits across
every repo a change touches, all in one place.
"""
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List

from deplar.graph.symbol_store import SymbolStore


@dataclass
class WorktreeResult:
    repo: str
    source_path: str
    worktree_path: str
    status: str          # created | exists | skipped | error
    detail: str = ""


def _git(repo_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True, text=True,
    )


def _is_git_repo(path: Path) -> bool:
    if not path.exists():
        return False
    result = _git(path, "rev-parse", "--is-inside-work-tree")
    return result.returncode == 0 and result.stdout.strip() == "true"


class WorktreeManager:
    def __init__(self, store: SymbolStore):
        self.store = store

    def affected_repos(
        self, target: str, transitive: bool = False, depth: int = 3,
        include_dependencies: bool = False,
    ) -> List[str]:
        """The target plus every repo affected by changing it.

        By default this is the target's direct dependents (repos that call it).
        `transitive=True` walks the full blast radius. `include_dependencies`
        additionally pulls in repos the target itself calls.
        """
        repos: List[str] = [target]
        seen = {target}

        if transitive:
            affected = self.store.blast_radius(target, depth=depth)
        else:
            affected = [d["repo"] for d in self.store.get_dependents(target)]

        for r in affected:
            if r not in seen:
                repos.append(r)
                seen.add(r)

        if include_dependencies:
            for d in self.store.get_dependencies(target):
                if d["repo"] not in seen:
                    repos.append(d["repo"])
                    seen.add(d["repo"])

        return repos

    def checkout(
        self, target: str, workspace: Path, branch: str,
        transitive: bool = False, depth: int = 3,
        include_dependencies: bool = False,
    ) -> List[WorktreeResult]:
        workspace = Path(workspace).resolve()
        workspace.mkdir(parents=True, exist_ok=True)

        results: List[WorktreeResult] = []
        for repo in self.affected_repos(
            target, transitive=transitive, depth=depth,
            include_dependencies=include_dependencies,
        ):
            results.append(self._add_worktree(repo, workspace, branch))
        return results

    def _add_worktree(
        self, repo: str, workspace: Path, branch: str
    ) -> WorktreeResult:
        src = self.store.repo_path(repo)
        dest = workspace / repo

        if src is None:
            return WorktreeResult(repo, "", str(dest), "skipped",
                                  "no local path on record (scan the org first)")

        src_path = Path(src)
        if not _is_git_repo(src_path):
            return WorktreeResult(repo, src, str(dest), "skipped",
                                  f"{src} is not a git repository")

        if dest.exists():
            return WorktreeResult(repo, src, str(dest), "exists",
                                  "worktree path already present")

        # Reuse the branch if it already exists in this repo, else create it.
        branch_exists = _git(
            src_path, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"
        ).returncode == 0

        if branch_exists:
            result = _git(src_path, "worktree", "add", str(dest), branch)
        else:
            result = _git(src_path, "worktree", "add", "-b", branch, str(dest))

        if result.returncode != 0:
            return WorktreeResult(repo, src, str(dest), "error",
                                  result.stderr.strip())

        return WorktreeResult(repo, src, str(dest), "created",
                              f"branch {branch}")

    def remove(self, workspace: Path) -> List[WorktreeResult]:
        """Tear down every worktree previously created under `workspace`."""
        workspace = Path(workspace).resolve()
        results: List[WorktreeResult] = []
        for repo in self.store.list_repos():
            dest = workspace / repo["name"]
            if not dest.exists():
                continue
            src = repo["path"]
            result = _git(Path(src), "worktree", "remove", "--force", str(dest))
            results.append(WorktreeResult(
                repo["name"], src, str(dest),
                "removed" if result.returncode == 0 else "error",
                result.stderr.strip(),
            ))
        return results
