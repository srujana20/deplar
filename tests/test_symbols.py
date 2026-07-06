from pathlib import Path

from deplar.scanner.symbols import SymbolExtractor
from deplar.scanner.walker import RepoWalker

FIXTURES = Path(__file__).parent / "fixtures"


def _index(repo):
    fm = RepoWalker(FIXTURES / repo).walk()
    return SymbolExtractor().extract(fm, repo)


def test_python_functions_and_calls():
    idx = _index("order-service")
    fns = {s.qualified_name for s in idx.symbols if s.kind == "function"}
    assert "create_order" in fns
    assert "cancel_order" in fns
    # call sites carry the enclosing caller + line number
    sends = [c for c in idx.calls if c.callee_name == "send"]
    assert sends and all(c.caller in ("create_order", "cancel_order") for c in sends)
    assert all(c.line > 0 for c in sends)


def test_python_signature_captured():
    idx = _index("order-service")
    create = next(s for s in idx.symbols if s.name == "create_order")
    assert create.signature == "create_order(order)"


def test_typescript_functions():
    idx = _index("user-service")
    names = {s.name for s in idx.symbols}
    assert "getUser" in names
    assert "updateUser" in names
    axios_calls = [c for c in idx.calls if c.callee.startswith("axios")]
    assert axios_calls


def test_java_class_method_interface():
    idx = _index("payment-service")
    kinds = {(s.kind, s.name) for s in idx.symbols}
    assert ("interface", "FraudClient") in kinds
    assert ("class", "PaymentService") in kinds
    method = next(s for s in idx.symbols if s.name == "getUser")
    assert method.kind == "method"
    assert method.parent == "UserServiceClient"
