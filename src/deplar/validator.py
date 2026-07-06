"""Cross-repo workspace validator.

After coordinated edits across a multi-repo workspace, this re-runs each repo's
test suite and collects the results — the deterministic half of a planner/
validator loop. Test commands are auto-detected per repo (pytest, npm test,
mvn/gradle, go test) or overridden globally.
"""
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class RepoValidation:
    repo: str
    path: str
    command: str
    passed: bool
    skipped: bool = False
    detail: str = ""
    output_tail: str = ""


@dataclass
class ValidationResult:
    repos: List[RepoValidation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.passed or r.skipped for r in self.repos)


def detect_test_command(repo_path: Path) -> Optional[str]:
    """Best-effort detection of a repo's test command."""
    if (repo_path / "pytest.ini").exists() or (repo_path / "tests").is_dir() \
            or _pyproject_has_pytest(repo_path):
        return "pytest -q"
    if (repo_path / "package.json").exists():
        return "npm test --silent"
    if (repo_path / "pom.xml").exists():
        return "mvn -q test"
    if (repo_path / "build.gradle").exists() or (repo_path / "build.gradle.kts").exists():
        return "gradle test"
    if (repo_path / "go.mod").exists():
        return "go test ./..."
    return None


def _pyproject_has_pytest(repo_path: Path) -> bool:
    pyproject = repo_path / "pyproject.toml"
    if not pyproject.exists():
        return False
    try:
        return "pytest" in pyproject.read_text()
    except OSError:
        return False


class WorkspaceValidator:
    def __init__(self, timeout: int = 600):
        self.timeout = timeout

    def validate(self, workspace: Path, test_cmd: Optional[str] = None,
                 repos: Optional[List[str]] = None) -> ValidationResult:
        workspace = Path(workspace).resolve()
        result = ValidationResult()

        candidates = sorted(
            d for d in workspace.iterdir() if d.is_dir()
        ) if workspace.exists() else []
        if repos is not None:
            wanted = set(repos)
            candidates = [d for d in candidates if d.name in wanted]

        for repo_dir in candidates:
            command = test_cmd or detect_test_command(repo_dir)
            if not command:
                result.repos.append(RepoValidation(
                    repo_dir.name, str(repo_dir), "", passed=False,
                    skipped=True, detail="no test command detected",
                ))
                continue
            result.repos.append(self._run(repo_dir, command))

        return result

    def _run(self, repo_dir: Path, command: str) -> RepoValidation:
        try:
            proc = subprocess.run(
                command, shell=True, cwd=str(repo_dir),
                capture_output=True, text=True, timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return RepoValidation(repo_dir.name, str(repo_dir), command,
                                  passed=False, detail="timed out")
        except OSError as e:
            return RepoValidation(repo_dir.name, str(repo_dir), command,
                                  passed=False, detail=str(e))

        tail = (proc.stdout + proc.stderr).strip().splitlines()[-8:]
        return RepoValidation(
            repo_dir.name, str(repo_dir), command,
            passed=proc.returncode == 0,
            detail="passed" if proc.returncode == 0 else f"exit {proc.returncode}",
            output_tail="\n".join(tail),
        )
