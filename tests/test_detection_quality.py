"""Precision guards: call-site gating, namespace denylist, gRPC evidence,
confidence tiers, and config-file scanning."""
from pathlib import Path

from deplar.scanner.config_scanner import scan_config_file
from deplar.scanner.network_detector import (
    detect_java_network_calls,
    detect_python_network_calls,
    is_namespace_noise,
)
from deplar.scanner.reconciler import AliasCatalog, Reconciler
from deplar.scanner.resolver import DependencyResolver


def _java(tmp_path, body):
    f = tmp_path / "C.java"
    f.write_text(body)
    return detect_java_network_calls(f)


class TestNamespaceDenylist:
    def test_flags_namespace_uris(self):
        assert is_namespace_noise("http://www.w3.org/2001/XMLSchema")
        assert is_namespace_noise("http://schemas.xmlsoap.org/soap/envelope/")
        assert is_namespace_noise("http://apache.org/xml/features/validation")
        assert is_namespace_noise("http://xml.org/sax/features/namespaces")
        assert not is_namespace_noise("https://payments.internal/v1/charge")

    def test_namespace_never_becomes_an_edge(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text(
            "import requests\n"
            "requests.get('http://www.w3.org/2001/XMLSchema')\n"
            "requests.post('https://payments.internal/charge')\n"
        )
        targets = [e.target for e in detect_python_network_calls(f)]
        assert not any("w3.org" in t for t in targets)
        assert any("payments.internal" in t for t in targets)

    def test_resolver_safety_net(self, tmp_path):
        # even if a namespace URI reached the resolver, it is dropped
        from deplar.scanner.network_detector import NetworkCallEdge
        edges = DependencyResolver().resolve("a", [], [], [
            NetworkCallEdge(Path("x.py"), "http",
                            "http://www.w3.org/2001/XMLSchema", 1.0, 1),
        ])
        assert edges == []


class TestCallSiteGating:
    def test_okhttp_url_requires_builder(self, tmp_path):
        calls = _java(tmp_path, """
        class C {
          void a(){ documentFactory.url("https://not-a-service.internal/x"); }
          void b(){ new Request.Builder().url("https://real.internal/v1/x").build(); }
        }
        """)
        targets = [e.target for e in calls]
        assert any("real.internal" in t for t in targets)
        assert not any("not-a-service" in t for t in targets)

    def test_http_url_connection(self, tmp_path):
        calls = _java(tmp_path, """
        class C { void a(){
          (HttpURLConnection) new URL("https://svc.internal/v1/ping").openConnection();
        }}""")
        assert any("svc.internal" in e.target for e in calls)


class TestGrpcEvidence:
    def test_requires_grpc_import(self, tmp_path):
        with_imp = tmp_path / "a.py"
        with_imp.write_text("import grpc\ngrpc.insecure_channel('inv:50051')\n")
        without = tmp_path / "b.py"
        without.write_text("obj.insecure_channel('inv:50051')\n")
        assert any(e.call_type == "grpc" for e in detect_python_network_calls(with_imp))
        assert not any(e.call_type == "grpc" for e in detect_python_network_calls(without))


class TestConfidenceTiers:
    def test_literal_is_call_site(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text("import requests\nrequests.get('https://svc.internal/x')\n")
        e = detect_python_network_calls(f)[0]
        assert e.tier == "call-site" and e.confidence == 1.0

    def test_unresolved_is_inferred(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text("import requests\ndef f(base):\n    requests.get(f'{base}/x')\n")
        e = detect_python_network_calls(f)[0]
        assert e.tier == "inferred" and e.confidence < 0.5

    def test_edge_takes_strongest_tier(self, tmp_path):
        from deplar.scanner.network_detector import NetworkCallEdge
        edges = DependencyResolver().resolve("a", [], [], [
            NetworkCallEdge(Path("x.py"), "http", "https://b.internal/x", 0.85, 1,
                            tier="config"),
            NetworkCallEdge(Path("x.py"), "http", "https://b.internal/y", 1.0, 2,
                            tier="call-site"),
        ])
        assert edges[0].tier == "call-site"


class TestConfigScanner:
    def test_properties_extracts_http_skips_infra(self, tmp_path):
        f = tmp_path / "application.properties"
        f.write_text(
            "payments.service.url=https://payment-service.internal\n"
            "server.port=8080\n"
            "spring.datasource.url=jdbc:postgresql://db:5432/o\n"
            "spring.kafka.bootstrap-servers=kafka:9092\n"
            "schema.ns=http://www.w3.org/2001/XMLSchema\n"
            "billing.url=${BILLING_URL}\n"
        )
        edges = scan_config_file(f)
        targets = [e.target for e in edges]
        assert any("payment-service.internal" in t for t in targets)
        assert all(e.tier == "config" for e in edges)
        assert not any("jdbc" in t or "kafka" in t or "w3.org" in t
                       or "BILLING" in t for t in targets)

    def test_yaml_nested_urls(self, tmp_path):
        f = tmp_path / "application.yml"
        f.write_text(
            "services:\n"
            "  payments:\n"
            "    url: https://payment-service.internal/v1\n"
            "datasource:\n"
            "  url: jdbc:mysql://db/o\n"
        )
        targets = [e.target for e in scan_config_file(f)]
        assert targets == ["https://payment-service.internal/v1"]

    def test_config_edge_resolves_to_repo(self, tmp_path):
        f = tmp_path / "application.yml"
        f.write_text("services:\n  orders:\n    url: https://order-service.internal\n")
        net = scan_config_file(f)
        edges = DependencyResolver().resolve("payment-service", [], [], net)
        cat = AliasCatalog.from_aliases([
            {"repo": "order-service", "alias": "order", "confidence": 1.0}])
        resolved, _ = Reconciler().reconcile(edges, cat)
        assert any(e.to_repo == "order-service" and e.tier == "config"
                   for e in resolved)
