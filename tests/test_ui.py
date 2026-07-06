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
