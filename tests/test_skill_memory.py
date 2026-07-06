import json

from deplar.graph.symbol_store import SymbolStore
from deplar.scanner.resolver import DependencyEdge
from deplar.scanner.symbols import Symbol, SymbolIndex
from deplar.skill import SkillGenerator, build_skillhub, read_skill, read_skill_index


def _store(tmp_path):
    store = SymbolStore(tmp_path / "t.db")
    store.upsert_repo("payment", "/repos/payment", "2026-01-01T00:00:00Z")
    store.replace_dependencies([
        DependencyEdge("order", "payment", ["http"], 1.0, []),
        DependencyEdge("payment", "orders.created", ["kafka"], 1.0, []),
    ])
    idx = SymbolIndex()
    idx.symbols.append(Symbol("payment", "p.py", "python", "class",
                              "Charger", "Charger", "Charger", "", 1, 20))
    store.replace_symbols("payment", idx)
    return store


# --- memory ---

def test_memory_remember_recall_forget(tmp_path):
    store = SymbolStore(tmp_path / "t.db")
    mid = store.remember("svc", "always call refresh() first", kind="gotcha")
    notes = store.recall("svc")
    assert notes[0]["note"] == "always call refresh() first"
    assert notes[0]["kind"] == "gotcha"
    assert store.recall("svc", kind="pattern") == []
    assert store.forget(mid) is True
    assert store.recall("svc") == []


def test_memory_invalid_kind_defaults_to_note(tmp_path):
    store = SymbolStore(tmp_path / "t.db")
    store.remember("svc", "x", kind="bogus")
    assert store.recall("svc")[0]["kind"] == "note"


# --- versioning ---

def test_snapshot_hash_is_stable_and_changes(tmp_path):
    store = _store(tmp_path)
    h1 = store.snapshot_hash("payment")
    assert h1 == store.snapshot_hash("payment")   # stable
    idx = SymbolIndex()
    idx.symbols.append(Symbol("payment", "p.py", "python", "method",
                              "Charger.charge", "Charger.charge", "charge(x)",
                              "Charger", 5, 9))
    store.replace_symbols("payment", idx)
    assert store.snapshot_hash("payment") != h1   # changes with the graph


# --- skill generation ---

def test_skill_has_frontmatter_and_sections(tmp_path):
    content = SkillGenerator(_store(tmp_path)).generate("payment")
    assert content.startswith("---")
    assert "name: work-in-payment" in content
    assert "version:" in content
    assert "## What this service calls" in content
    assert "## Public API surface" in content
    assert "Charger" in content


def test_build_skillhub_writes_registry_and_portal(tmp_path):
    store = _store(tmp_path)
    out = tmp_path / "hub"
    build_skillhub(store, out)
    assert (out / "payment" / "SKILL.md").exists()
    assert (out / "index.json").exists()
    assert (out / "index.html").exists()
    # registry readers used by the MCP server
    assert read_skill_index(out)[0]["repo"] == "payment"
    assert "Working in `payment`" in read_skill(out, "payment")
    assert read_skill(out, "missing") is None
    on_disk = json.loads((out / "index.json").read_text())
    assert on_disk[0]["languages"] == ["python"]
