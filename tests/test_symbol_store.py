from deplar.graph.symbol_store import SymbolStore
from deplar.scanner.resolver import DependencyEdge
from deplar.scanner.symbols import CallSite, Symbol, SymbolIndex


def _store(tmp_path):
    return SymbolStore(tmp_path / "t.db")


def _sample_index(repo="svc"):
    idx = SymbolIndex()
    idx.symbols.append(Symbol(repo, "a.py", "python", "function", "charge",
                              "charge", "charge(x)", "", 10, 12))
    idx.calls.append(CallSite(repo, "a.py", "main", "pay.charge", "charge", 20))
    return idx


def test_symbols_replace_and_search(tmp_path):
    store = _store(tmp_path)
    store.replace_symbols("svc", _sample_index())
    hits = store.search_symbols("charge")
    assert len(hits) == 1
    assert hits[0]["qualified_name"] == "charge"
    assert hits[0]["start_line"] == 10


def test_replace_is_idempotent(tmp_path):
    store = _store(tmp_path)
    store.replace_symbols("svc", _sample_index())
    store.replace_symbols("svc", _sample_index())
    assert len(store.search_symbols("charge")) == 1


def test_get_callers(tmp_path):
    store = _store(tmp_path)
    store.replace_symbols("svc", _sample_index())
    callers = store.get_callers("charge")
    assert callers[0]["caller"] == "main"
    assert callers[0]["line"] == 20


def test_dependencies_and_dependents(tmp_path):
    store = _store(tmp_path)
    store.replace_dependencies([
        DependencyEdge("order", "payment", ["http"], 1.0, ["a.py:1"]),
        DependencyEdge("checkout", "order", ["feign"], 0.95, ["b.py:2"]),
    ])
    assert store.get_dependencies("order")[0]["repo"] == "payment"
    assert store.get_dependents("order")[0]["repo"] == "checkout"


def test_blast_radius_transitive(tmp_path):
    store = _store(tmp_path)
    store.replace_dependencies([
        DependencyEdge("order", "payment", ["http"], 1.0, []),
        DependencyEdge("checkout", "order", ["http"], 1.0, []),
    ])
    # who is affected by changing payment? order (direct) + checkout (transitive)
    radius = store.blast_radius("payment")
    assert set(radius) == {"order", "checkout"}


def test_repo_path_roundtrip(tmp_path):
    store = _store(tmp_path)
    store.upsert_repo("svc", "/repos/svc", "2026-01-01T00:00:00Z")
    assert store.repo_path("svc") == "/repos/svc"
    assert store.repo_path("missing") is None


# --- manual alias override ---

def test_add_and_remove_manual_alias(tmp_path):
    store = _store(tmp_path)
    alias = store.add_alias("billing", "https://neptune.internal/api")
    assert alias == "neptune"
    rows = {a["alias"]: a for a in store.aliases_for_repo("billing")}
    assert rows["neptune"]["source"] == "manual"
    assert rows["neptune"]["confidence"] == 1.0
    # remove by raw or by normalized form
    assert store.remove_alias("billing", "https://neptune.internal/api") is True
    assert store.aliases_for_repo("billing") == []
    assert store.remove_alias("billing", "neptune") is False


def test_manual_alias_survives_rescan(tmp_path):
    store = _store(tmp_path)
    store.add_alias("billing", "neptune")
    # a re-scan replaces auto aliases but must keep the manual pin
    store.replace_aliases("billing", [
        {"alias": "billing", "raw": "billing", "source": "config", "confidence": 1.0},
    ])
    aliases = {a["alias"]: a["source"] for a in store.aliases_for_repo("billing")}
    assert aliases == {"billing": "config", "neptune": "manual"}


def test_empty_alias_not_pinned(tmp_path):
    store = _store(tmp_path)
    assert store.add_alias("billing", "https://") == ""
    assert store.aliases_for_repo("billing") == []
