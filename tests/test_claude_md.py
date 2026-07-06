from deplar.graph.store import DependencyGraph
from deplar.output.claude_md import ClaudeMdGenerator
from deplar.scanner.resolver import DependencyEdge


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