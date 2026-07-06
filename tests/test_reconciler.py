from deplar.scanner.reconciler import AliasCatalog, Reconciler
from deplar.scanner.resolver import DependencyEdge


def _catalog(*pairs):
    """pairs of (repo, alias) -> catalog."""
    aliases = [{"repo": r, "alias": a, "confidence": 0.9, "source": "test"}
               for r, a in pairs]
    return AliasCatalog.from_aliases(aliases)


def test_resolve_exact():
    cat = _catalog(("orders", "order-management"))
    m = cat.resolve("order-management-service.internal")
    assert m.repo == "orders" and m.reason == "exact"


def test_resolve_stem_plural():
    cat = _catalog(("payment", "payment"))
    m = cat.resolve("https://payments-svc.internal/charge")
    assert m.repo == "payment" and m.reason == "stem"


def test_resolve_excludes_self():
    cat = _catalog(("user", "user"))
    assert cat.resolve("user-service", exclude_repo="user") is None


def test_resolve_miss_returns_none():
    cat = _catalog(("orders", "order-management"))
    assert cat.resolve("requests") is None
    assert cat.resolve("totally-unrelated") is None


def test_reconcile_binds_dangling_reference():
    cat = _catalog(("orders", "order-management"))
    edges = [DependencyEdge("checkout", "order-management-service", ["http"], 1.0, [])]
    resolved, stats = Reconciler().reconcile(edges, cat)
    assert stats.resolved == 1
    assert resolved[0].to_repo == "orders"


def test_reconcile_drops_self_reference():
    cat = _catalog(("user", "user"))
    edges = [DependencyEdge("user", "user-service.internal", ["http"], 0.6, [])]
    resolved, stats = Reconciler().reconcile(edges, cat)
    assert stats.dropped_self == 1
    assert resolved == []


def test_reconcile_merges_duplicates_after_binding():
    cat = _catalog(("payment", "payment"))
    edges = [
        DependencyEdge("order", "payment-service", ["http"], 0.7, ["a.py:1"]),
        DependencyEdge("order", "payments", ["feign"], 0.95, ["b.py:2"]),
    ]
    resolved, stats = Reconciler().reconcile(edges, cat)
    assert len(resolved) == 1
    edge = resolved[0]
    assert edge.to_repo == "payment"
    assert set(edge.dep_types) == {"http", "feign"}
    assert edge.confidence == 0.95
    assert stats.merged == 1


def test_reconcile_leaves_external_libs_unresolved():
    cat = _catalog(("orders", "order-management"))
    edges = [DependencyEdge("checkout", "requests", ["import"], 0.6, [])]
    resolved, stats = Reconciler().reconcile(edges, cat)
    assert stats.unresolved == 1
    assert resolved[0].to_repo == "requests"
