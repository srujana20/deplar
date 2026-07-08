from deplar.graph.symbol_store import SymbolStore
from deplar.scanner.resolver import DependencyEdge
from deplar.scanner.symbols import Symbol, SymbolIndex
from deplar.ui import build_ui_data, render_html


def _store(tmp_path):
    store = SymbolStore(tmp_path / "t.db")
    store.upsert_repo("order", "/repos/order", "2026-01-01T00:00:00Z")
    store.upsert_repo("payment", "/repos/payment", "2026-01-01T00:00:00Z")
    idx = SymbolIndex()
    idx.symbols.append(Symbol("payment", "p.py", "python", "class",
                              "Charger", "Charger", "Charger", "", 1, 9))
    store.replace_symbols("payment", idx)
    store.replace_dependencies([
        DependencyEdge("order", "payment", ["http"], 1.0, []),
        DependencyEdge("order", "requests", ["import"], 0.6, []),  # external
    ])
    return store


def test_build_ui_data_marks_repos_and_externals(tmp_path):
    data = build_ui_data(_store(tmp_path))
    nodes = {n["id"]: n for n in data["nodes"]}
    assert nodes["payment"]["external"] is False
    assert nodes["order"]["external"] is False
    assert nodes["requests"]["external"] is True   # not a scanned repo
    assert nodes["payment"]["in"] == 1
    assert nodes["order"]["out"] == 2


def test_build_ui_data_includes_symbols_and_languages(tmp_path):
    data = build_ui_data(_store(tmp_path))
    payment = next(n for n in data["nodes"] if n["id"] == "payment")
    assert payment["languages"] == ["python"]
    assert any(s["name"] == "Charger" for s in payment["symbols"])


def test_build_ui_data_edges(tmp_path):
    data = build_ui_data(_store(tmp_path))
    edge = next(e for e in data["edges"] if e["target"] == "payment")
    assert edge["source"] == "order"
    assert edge["types"] == ["http"]
    assert edge["confidence"] == 1.0


def test_render_html_static_embeds_data_and_replaces_tokens(tmp_path):
    data = build_ui_data(_store(tmp_path))
    html = render_html(data, served=False)
    assert "__DATA__" not in html and "__MODE__" not in html
    assert 'MODE = "static"' in html
    assert "payment" in html
    assert "<svg" in html


def test_render_html_server_mode_has_no_embedded_data():
    html = render_html(None, served=True)
    assert 'MODE = "server"' in html
    assert "const EMBEDDED = null;" in html


def _surface_store(tmp_path):
    store = SymbolStore(tmp_path / "t.db")
    store.upsert_repo("order", "/r/order", "2026-01-01T00:00:00Z")
    store.upsert_repo("user", "/r/user", "2026-01-01T00:00:00Z")
    from deplar.scanner.route_detector import RouteEdge
    from pathlib import Path as _P
    store.replace_routes("user", [
        RouteEdge(_P("routes.ts"), "GET", "/v1/users/{}", "express", 6),
        RouteEdge(_P("routes.ts"), "POST", "/users", "express", 10),
    ])
    store.replace_dependencies([
        DependencyEdge("order", "user", ["http"], 1.0, [], [
            {"channel": "http", "method": "GET", "path": "/v1/users/{}",
             "key": "GET /v1/users/{}", "matched": True, "evidence": "c.ts:3"}]),
    ])
    return store


def test_ui_data_carries_surfaces_and_provides(tmp_path):
    data = build_ui_data(_surface_store(tmp_path))
    nodes = {n["id"]: n for n in data["nodes"]}

    # edge carries the endpoint the consumer hits
    edge = next(e for e in data["edges"] if e["target"] == "user")
    assert edge["surfaces"][0]["key"] == "GET /v1/users/{}"
    assert edge["surfaces"][0]["matched"] is True

    # user PROVIDES its routes; the called one is flagged consumed, the other unused
    provides = {p["path"]: p for p in nodes["user"]["provides"]}
    assert provides["/v1/users/{}"]["consumed"] is True
    assert provides["/users"]["consumed"] is False

    # order CONSUMES that endpoint, tagged with its target
    assert nodes["order"]["consumes"][0]["target"] == "user"


def test_ui_html_renders_endpoint_widgets(tmp_path):
    html = render_html(build_ui_data(_surface_store(tmp_path)), served=False)
    assert "verbBadge" in html and "Provides — API endpoints" in html


def test_ui_html_has_tabs_and_impact_links(tmp_path):
    html = render_html(build_ui_data(_surface_store(tmp_path)), served=False)
    # org vs single-repo tabs + repo picker
    assert 'data-mode="org"' in html and 'data-mode="repo"' in html
    assert 'id="reposel"' in html
    # navigable impact links + impact section
    assert "function rlink(" in html and "function goRepo(" in html
    assert "Impact — if you change this" in html


def test_ui_html_has_redesign_chrome(tmp_path):
    html = render_html(build_ui_data(_surface_store(tmp_path)), served=False)
    # branded logo chip, zoom controls, metric row, radial-gradient nodes
    assert 'id="logo"' in html and "dependency map" in html
    assert 'id="zin"' in html and 'id="zout"' in html and 'id="zfit"' in html
    assert 'class="metric"' in html and "fan-out" in html and "blast" in html
    assert "node-internal" in html and "node-external" in html
    # gitnexus-style depth-grouped blast
    assert "function blastDepths(" in html
