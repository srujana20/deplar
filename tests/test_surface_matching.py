"""End-to-end surface matching: consumer calls bound to concrete provider
routes, and the per-repo interface manifest."""
from pathlib import Path

from deplar.scanner.org_scanner import OrgConfig, OrgScanner
from deplar.scanner.resolver import DependencyEdge
from deplar.scanner.route_detector import RouteEdge
from deplar.scanner.surface_matcher import (
    RouteIndex,
    SurfaceMatcher,
    build_interface_manifest,
)

FX = Path(__file__).parent / "fixtures"


def _edge(frm, to, method, path):
    return DependencyEdge(
        from_repo=frm, to_repo=to, dep_types=["http"], confidence=1.0,
        surfaces=[{"channel": "http", "method": method, "path": path,
                   "key": f"{method} {path}", "matched": False, "evidence": "x:1"}],
    )


class TestRouteIndex:
    def test_matches_exact(self):
        idx = RouteIndex()
        idx.add("b", RouteEdge(Path("R.java"), "GET", "/v1/users/{}", "spring", 1))
        assert idx.matches("b", "GET", "/v1/users/{id}")   # param form folds
        assert not idx.matches("b", "POST", "/v1/users/{id}")
        assert not idx.matches("other", "GET", "/v1/users/{id}")

    def test_any_verb_wildcards(self):
        idx = RouteIndex()
        idx.add("b", RouteEdge(Path("R.java"), "ANY", "/x", "spring", 1))
        assert idx.matches("b", "GET", "/x")
        # consumer with unknown verb matches any provided verb on that path
        idx2 = RouteIndex()
        idx2.add("b", RouteEdge(Path("R.java"), "POST", "/x", "spring", 1))
        assert idx2.matches("b", "ANY", "/x")


class TestSurfaceMatcher:
    def test_marks_matched_and_unmatched(self):
        idx = RouteIndex()
        idx.add("b", RouteEdge(Path("R.java"), "GET", "/users/{}", "spring", 1))
        edges = [
            _edge("a", "b", "GET", "/users/{id}"),   # should match
            _edge("a", "b", "DELETE", "/users/{id}"),  # verb miss -> unmatched
        ]
        stats = SurfaceMatcher().match(edges, idx)
        assert stats.surfaces_total == 2
        assert stats.surfaces_matched == 1
        assert stats.surfaces_unmatched == 1
        assert edges[0].surfaces[0]["matched"] is True
        assert edges[1].surfaces[0]["matched"] is False


class TestInterfaceManifest:
    def test_provides_and_consumes(self):
        idx = RouteIndex()
        idx.add("b", RouteEdge(Path("R.java"), "GET", "/users/{}", "spring", 10))
        edges = [_edge("a", "b", "GET", "/users/{id}")]
        SurfaceMatcher().match(edges, idx)
        m = build_interface_manifest(["a", "b"], edges, idx)
        assert m["b"]["provides"]["http"][0]["path"] == "/users/{}"
        assert m["a"]["consumes"]["http"][0]["target"] == "b"
        assert m["a"]["consumes"]["http"][0]["matched"] is True


class TestOrgScanEndToEnd:
    def test_fixtures_surface_matches(self):
        scanner = OrgScanner()
        cfg = OrgConfig.from_yaml(FX / "deplar.yaml")
        scanner.scan_org(cfg)

        # the two real cross-repo endpoint matches in the fixtures
        assert scanner.last_match_stats.surfaces_matched >= 2

        m = scanner.last_manifest
        # payment-service provides the routes it declares
        pay_provides = {(p["method"], p["path"])
                        for p in m["payment-service"]["provides"]["http"]}
        assert ("POST", "/v1/charge") in pay_provides

        # order-service -> payment-service on POST /v1/charge is matched
        charge = [c for c in m["order-service"]["consumes"]["http"]
                  if c["key"] == "POST /v1/charge"]
        assert charge and charge[0]["target"] == "payment-service"
        assert charge[0]["matched"] is True

        # payment-service -> user-service via Feign GET /v1/users/{} is matched
        getu = [c for c in m["payment-service"]["consumes"]["http"]
                if c["key"] == "GET /v1/users/{}"]
        assert getu and getu[0]["target"] == "user-service"
        assert getu[0]["matched"] is True
