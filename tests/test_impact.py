from deplar.graph.symbol_store import SymbolStore
from deplar.impact import ImpactAnalyzer
from deplar.scanner.resolver import DependencyEdge
from deplar.scanner.symbols import CallSite, Symbol, SymbolIndex


def _store(tmp_path):
    store = SymbolStore(tmp_path / "t.db")
    store.replace_dependencies([
        DependencyEdge("order", "payment", ["http"], 1.0, []),
        DependencyEdge("checkout", "order", ["http"], 1.0, []),
        DependencyEdge("payment", "orders.created", ["kafka"], 1.0, []),
    ])
    idx = SymbolIndex()
    idx.symbols.append(Symbol("payment", "p.py", "python", "function",
                              "charge", "charge", "charge(x)", "", 5, 9))
    idx.calls.append(CallSite("order", "o.py", "main", "pay.charge", "charge", 12))
    store.replace_symbols("payment", idx)
    store.replace_symbols("order", idx)  # gives order a cross-repo caller of charge
    return store


def test_impact_lists_dependents_and_blast(tmp_path):
    report = ImpactAnalyzer(_store(tmp_path)).analyze("payment")
    assert [d["repo"] for d in report.direct_dependents] == ["order"]
    assert set(report.blast_radius) == {"order", "checkout"}
    assert "orders.created" in report.events_emitted


def test_impact_symbol_scope(tmp_path):
    report = ImpactAnalyzer(_store(tmp_path)).analyze("payment", symbol="charge")
    assert any(s["qualified_name"] == "charge" for s in report.symbol_definitions)
    # order calls charge -> cross-repo caller relative to payment
    assert any(c["repo"] == "order" for c in report.cross_repo_callers)


def test_impact_markdown_renders(tmp_path):
    report = ImpactAnalyzer(_store(tmp_path)).analyze("payment", symbol="charge")
    md = ImpactAnalyzer.render_markdown(report)
    assert "# Impact report" in md
    assert "deplar workspace payment" in md
    assert "order" in md
