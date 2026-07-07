"""Coverage for the completed HTTP signal: Java clients, env-var / wrapped-client
resolution, JAX-RS, Express mount prefixes, SOAP, and endpoint-scoped impact."""
from pathlib import Path

from deplar.graph.symbol_store import SymbolStore
from deplar.impact import ImpactAnalyzer
from deplar.scanner.network_detector import (
    detect_java_network_calls,
    detect_python_network_calls,
    detect_ts_network_calls,
)
from deplar.scanner.resolver import DependencyEdge
from deplar.scanner.route_detector import detect_java_routes, detect_ts_routes


def _java(tmp_path, body):
    f = tmp_path / "C.java"
    f.write_text(body)
    return {f"{e.method} {e.path}": e for e in detect_java_network_calls(f)}


def _ts(tmp_path, body, suffix=".ts"):
    f = tmp_path / f"c{suffix}"
    f.write_text(body)
    return detect_ts_network_calls(f, tsx=suffix == ".tsx")


class TestJavaHttpClients:
    def test_rest_template_verbs(self, tmp_path):
        calls = _java(tmp_path, """
        class C {
          void m(String id) {
            restTemplate.getForObject("https://user-service.internal/v1/users/" + id, User.class);
            restTemplate.postForEntity("https://payment-service.internal/v1/charge", req, R.class);
            restTemplate.delete("https://payment-service.internal/v1/orders/" + id);
          }
        }
        """)
        assert "GET /v1/users/{}" in calls
        assert "POST /v1/charge" in calls
        assert "DELETE /v1/orders/{}" in calls
        assert calls["POST /v1/charge"].target.startswith("https://payment-service")

    def test_exchange_reads_httpmethod(self, tmp_path):
        calls = _java(tmp_path, """
        class C { void m() {
          restTemplate.exchange("https://inv.internal/v1/stock", HttpMethod.PUT, e, R.class);
        }}
        """)
        assert "PUT /v1/stock" in calls

    def test_webclient_uri(self, tmp_path):
        calls = _java(tmp_path, """
        class C { Mono<U> m(String id) {
          return webClient.get().uri("/v1/users/{id}", id).retrieve().bodyToMono(U.class);
        }}
        """)
        assert "GET /v1/users/{id}" in calls

    def test_var_resolved_base(self, tmp_path):
        calls = _java(tmp_path, """
        class C {
          static final String BASE = "https://svc.internal";
          void m() { restTemplate.getForObject(BASE + "/v1/ping", R.class); }
        }
        """)
        assert "GET /v1/ping" in calls
        assert calls["GET /v1/ping"].target.startswith("https://svc.internal")


class TestSoapConsumer:
    def test_spring_ws_template(self, tmp_path):
        calls = _java(tmp_path, """
        class C {
          WebServiceTemplate webServiceTemplate;
          Object m(Req r) {
            return webServiceTemplate.marshalSendAndReceive("https://legacy-billing.internal/ws/bill", r);
          }
        }
        """)
        soap = [e for e in calls.values() if e.call_type == "soap"]
        assert soap and soap[0].target.startswith("https://legacy-billing")


class TestEnvAndWrappedClients:
    def test_python_getenv_var(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text(
            "import os, requests\n"
            "url = os.getenv('PAYMENTS_URL')\n"
            "def f(oid):\n"
            "    return requests.delete(f'{url}/v1/orders/{oid}')\n"
        )
        edges = detect_python_network_calls(f)
        e = next(e for e in edges if e.method == "DELETE")
        # env name becomes the resolvable host; path is clean (no {url} leak)
        assert e.target.startswith("$ENV:PAYMENTS_URL")
        assert e.path == "/v1/orders/{oid}"

    def test_ts_process_env_var(self, tmp_path):
        edges = _ts(tmp_path,
                    "const U = process.env.USER_URL\n"
                    "export const g = (id) => axios.get(`${U}/v1/users/${id}`)\n")
        e = next(e for e in edges if e.method == "GET")
        assert e.target.startswith("$ENV:USER_URL")
        assert e.path == "/v1/users/{id}"

    def test_axios_create_instance(self, tmp_path):
        edges = _ts(tmp_path,
                    "const api = axios.create({ baseURL: 'https://inv.internal/v2' })\n"
                    "export const g = () => api.post('/items', {})\n")
        e = next(e for e in edges if e.method == "POST")
        assert e.target == "https://inv.internal/v2/items"

    def test_fetch_method_option(self, tmp_path):
        edges = _ts(tmp_path,
                    "export const d = (id) => "
                    "fetch(`https://b.internal/v1/invoices/${id}`, { method: 'DELETE' })\n")
        assert any(e.method == "DELETE" for e in edges)


class TestJaxRsAndMounts:
    def test_jaxrs_routes(self, tmp_path):
        f = tmp_path / "R.java"
        f.write_text("""
        @Path("/v1/accounts")
        public class R {
          @GET @Path("/{id}") public A get(String id){return null;}
          @POST public A create(A a){return null;}
        }
        """)
        keys = {f"{r.method} {r.path}" for r in detect_java_routes(f)}
        assert "GET /v1/accounts/{}" in keys
        assert "POST /v1/accounts" in keys

    def test_express_mount_prefix(self, tmp_path):
        f = tmp_path / "app.js"
        f.write_text(
            "const app=express(); const r=express.Router()\n"
            "r.get('/:id', (q,s)=>{})\n"
            "app.use('/api/orders', r)\n"
        )
        keys = {f"{x.method} {x.path}" for x in detect_ts_routes(f)}
        assert "GET /api/orders/{}" in keys


class TestEndpointScopedImpact:
    def _store(self, tmp_path):
        store = SymbolStore(tmp_path / "t.db")
        surf = lambda m, p: {"channel": "http", "method": m, "path": p,
                             "key": f"{m} {p}", "matched": True, "evidence": "x:1"}
        store.replace_dependencies([
            DependencyEdge("order", "user", ["http"], 1.0, [], [surf("GET", "/v1/users/{}")]),
            DependencyEdge("billing", "user", ["http"], 1.0, [], [surf("POST", "/v1/users")]),
        ])
        return store

    def test_unscoped_lists_all_dependents(self, tmp_path):
        rep = ImpactAnalyzer(self._store(tmp_path)).analyze("user")
        assert {d["repo"] for d in rep.direct_dependents} == {"order", "billing"}

    def test_endpoint_scope_is_surgical(self, tmp_path):
        rep = ImpactAnalyzer(self._store(tmp_path)).analyze(
            "user", endpoint="GET /v1/users/{id}")
        assert [d["repo"] for d in rep.direct_dependents] == ["order"]
        assert rep.endpoint == "GET /v1/users/{}"

    def test_endpoint_nobody_calls(self, tmp_path):
        rep = ImpactAnalyzer(self._store(tmp_path)).analyze(
            "user", endpoint="DELETE /v1/users/{id}")
        assert rep.direct_dependents == []

    def test_dependent_carries_called_endpoints(self, tmp_path):
        rep = ImpactAnalyzer(self._store(tmp_path)).analyze("user")
        order = next(d for d in rep.direct_dependents if d["repo"] == "order")
        assert "GET /v1/users/{}" in order["endpoints"]
