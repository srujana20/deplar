from pathlib import Path

from deplar.output.claude_md import ClaudeMdGenerator
from deplar.scanner.ast_parser import ASTParser
from deplar.scanner.network_detector import NetworkDetector
from deplar.scanner.org_scanner import OrgConfig, OrgScanner
from deplar.scanner.resolver import DependencyResolver
from deplar.scanner.walker import RepoWalker

FIXTURES = Path(__file__).parent / "fixtures"
ORDER   = FIXTURES / "order-service"
PAYMENT = FIXTURES / "payment-service"
USER    = FIXTURES / "user-service"


# --- order-service ---

def test_order_service_http_detected():
    fm = RepoWalker(ORDER).walk()
    edges = NetworkDetector().detect(fm)
    http = [e for e in edges if e.call_type == "http"]
    assert len(http) >= 1

def test_order_service_kafka_detected():
    fm = RepoWalker(ORDER).walk()
    edges = NetworkDetector().detect(fm)
    kafka = [e for e in edges if e.call_type == "kafka"]
    topics = [e.target for e in kafka]
    assert "orders.created" in topics
    assert "orders.failed" in topics

def test_order_service_literal_url_high_confidence():
    fm = RepoWalker(ORDER).walk()
    edges = NetworkDetector().detect(fm)
    literal = [e for e in edges
               if "payments-service.internal" in e.target]
    assert literal, "Expected at least one literal URL detection"
    assert literal[0].confidence == 1.0


# --- payment-service ---

def test_payment_service_feign_clients_detected():
    fm = RepoWalker(PAYMENT).walk()
    _, feign = ASTParser().parse(fm)
    names = [e.client_name for e in feign]
    assert "fraud-service" in names
    assert "user-service" in names

def test_payment_service_feign_url_extracted():
    fm = RepoWalker(PAYMENT).walk()
    _, feign = ASTParser().parse(fm)
    fraud = next(e for e in feign if e.client_name == "fraud-service")
    assert "${FRAUD_SERVICE_URL}" in fraud.url_pattern

def test_payment_service_literal_feign_url():
    fm = RepoWalker(PAYMENT).walk()
    _, feign = ASTParser().parse(fm)
    user = next(e for e in feign if e.client_name == "user-service")
    assert "user-service.internal" in user.url_pattern


# --- user-service ---

def test_user_service_ts_imports_detected():
    fm = RepoWalker(USER).walk()
    imports, _ = ASTParser().parse(fm)
    modules = [e.imported_module for e in imports]
    assert any("axios" in m for m in modules)

def test_user_service_http_detected():
    fm = RepoWalker(USER).walk()
    edges = NetworkDetector().detect(fm)
    assert len(edges) >= 1


# --- full pipeline ---

def _run_pipeline(repo_path, repo_name):
    fm = RepoWalker(repo_path).walk()
    imports, feign = ASTParser().parse(fm)
    network = NetworkDetector().detect(fm)
    return DependencyResolver().resolve(repo_name, imports, feign, network)

def test_order_pipeline_produces_edges():
    edges = _run_pipeline(ORDER, "order-service")
    assert len(edges) >= 1

def test_payment_pipeline_finds_fraud_service():
    edges = _run_pipeline(PAYMENT, "payment-service")
    targets = [e.to_repo for e in edges]
    assert any("fraud" in t for t in targets)


# --- org scan ---

def test_org_scan_all_repos():
    config = OrgConfig.from_yaml(FIXTURES / "deplar.yaml")
    graph = OrgScanner().scan_org(config)
    assert graph.g.number_of_nodes() >= 2

def test_org_scan_produces_deps_json(tmp_path):
    config = OrgConfig.from_yaml(FIXTURES / "deplar.yaml")
    graph = OrgScanner().scan_org(config)
    out = tmp_path / "org-deps.json"
    graph.save(out)
    assert out.exists()
    import json
    data = json.loads(out.read_text())
    assert "dependencies" in data
    assert data["version"] == "1.0"


# --- CLAUDE.md ---

def test_claude_md_generated_for_org(tmp_path):
    config = OrgConfig.from_yaml(FIXTURES / "deplar.yaml")
    graph = OrgScanner().scan_org(config)
    content = ClaudeMdGenerator(graph, "order-service").generate()
    assert "order-service" in content
    assert "Agent instructions" in content