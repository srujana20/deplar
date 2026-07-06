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
