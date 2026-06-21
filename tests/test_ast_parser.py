from pathlib import Path
from deplar.scanner.ast_parser import (
    parse_python_imports,
    parse_java_imports,
    parse_feign_clients,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo" / "src"


def test_python_import_statement():
    edges = parse_python_imports(FIXTURE / "main.py")
    modules = [e.imported_module for e in edges]
    assert "requests" in modules

def test_python_from_import():
    edges = parse_python_imports(FIXTURE / "main.py")
    modules = [e.imported_module for e in edges]
    assert any("." in m or "utils" in m for m in modules)

def test_java_imports():
    edges = parse_java_imports(FIXTURE / "OrderService.java")
    modules = [e.imported_module for e in edges]
    print(f"Java imported modules: {modules}")
    assert any("payments" in m for m in modules)

def test_feign_client_detected():
    edges = parse_feign_clients(FIXTURE / "OrderService.java")
    assert len(edges) >= 1
    assert edges[0].client_name == "payments-service"

def test_feign_url_extracted():
    edges = parse_feign_clients(FIXTURE / "OrderService.java")
    assert "${PAYMENTS_URL}" in edges[0].url_pattern