from pathlib import Path

from deplar.scanner.org_scanner import OrgConfig, OrgScanner

FIXTURES = Path(__file__).parent / "fixtures"


def test_org_config_from_yaml():
    config = OrgConfig.from_yaml(FIXTURES / "deplar.yaml")
    assert len(config.repos) == 3
    assert config.repos[0].name == "order-service"


def test_org_config_from_directory():
    config = OrgConfig.from_directory(FIXTURES)
    assert len(config.repos) >= 1


def test_scan_org_builds_graph():
    config = OrgConfig.from_yaml(FIXTURES / "deplar.yaml")
    scanner = OrgScanner(verbose=False)
    graph = scanner.scan_org(config)
    assert graph.g.number_of_nodes() >= 1


def test_cross_resolve_boosts_confidence():
    from deplar.scanner.resolver import DependencyEdge
    scanner = OrgScanner()
    edges = [
        DependencyEdge(
            from_repo="order-service",
            to_repo="payments",
            dep_types=["http"],
            confidence=0.7,
            evidence=[],
        )
    ]
    known = {"payments-service"}
    resolved = scanner._cross_resolve(edges, known)
    # payments fuzzy-matches payments-service
    assert resolved[0].confidence > 0.7