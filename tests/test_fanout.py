"""Endpoint fan-out: which external APIs each provided endpoint reaches."""
from pathlib import Path

from deplar.graph.symbol_store import SymbolStore
from deplar.scanner.fanout import compute_fanout
from deplar.scanner.org_scanner import OrgConfig, OrgScanner, RepoConfig
from deplar.scanner.resolver import DependencyEdge
from deplar.scanner.symbols import CallSite, Symbol

FX = Path(__file__).parent / "fixtures" / "fanout_repo"


def _cfg():
    return OrgConfig(repos=[RepoConfig(path=FX.resolve(), name="pss")])


class TestComputeFanoutUnit:
    def _fixture(self):
        # Controller.handler -> Service.work -> Client.send() [outbound]
        symbols = [
            Symbol("r", "Ctrl.java", "java", "method", "handler", "Ctrl.handler",
                   "", "Ctrl", 5, 8),
            Symbol("r", "Svc.java", "java", "method", "work", "Svc.work",
                   "", "Svc", 3, 6),
            Symbol("r", "Cl.java", "java", "method", "send", "Cl.send",
                   "", "Cl", 3, 6),
        ]
        calls = [
            CallSite("r", "Ctrl.java", "Ctrl.handler", "svc.work", "work", 6),
            CallSite("r", "Svc.java", "Svc.work", "cl.send", "send", 4),
        ]

        class R:  # minimal RouteEdge stand-in
            def __init__(self):
                self.source_file = Path("Ctrl.java"); self.line_number = 6
                self.method = "POST"; self.path = "/x"
        out = [DependencyEdge("r", "doli", ["http"], 1.0, surfaces=[
            {"channel": "http", "method": "POST", "path": "/create",
             "key": "POST /create", "matched": True, "evidence": "Cl.java:5"}])]
        return symbols, calls, [R()], out

    def test_traces_through_call_graph(self):
        symbols, calls, routes, out = self._fixture()
        fo = compute_fanout("r", symbols, calls, routes, out)
        calls_out = fo["POST /x"]
        assert len(calls_out) == 1
        c = calls_out[0]
        assert c["target"] == "doli" and c["key"] == "POST /create"
        assert c["hops"] == 2
        assert c["via"] == ["handler", "work", "send"]

    def test_confidence_decays_with_hops(self):
        symbols, calls, routes, out = self._fixture()
        fo = compute_fanout("r", symbols, calls, routes, out)
        assert fo["POST /x"][0]["confidence"] == 0.6   # 0.9 - 0.15*2

    def test_no_route_no_fanout(self):
        symbols, calls, _, out = self._fixture()
        assert compute_fanout("r", symbols, calls, [], out) == {}

    def test_max_hops_cuts_deep_chains(self):
        symbols, calls, routes, out = self._fixture()
        assert compute_fanout("r", symbols, calls, routes, out, max_hops=1) == {}


class TestEndToEnd:
    def test_fixture_fans_out_to_external(self):
        sc = OrgScanner()
        sc.scan_org(_cfg())
        prov = {p["path"]: p for p in sc.last_manifest["pss"]["provides"]["http"]}

        create = prov["/pss-api/create-pnr"]
        assert len(create["calls"]) == 1
        c = create["calls"][0]
        assert c["target"] == "doli.ek.com"
        assert c["key"] == "POST /create-pnr"
        assert c["hops"] == 2
        assert c["via"] == ["createPnr", "createPnrRecord", "postCreate"]

        # the pure-read endpoint fans out to nothing
        assert prov["/pss-api/pnr/{}"]["calls"] == []

    def test_persisted_and_read_back(self, tmp_path):
        store = SymbolStore(tmp_path / "t.db")
        OrgScanner().scan_org(_cfg(), store=store)
        rows = store.route_calls_for_repo("pss")
        assert any(r["ep_path"] == "/pss-api/create-pnr"
                   and r["target"] == "doli.ek.com" and r["key"] == "POST /create-pnr"
                   for r in rows)
        store.close()
