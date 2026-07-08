"""Server context-path / base-path folding into provided routes.

The bug: a Spring controller declares `@PostMapping("/create-pnr")` but the
service is mounted behind `server.servlet.context-path=/pss-api`, so the real
surface is `/pss-api/create-pnr`. A consumer calls the full path; the provider
only recorded `/create-pnr`; the match was missed. These tests pin both the
detection (Layer 1) and the segment-suffix fallback (Layer 2).
"""
from pathlib import Path

from deplar.scanner.base_path_detector import (
    apply_base_paths,
    detect_base_paths,
)
from deplar.scanner.resolver import DependencyEdge
from deplar.scanner.route_detector import RouteEdge, detect_java_routes
from deplar.scanner.surface_matcher import RouteIndex, SurfaceMatcher

FX = Path(__file__).parent / "fixtures"
PSS = FX / "pss"


def _consumer_edge(to, method, path):
    return DependencyEdge(
        from_repo="baggage", to_repo=to, dep_types=["http"], confidence=1.0,
        surfaces=[{"channel": "http", "method": method, "path": path,
                   "key": f"{method} {path}", "matched": False, "evidence": "x:1"}],
    )


class TestDetectBasePaths:
    def test_reads_context_path_from_properties(self):
        base = detect_base_paths(PSS)
        assert base.get("spring") == "/pss-api"
        # a JAX-RS resource in the same Boot app sits behind the same prefix
        assert base.get("jaxrs") == "/pss-api"

    def test_yaml_context_path(self, tmp_path):
        (tmp_path / "application.yml").write_text(
            "server:\n  servlet:\n    context-path: /orders-api\n")
        assert detect_base_paths(tmp_path).get("spring") == "/orders-api"

    def test_boot1_server_context_path(self, tmp_path):
        (tmp_path / "application.properties").write_text(
            "server.context-path=/legacy\n")
        assert detect_base_paths(tmp_path).get("spring") == "/legacy"

    def test_context_path_plus_servlet_path(self, tmp_path):
        (tmp_path / "application.properties").write_text(
            "server.servlet.context-path=/pss-api\n"
            "spring.mvc.servlet.path=/svc\n")
        assert detect_base_paths(tmp_path).get("spring") == "/pss-api/svc"

    def test_webflux_base_path(self, tmp_path):
        (tmp_path / "application.yml").write_text(
            "spring:\n  webflux:\n    base-path: /reactive\n")
        assert detect_base_paths(tmp_path).get("spring") == "/reactive"

    def test_placeholder_with_default_resolves(self, tmp_path):
        (tmp_path / "application.properties").write_text(
            "server.servlet.context-path=${CTX:/pss-api}\n")
        assert detect_base_paths(tmp_path).get("spring") == "/pss-api"

    def test_bare_placeholder_is_dropped(self, tmp_path):
        (tmp_path / "application.properties").write_text(
            "server.servlet.context-path=${CTX}\n")
        assert detect_base_paths(tmp_path) == {}

    def test_no_context_path_is_empty(self, tmp_path):
        (tmp_path / "application.properties").write_text("server.port=8080\n")
        assert detect_base_paths(tmp_path) == {}

    def test_root_context_path_is_noop(self, tmp_path):
        (tmp_path / "application.properties").write_text(
            "server.servlet.context-path=/\n")
        assert detect_base_paths(tmp_path) == {}


class TestApplyBasePaths:
    def test_prefix_folded_into_spring_route(self):
        routes = detect_java_routes(PSS / "src/PnrController.java")
        before = {(r.method, r.path) for r in routes}
        assert ("POST", "/create-pnr") in before

        apply_base_paths(routes, detect_base_paths(PSS))
        after = {(r.method, r.path) for r in routes}
        assert ("POST", "/pss-api/create-pnr") in after
        assert ("GET", "/pss-api/pnr/{}") in after

    def test_non_matching_framework_untouched(self):
        r = RouteEdge(Path("app.py"), "GET", "/health", "fastapi", 1)
        apply_base_paths([r], {"spring": "/pss-api"})
        assert r.path == "/health"

    def test_empty_base_is_noop(self):
        r = RouteEdge(Path("R.java"), "POST", "/create-pnr", "spring", 1)
        apply_base_paths([r], {})
        assert r.path == "/create-pnr"


class TestEndToEndPssBaggage:
    """The provider records the full external path; the consumer's call matches
    it exactly — the scenario the bug broke."""

    def test_full_path_matches_exact(self):
        routes = detect_java_routes(PSS / "src/PnrController.java")
        apply_base_paths(routes, detect_base_paths(PSS))
        idx = RouteIndex()
        for r in routes:
            idx.add("pss", r)

        edges = [_consumer_edge("pss", "POST", "/pss-api/create-pnr")]
        SurfaceMatcher().match(edges, idx)
        s = edges[0].surfaces[0]
        assert s["matched"] is True
        assert s["match_kind"] == "exact"


class TestPrefixFallback:
    """Layer 2: even when the context path is *not* resolved statically (e.g.
    injected purely at deploy time), a consumer's full path still binds to the
    provider's shorter declared path via a segment-boundary suffix match."""

    def test_consumer_longer_than_provider(self):
        idx = RouteIndex()
        # provider recorded only the controller path (base not applied)
        idx.add("pss", RouteEdge(Path("R.java"), "POST", "/create-pnr", "spring", 1))
        edges = [_consumer_edge("pss", "POST", "/pss-api/create-pnr")]
        stats = SurfaceMatcher().match(edges, idx)
        s = edges[0].surfaces[0]
        assert s["matched"] is True
        assert s["match_kind"] == "prefix"
        assert stats.surfaces_prefix_matched == 1

    def test_provider_longer_than_consumer(self):
        idx = RouteIndex()
        idx.add("pss", RouteEdge(Path("R.java"), "POST", "/pss-api/create-pnr", "spring", 1))
        edges = [_consumer_edge("pss", "POST", "/create-pnr")]
        SurfaceMatcher().match(edges, idx)
        assert edges[0].surfaces[0]["match_kind"] == "prefix"

    def test_no_false_match_across_partial_segment(self):
        idx = RouteIndex()
        idx.add("pss", RouteEdge(Path("R.java"), "POST", "/create-pnr", "spring", 1))
        # not a segment-boundary suffix: must NOT match
        edges = [_consumer_edge("pss", "POST", "/bulk-create-pnr")]
        SurfaceMatcher().match(edges, idx)
        assert edges[0].surfaces[0]["matched"] is False

    def test_prefix_respects_verb(self):
        idx = RouteIndex()
        idx.add("pss", RouteEdge(Path("R.java"), "POST", "/create-pnr", "spring", 1))
        edges = [_consumer_edge("pss", "GET", "/pss-api/create-pnr")]
        SurfaceMatcher().match(edges, idx)
        assert edges[0].surfaces[0]["matched"] is False

    def test_fallback_is_repo_scoped(self):
        idx = RouteIndex()
        idx.add("pss", RouteEdge(Path("R.java"), "POST", "/create-pnr", "spring", 1))
        # consumer resolved to a different repo — must not borrow pss's route
        edges = [_consumer_edge("other", "POST", "/pss-api/create-pnr")]
        SurfaceMatcher().match(edges, idx)
        assert edges[0].surfaces[0]["matched"] is False
