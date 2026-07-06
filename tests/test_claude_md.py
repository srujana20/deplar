from deplar.graph.store import DependencyGraph
from deplar.graph.symbol_store import SymbolStore
from deplar.output.claude_md import ClaudeMdGenerator
from deplar.scanner.resolver import DependencyEdge
from deplar.scanner.symbols import CallSite, Symbol, SymbolIndex


def _make_graph():
    g = DependencyGraph()
    g.add_dependency(DependencyEdge(
        from_repo="order-service",
        to_repo="payments-service",
        dep_types=["feign", "http"],
        confidence=1.0,
        evidence=["OrderService.java:47"],
    ))
    g.add_dependency(DependencyEdge(
        from_repo="order-service",
        to_repo="orders.created",
        dep_types=["kafka"],
        confidence=1.0,
        evidence=["events.py:12"],
    ))
    g.add_dependency(DependencyEdge(
        from_repo="checkout-service",
        to_repo="order-service",
        dep_types=["http"],
        confidence=0.9,
        evidence=["CheckoutClient.java:23"],
    ))
    return g


def test_contains_service_calls():
    g = _make_graph()
    content = ClaudeMdGenerator(g, "order-service").generate()
    assert "payments-service" in content


def test_contains_kafka_topics():
    g = _make_graph()
    content = ClaudeMdGenerator(g, "order-service").generate()
    assert "orders.created" in content


def test_contains_dependents():
    g = _make_graph()
    content = ClaudeMdGenerator(g, "order-service").generate()
    assert "checkout-service" in content


def test_contains_agent_instructions():
    g = _make_graph()
    content = ClaudeMdGenerator(g, "order-service").generate()
    assert "Agent instructions" in content


def test_confidence_label_high():
    g = _make_graph()
    content = ClaudeMdGenerator(g, "order-service").generate()
    assert "confidence: high" in content


def test_write_creates_file(tmp_path):
    g = _make_graph()
    out = tmp_path / "CLAUDE.md"
    ClaudeMdGenerator(g, "order-service").write(out)
    assert out.exists()
    assert out.stat().st_size > 0


# --- v2: symbol-aware sections (require the store) ---

def _store_with_cross_repo_caller(tmp_path):
    store = SymbolStore(tmp_path / "t.db")
    idx = SymbolIndex()
    idx.symbols.append(Symbol("order-service", "src/api.py", "python", "function",
                              "create_order", "create_order", "create_order(o)",
                              "", 10, 20))
    store.replace_symbols("order-service", idx)
    # checkout-service calls order-service's create_order at line 23
    other = SymbolIndex()
    other.calls.append(CallSite("checkout-service", "src/main.py", "checkout",
                                "client.create_order", "create_order", 23))
    store.replace_symbols("checkout-service", other)
    return store


def test_v2_includes_api_surface_with_line_numbers(tmp_path):
    g = _make_graph()
    store = _store_with_cross_repo_caller(tmp_path)
    content = ClaudeMdGenerator(g, "order-service", store=store).generate()
    assert "Public API surface" in content
    assert "create_order(o)" in content
    assert "src/api.py:10" in content


def test_v2_includes_cross_repo_call_sites(tmp_path):
    g = _make_graph()
    store = _store_with_cross_repo_caller(tmp_path)
    content = ClaudeMdGenerator(g, "order-service", store=store).generate()
    assert "Cross-repo call sites" in content
    assert "checkout-service/src/main.py:23" in content


def test_v2_learned_patterns(tmp_path):
    g = _make_graph()
    store = _store_with_cross_repo_caller(tmp_path)
    store.remember("order-service", "orders are not idempotent", kind="gotcha")
    content = ClaudeMdGenerator(g, "order-service", store=store).generate()
    assert "Learned patterns" in content
    assert "not idempotent" in content