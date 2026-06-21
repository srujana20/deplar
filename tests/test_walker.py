from pathlib import Path
from deplar.scanner.walker import RepoWalker

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


def test_finds_python_files():
    print(f"Using fixture repo at: {FIXTURE}")
    fm = RepoWalker(FIXTURE).walk()
    print(f"Found files: {fm.files}")
    assert "python" in fm.files
    assert len(fm.files["python"]) == 2  # main.py + utils.py

def test_finds_typescript_files():
    fm = RepoWalker(FIXTURE).walk()
    assert "typescript" in fm.files

def test_finds_java_files():
    fm = RepoWalker(FIXTURE).walk()
    assert "java" in fm.files

def test_excludes_vendor():
    fm = RepoWalker(FIXTURE).walk()
    all_paths = [str(p) for p in fm.all_files()]
    assert not any("vendor" in p for p in all_paths)

def test_total_count():
    fm = RepoWalker(FIXTURE).walk()
    assert fm.total() == 5  # main.py, utils.py, client.ts, OrderService.java