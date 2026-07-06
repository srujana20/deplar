import subprocess

from deplar.graph.symbol_store import SymbolStore
from deplar.scanner.resolver import DependencyEdge
from deplar.worktree import WorktreeManager


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args],
                          capture_output=True, text=True)


def _make_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "file.txt").write_text("hello\n")
    _git(path, "init", "-q")
    _git(path, "add", "-A")
    _git(path, "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-qm", "init")


def _store_with_org(tmp_path):
    store = SymbolStore(tmp_path / "t.db")
    repos = {}
    for name in ("payment", "order", "checkout"):
        repo_dir = tmp_path / "org" / name
        _make_repo(repo_dir)
        store.upsert_repo(name, str(repo_dir), "2026-01-01T00:00:00Z")
        repos[name] = repo_dir
    # order -> payment, checkout -> order
    store.replace_dependencies([
        DependencyEdge("order", "payment", ["http"], 1.0, []),
        DependencyEdge("checkout", "order", ["http"], 1.0, []),
    ])
    return store, repos


def test_affected_repos_direct(tmp_path):
    store, _ = _store_with_org(tmp_path)
    affected = WorktreeManager(store).affected_repos("payment")
    assert affected[0] == "payment"        # target first
    assert set(affected) == {"payment", "order"}


def test_affected_repos_transitive(tmp_path):
    store, _ = _store_with_org(tmp_path)
    affected = WorktreeManager(store).affected_repos("payment", transitive=True)
    assert set(affected) == {"payment", "order", "checkout"}


def test_checkout_creates_worktrees(tmp_path):
    store, _ = _store_with_org(tmp_path)
    ws = tmp_path / "ws"
    results = WorktreeManager(store).checkout("payment", ws, "deplar/change")
    created = {r.repo for r in results if r.status == "created"}
    assert created == {"payment", "order"}
    for repo in created:
        assert (ws / repo).exists()
        branch = _git(ws / repo, "branch", "--show-current").stdout.strip()
        assert branch == "deplar/change"


def test_checkout_skips_non_git_and_missing(tmp_path):
    store = SymbolStore(tmp_path / "t.db")
    plain = tmp_path / "plain"
    plain.mkdir()
    store.upsert_repo("plain", str(plain), "2026-01-01T00:00:00Z")
    store.replace_dependencies([DependencyEdge("dep", "plain", ["http"], 1.0, [])])
    results = WorktreeManager(store).checkout("plain", tmp_path / "ws", "b")
    statuses = {r.repo: r.status for r in results}
    assert statuses["plain"] == "skipped"      # not a git repo
    assert statuses["dep"] == "skipped"        # no path on record


def test_remove_tears_down(tmp_path):
    store, _ = _store_with_org(tmp_path)
    ws = tmp_path / "ws"
    mgr = WorktreeManager(store)
    mgr.checkout("payment", ws, "deplar/change")
    removed = mgr.remove(ws)
    assert all(r.status == "removed" for r in removed)
    assert not (ws / "payment").exists()
