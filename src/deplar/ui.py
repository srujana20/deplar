"""Interactive dependency-graph UI.

Turns the knowledge graph into a browsable, force-directed map. Two delivery
modes share one frontend:

- build_ui_data + render_html(..., served=False): a self-contained HTML file
  with the data embedded (works offline, no server).
- serve(): a stdlib-only local web server that serves the same page with live
  data from the store plus a reconcile action.

No external CDNs or JS libraries — the force layout and rendering are inline
vanilla JS/SVG, so the static file is portable and CSP-safe.
"""
import json
from pathlib import Path
from typing import Optional

from deplar.graph.symbol_store import SymbolStore


def build_ui_data(store: SymbolStore) -> dict:
    """Assemble the graph payload the frontend renders."""
    repos = store.list_repos()
    repo_names = {r["name"] for r in repos}
    edges = store.all_dependencies()

    # languages + aliases + a capped API surface per known repo
    aliases_by_repo: dict[str, list] = {}
    for a in store.all_aliases():
        aliases_by_repo.setdefault(a["repo"], []).append(a)

    nodes: dict[str, dict] = {}

    def ensure(name: str, external: bool):
        if name not in nodes:
            nodes[name] = {
                "id": name, "external": external, "languages": [],
                "in": 0, "out": 0, "aliases": [], "symbols": [], "path": "",
                "provides": [], "consumes": [], "consumed_keys": [],
            }
        return nodes[name]

    for r in repos:
        n = ensure(r["name"], external=False)
        n["path"] = r.get("path", "")
        n["aliases"] = [
            {"alias": a["alias"], "raw": a["raw"], "source": a["source"]}
            for a in aliases_by_repo.get(r["name"], [])
        ]
        syms = store.symbols_for_repo(
            r["name"], kinds=["class", "interface", "method", "function"], limit=40)
        n["symbols"] = [
            {"kind": s["kind"], "name": s["qualified_name"] or s["name"],
             "signature": s["signature"], "file": s["file"],
             "line": s["start_line"]}
            for s in syms
        ]
        n["languages"] = sorted({s["language"] for s in syms if s.get("language")})
        # HTTP routes this repo serves (what it provides)
        n["provides"] = [
            {"method": rt["method"] or "ANY", "path": rt["path"] or "/",
             "framework": rt.get("framework", ""),
             "file": rt.get("file", ""), "line": rt.get("line", 0)}
            for rt in store.routes_for_repo(r["name"])
        ]

    def _surfaces(e) -> list:
        out = []
        for s in getattr(e, "surfaces", []) or []:
            out.append({
                "method": s.get("method", "ANY"), "path": s.get("path", ""),
                "key": s.get("key", ""), "matched": bool(s.get("matched")),
                "match_kind": s.get("match_kind", "none"),
                "evidence": s.get("evidence", ""),
            })
        return out

    edge_list = []
    for e in edges:
        ensure(e.from_repo, external=e.from_repo not in repo_names)
        ensure(e.to_repo, external=e.to_repo not in repo_names)
        nodes[e.from_repo]["out"] += 1
        nodes[e.to_repo]["in"] += 1
        surfaces = _surfaces(e)
        edge_list.append({
            "source": e.from_repo, "target": e.to_repo,
            "types": e.dep_types, "confidence": round(e.confidence, 2),
            "tier": getattr(e, "tier", ""),
            "surfaces": surfaces,
            # import-only edges (framework/lib imports) are noise for a call map
            "import_only": all(t == "import" for t in e.dep_types),
        })
        # outbound endpoints this repo calls on the target
        for s in surfaces:
            nodes[e.from_repo]["consumes"].append({**s, "target": e.to_repo})
        # remember which of the target's endpoints are actually consumed
        for s in surfaces:
            if s["matched"] and s["key"]:
                nodes[e.to_repo]["consumed_keys"].append(s["key"])

    # flag each provided route that some consumer actually calls
    from deplar.scanner.endpoints import endpoint_key
    for n in nodes.values():
        consumed = set(n.get("consumed_keys", []))
        for p in n["provides"]:
            p["consumed"] = endpoint_key(p["method"], p["path"]) in consumed
        n.pop("consumed_keys", None)

    return {"nodes": list(nodes.values()), "edges": edge_list}


def render_html(data: Optional[dict], served: bool = False,
                title: str = "deplar — dependency map") -> str:
    if served:
        data_json = "null"
    else:
        data_json = json.dumps(data or {"nodes": [], "edges": []})
        data_json = data_json.replace("</", "<\\/")  # guard against </script>
    return (_TEMPLATE
            .replace("__TITLE__", title)
            .replace("__MODE__", "server" if served else "static")
            .replace("__DATA__", data_json))


# --- stdlib web server ---------------------------------------------------

def serve(db: Path, host: str = "127.0.0.1", port: int = 8000):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    db = Path(db)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console quiet
            pass

        def _send(self, code, body, ctype):
            payload = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, render_html(None, served=True), "text/html")
            elif self.path == "/api/data":
                store = SymbolStore(db)
                try:
                    self._send(200, json.dumps(build_ui_data(store)),
                               "application/json")
                finally:
                    store.close()
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            if self.path == "/api/reconcile":
                from deplar.scanner.reconciler import AliasCatalog, Reconciler
                store = SymbolStore(db)
                try:
                    catalog = AliasCatalog.from_aliases(store.all_aliases())
                    resolved, stats = Reconciler().reconcile(
                        store.all_dependencies(), catalog)
                    store.clear_dependencies()
                    store.replace_dependencies(resolved)
                    self._send(200, json.dumps({
                        "resolved": stats.resolved, "merged": stats.merged,
                        "dropped_self": stats.dropped_self,
                        "unresolved": stats.unresolved,
                    }), "application/json")
                finally:
                    store.close()
            else:
                self._send(404, "not found", "text/plain")

    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { color-scheme: dark;
    --bg:oklch(0.16 0.012 260); --bg2:oklch(0.19 0.014 260);
    --panel:oklch(0.2 0.014 260); --panel2:oklch(0.26 0.016 260);
    --line:oklch(0.3 0.016 260); --txt:oklch(0.93 0.008 250);
    --muted:oklch(0.68 0.015 258); --accent:oklch(0.68 0.15 245);
    --ext:oklch(0.78 0.14 75); --ok:oklch(0.75 0.16 155);
    --warn:oklch(0.78 0.14 75); --bad:oklch(0.62 0.2 25);
    --glow:oklch(0.68 0.15 245 / 0.35);
    --get:var(--ok); --post:var(--accent); --put:var(--warn); --patch:var(--warn);
    --del:var(--bad); --any:var(--muted); }
  * { box-sizing: border-box; }
  html,body { margin:0; height:100%; font-family: ui-sans-serif, system-ui, -apple-system, sans-serif;
              background:var(--bg); color:var(--txt); font-size:14px; }
  #app { display:flex; height:100%; }
  #graph { flex:1; position:relative;
           background:radial-gradient(circle at 30% 20%, var(--bg2), var(--bg) 70%); }
  svg { width:100%; height:100%; display:block; cursor:grab; }
  svg.panning { cursor:grabbing; }
  .edge { stroke:var(--edge,oklch(0.42 0.02 260)); stroke-width:1.2; opacity:.85;
          transition:stroke .2s, opacity .2s; }
  .edge.low { stroke-dasharray:5 4; opacity:.5; }
  .edge.hi { stroke-width:2; }
  .edge.hot { stroke:var(--accent); stroke-width:2.4; opacity:1; }
  .edge.mute { opacity:.05; }
  .grp.epgrp { cursor:pointer; transition:box-shadow .15s; }
  .grp.epgrp:hover { box-shadow:inset 3px 0 0 var(--accent); }
  .node .core { stroke:var(--bg); stroke-width:2.5; cursor:pointer; }
  .node.repo .core { fill:url(#node-internal); }
  .node.ext .core { fill:url(#node-external); }
  .node .ring { display:none; fill:none; stroke:var(--accent); stroke-width:1.5; opacity:.55; }
  .node.sel .ring { display:inline; }
  .node.sel .core { filter:drop-shadow(0 0 7px var(--glow)); }
  .node { opacity:1; transition:opacity .2s; }
  .node.dim { opacity:.14; }
  .node text { fill:var(--txt); font-size:11px; font-weight:500; pointer-events:none;
               paint-order:stroke; stroke:var(--bg); stroke-width:3px; }
  .node.ext text { font-weight:400; }
  #bar { position:absolute; top:60px; left:14px; right:14px; display:flex; gap:9px;
         align-items:center; flex-wrap:wrap; z-index:5; }
  /* frosted card surface shared by the toolbar controls */
  .pill { background:color-mix(in oklch, var(--panel) 82%, transparent);
          border:1px solid var(--line); border-radius:12px; padding:8px 12px;
          font-size:12px; color:var(--muted); display:flex; gap:8px;
          align-items:center; backdrop-filter:blur(10px); }
  .searchbox { position:relative; display:flex; align-items:center; }
  .searchbox svg { position:absolute; left:11px; width:15px; height:15px;
          color:var(--muted); pointer-events:none; }
  #bar input[type=search]{ padding:9px 12px 9px 32px; border-radius:12px;
          border:1px solid var(--line); background:color-mix(in oklch, var(--panel) 82%, transparent);
          color:var(--txt); min-width:200px; outline:none; backdrop-filter:blur(10px); }
  #bar input[type=search]:focus{ border-color:var(--accent);
          box-shadow:0 0 0 3px color-mix(in oklch, var(--accent) 25%, transparent); }
  #bar select{ background:transparent; border:0; color:var(--txt); font-size:12px; outline:none; }
  button { background:var(--accent); color:var(--bg); border:0; border-radius:12px;
           padding:8px 13px; font-size:12px; font-weight:600; cursor:pointer;
           display:flex; align-items:center; gap:6px; }
  button:hover { filter:brightness(1.08); }
  button svg { width:15px; height:15px; }
  button.ghost { background:color-mix(in oklch, var(--panel) 82%, transparent);
           color:var(--muted); border:1px solid var(--line); backdrop-filter:blur(10px); }
  button.ghost:hover { color:var(--txt); filter:none; }
  /* branded logo chip */
  #logo { display:flex; align-items:center; gap:9px; }
  #logo .mark { width:24px; height:24px; border-radius:8px; display:flex;
          align-items:center; justify-content:center;
          background:color-mix(in oklch, var(--accent) 16%, transparent);
          color:var(--accent); box-shadow:inset 0 0 0 1px color-mix(in oklch, var(--accent) 30%, transparent); }
  #logo .mark svg{ width:15px; height:15px; }
  #logo b { font-size:14px; letter-spacing:-.01em; color:var(--txt); }
  #logo span { font-size:12px; color:var(--muted); }
  /* zoom controls (bottom-right) */
  #zoom { position:absolute; bottom:16px; right:16px; z-index:5; display:flex;
          flex-direction:column; background:color-mix(in oklch, var(--panel) 82%, transparent);
          border:1px solid var(--line); border-radius:12px; overflow:hidden; backdrop-filter:blur(10px); }
  #zoom button { background:transparent; color:var(--muted); border:0; border-radius:0;
          width:38px; height:36px; justify-content:center; }
  #zoom button:hover { background:var(--panel2); color:var(--txt); filter:none; }
  #zoom button + button { border-top:1px solid var(--line); }
  #side { width:392px; background:var(--panel); border-left:1px solid var(--line);
          padding:0; overflow:auto; }
  #hint { padding:40px 32px; text-align:center; }
  .insp-body { padding:4px 20px 40px; }
  #side .sub { color:var(--muted); font-size:12px; }
  #side h3 { font-size:11px; text-transform:uppercase; letter-spacing:.05em;
             color:var(--muted); margin:20px 0 8px; display:flex; gap:7px; align-items:center; }
  #side h3 .n { background:var(--panel2); border:1px solid var(--line); border-radius:20px;
                padding:0 7px; font-size:10px; }
  #side ul { list-style:none; margin:0; padding:0; }
  #side .empty { color:var(--muted); font-size:13px; padding:2px 0; }
  code { font-family: ui-monospace, "SF Mono", monospace; font-size:12px; }
  /* group card: a caller/callee repo with its endpoints */
  .grp { background:var(--panel2); border:1px solid var(--line); border-radius:11px;
         padding:9px 11px; margin-bottom:8px; }
  .grp-h { display:flex; align-items:center; gap:7px; justify-content:space-between;
           cursor:pointer; }
  .grp-h b { font-size:13px; }
  .grp-eps { margin-top:7px; display:flex; flex-direction:column; gap:5px; }
  .ep { display:flex; align-items:center; gap:8px; font-size:12px; }
  .ep code { color:var(--txt); }
  .verb { font-size:10px; font-weight:700; letter-spacing:.03em; padding:2px 6px;
          border-radius:5px; color:#0d0f14; min-width:44px; text-align:center; }
  .verb.get{background:var(--get)} .verb.post{background:var(--post)}
  .verb.put,.verb.patch{background:var(--put)} .verb.delete{background:var(--del)}
  .verb.any,.verb.soap{background:var(--any)}
  .dot { width:7px; height:7px; border-radius:50%; flex:none; }
  .dot.ok{background:var(--ok)} .dot.gap{background:var(--warn)}
  .mini { font-size:10.5px; color:var(--muted); }
  .tag { font-size:10.5px; padding:1px 7px; border-radius:6px; border:1px solid var(--line);
         color:var(--muted); }
  .tag.ext { color:var(--ext); border-color:var(--ext); }
  .badge { font-size:10px; padding:1px 7px; border-radius:20px; font-weight:600; }
  .badge.used { background:rgba(62,207,142,.16); color:var(--ok); }
  .badge.unused { background:rgba(224,160,58,.16); color:var(--warn); }
  .conf-hi { color:var(--ok); } .conf-lo { color:var(--warn); }
  .arrow { color:var(--muted); }
  .cmd { background:var(--bg); border:1px solid var(--line); border-radius:9px;
         padding:9px 10px; font-family:ui-monospace,monospace; font-size:11px;
         word-break:break-all; margin-top:6px; color:var(--muted); }
  .legend { position:absolute; bottom:14px; left:14px; display:flex; gap:12px;
            font-size:11px; color:var(--muted); background:var(--panel); padding:7px 12px;
            border:1px solid var(--line); border-radius:9px; z-index:5; }
  .legend span { display:flex; align-items:center; gap:5px; }
  .swatch { width:9px; height:9px; border-radius:50%; }
  .sym-toggle { cursor:pointer; color:var(--accent); font-size:12px; user-select:none; }
  /* inspector header + metric row (fan-out / fan-in / blast) */
  .insp-head { display:flex; align-items:flex-start; justify-content:space-between;
          gap:10px; padding:18px 20px 16px; border-bottom:1px solid var(--line); }
  .insp-title { display:flex; align-items:center; gap:8px; min-width:0; }
  .insp-title .hdot { width:10px; height:10px; border-radius:50%; flex:none; }
  .insp-title h2 { font-family:ui-monospace,monospace; font-size:16px; font-weight:600;
          margin:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .iconbtn { background:transparent; border:0; color:var(--muted); cursor:pointer;
          border-radius:8px; padding:5px; display:flex; }
  .iconbtn:hover { background:var(--panel2); color:var(--txt); filter:none; }
  .iconbtn.ok { color:var(--ok); }
  .iconbtn svg{ width:15px; height:15px; }
  .metrics { display:grid; grid-template-columns:repeat(3,1fr);
          border-bottom:1px solid var(--line); }
  .metric { display:flex; flex-direction:column; align-items:center; gap:2px; padding:12px 0; }
  .metric + .metric { border-left:1px solid var(--line); }
  .metric .v { font-family:ui-monospace,monospace; font-size:22px; font-weight:600;
          font-variant-numeric:tabular-nums; }
  .metric .l { font-size:10px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }
  .confb { font-family:ui-monospace,monospace; font-size:10px; font-weight:600;
          padding:1px 6px; border-radius:6px; }
  .confb.hi { color:var(--ok); background:color-mix(in oklch, var(--ok) 15%, transparent); }
  .confb.lo { color:var(--warn); background:color-mix(in oklch, var(--warn) 15%, transparent); }
  .navrow { display:flex; align-items:center; gap:8px; width:100%; text-align:left;
          background:transparent; border:0; color:var(--txt); border-radius:9px;
          padding:6px 8px; cursor:pointer; font-size:12px; }
  .navrow:hover { background:var(--panel2); filter:none; }
  .navrow code, .navrow .nm { font-family:ui-monospace,monospace; font-size:12px; color:var(--txt); }
  .navrow .r { margin-left:auto; display:flex; align-items:center; gap:6px; }
  /* view tabs */
  #tabs { position:absolute; top:14px; right:410px; z-index:6; display:flex; gap:4px;
          background:color-mix(in oklch, var(--panel) 82%, transparent);
          border:1px solid var(--line); border-radius:12px; padding:4px; backdrop-filter:blur(10px); }
  .tab { padding:6px 14px; border-radius:9px; font-size:12.5px; font-weight:600;
         color:var(--muted); cursor:pointer; border:0; background:transparent; }
  .tab:hover { color:var(--txt); filter:none; }
  .tab.active { background:var(--accent); color:var(--bg); }
  #repopick { display:none; }
  /* clickable repo links in the panel */
  .rlink { color:var(--accent); cursor:pointer; text-decoration:none; }
  .rlink:hover { text-decoration:underline; }
  .impact { background:color-mix(in oklch, var(--bad) 8%, transparent);
            border:1px solid color-mix(in oklch, var(--bad) 30%, transparent);
            border-radius:11px; padding:10px 12px; margin-top:4px; }
  .impact .lvl { font-size:11px; color:var(--muted); margin:6px 0 3px; }
</style>
</head>
<body>
<div id="app">
  <div id="graph">
    <div id="tabs">
      <button class="tab active" data-mode="org">Org graph</button>
      <button class="tab" data-mode="repo">Single repo</button>
    </div>
    <div id="bar">
      <span class="pill" id="logo">
        <span class="mark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="5" r="2.5"/><circle cx="6" cy="19" r="2.5"/><circle cx="18" cy="19" r="2.5"/><path d="M12 7.5v4M12 11.5l-5 5M12 11.5l5 5"/></svg></span>
        <b>deplar</b><span>dependency map</span>
      </span>
      <span id="repopick" class="pill">repo <select id="reposel"></select></span>
      <span class="searchbox">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
        <input id="search" type="search" placeholder="Search services…">
      </span>
      <label class="pill">min conf
        <input id="conf" type="range" min="0" max="100" value="0"> <span id="confv">0%</span>
      </label>
      <label class="pill"><input id="hideext" type="checkbox"> hide external</label>
      <label class="pill"><input id="showimports" type="checkbox"> show imports</label>
      <button id="reconcile" style="display:none">Reconcile</button>
      <span class="pill" id="stat"></span>
    </div>
    <svg id="svg"></svg>
    <div id="zoom">
      <button id="zin" title="Zoom in" aria-label="Zoom in"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg></button>
      <button id="zout" title="Zoom out" aria-label="Zoom out"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 12h14"/></svg></button>
      <button id="zfit" title="Fit to screen" aria-label="Fit"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"/></svg></button>
    </div>
    <div class="legend">
      <span><i class="swatch" style="background:var(--accent)"></i>service</span>
      <span><i class="swatch" style="background:var(--ext)"></i>external</span>
      <span><i class="dot ok"></i>endpoint matched</span>
      <span><i class="dot gap"></i>unmatched / unused</span>
    </div>
  </div>
  <div id="side">
    <div id="hint">
      <div style="width:48px;height:48px;margin:0 auto 14px;border-radius:14px;
           display:flex;align-items:center;justify-content:center;
           background:var(--panel2);border:1px solid var(--line);color:var(--muted)">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="5" r="2.5"/><circle cx="6" cy="19" r="2.5"/><circle cx="18" cy="19" r="2.5"/><path d="M12 7.5v4M12 11.5l-5 5M12 11.5l5 5"/></svg>
      </div>
      <p style="color:var(--muted);font-size:13px;max-width:240px;margin:0 auto;line-height:1.5">
        Click a service to inspect the API it <b>provides</b>, the endpoints it
        <b>calls</b>, who <b>calls it</b>, and its blast radius.</p>
    </div>
    <div id="detail" style="display:none"></div>
  </div>
</div>
<script>
const EMBEDDED = __DATA__;
const MODE = "__MODE__";
const SVGNS = "http://www.w3.org/2000/svg";
const svg = document.getElementById("svg");
let NODES=[], EDGES=[], byId={}, adjOut={}, adjIn={}, selected=null;
let view={x:0,y:0,w:1000,h:800};
let viewMode="org", focusRepo=null;   // "org" = whole graph, "repo" = one service + neighbours

async function boot(){
  let data = EMBEDDED;
  if (MODE === "server") {
    document.getElementById("reconcile").style.display = "";
    data = await (await fetch("api/data")).json();
  }
  build(data);
}

function build(data){
  NODES = data.nodes.map((n,i)=>({...n,
    x: 500 + Math.cos(i*2.4)*250*Math.random()+ Math.random()*40,
    y: 400 + Math.sin(i*2.4)*250*Math.random()+ Math.random()*40,
    vx:0, vy:0, r: 6 + Math.min(18, (n.in||0)*3)}));
  byId={}; adjOut={}; adjIn={};
  NODES.forEach(n=>{ byId[n.id]=n; adjOut[n.id]=[]; adjIn[n.id]=[]; });
  EDGES = data.edges.filter(e=>byId[e.source]&&byId[e.target]);
  EDGES.forEach(e=>{ adjOut[e.source].push(e.target); adjIn[e.target].push(e.source); });
  document.getElementById("stat").textContent =
    NODES.length + " services · " + EDGES.length + " deps";
  populateRepoSelect();
  runLayout();     // settle synchronously so the first paint is stable
  render();
  fit();
}

function populateRepoSelect(){
  const sel=document.getElementById("reposel");
  const repos=NODES.filter(n=>!n.external).map(n=>n.id).sort();
  sel.innerHTML = repos.map(r=>`<option value="${r}">${r}</option>`).join("");
  if(!focusRepo && repos.length) focusRepo=repos[0];
}

/* --- force layout (synchronous, stability-guarded) --- */
function step(alpha){
  const k=90, rep=9000, MAXV=60;
  for(let i=0;i<NODES.length;i++){
    const a=NODES[i];
    for(let j=i+1;j<NODES.length;j++){
      const b=NODES[j];
      let dx=a.x-b.x, dy=a.y-b.y, d=Math.sqrt(dx*dx+dy*dy);
      if(d<0.01){ dx=(Math.random()-0.5); dy=(Math.random()-0.5); d=0.01; }
      const f=Math.min(rep/(d*d), 1000); const fx=dx/d*f, fy=dy/d*f;
      a.vx+=fx; a.vy+=fy; b.vx-=fx; b.vy-=fy;
    }
    a.vx += (500-a.x)*0.01; a.vy += (400-a.y)*0.01;   // gravity to centre
  }
  EDGES.forEach(e=>{
    const a=byId[e.source], b=byId[e.target];
    let dx=b.x-a.x, dy=b.y-a.y, d=Math.sqrt(dx*dx+dy*dy)||0.01;
    const f=(d-k)*0.04; const fx=dx/d*f, fy=dy/d*f;
    a.vx+=fx; a.vy+=fy; b.vx-=fx; b.vy-=fy;
  });
  NODES.forEach(n=>{
    n.vx=Math.max(-MAXV,Math.min(MAXV,n.vx))*0.85;
    n.vy=Math.max(-MAXV,Math.min(MAXV,n.vy))*0.85;
    n.x+=n.vx*alpha; n.y+=n.vy*alpha;
  });
}
function runLayout(){
  let alpha=1;
  for(let it=0; it<400; it++){ step(alpha); alpha*=0.99; }
}

/* --- render --- */
let edgeEls=[], nodeEls={};
const DEFS = `<defs>
  <radialGradient id="node-internal" cx="35%" cy="30%">
    <stop offset="0%" stop-color="oklch(0.82 0.11 245)"/>
    <stop offset="100%" stop-color="oklch(0.68 0.15 245)"/>
  </radialGradient>
  <radialGradient id="node-external" cx="35%" cy="30%">
    <stop offset="0%" stop-color="oklch(0.88 0.1 75)"/>
    <stop offset="100%" stop-color="oklch(0.78 0.14 75)"/>
  </radialGradient>
</defs>`;
function render(){
  svg.innerHTML=DEFS;
  edgeEls=[]; nodeEls={};
  EDGES.forEach(e=>{
    const ln=document.createElementNS(SVGNS,"line");
    ln.setAttribute("class","edge "+(e.confidence>=0.8?"hi":e.confidence<0.5?"low":""));
    ln.__e=e; svg.appendChild(ln); edgeEls.push(ln);
  });
  NODES.forEach(n=>{
    const g=document.createElementNS(SVGNS,"g");
    g.setAttribute("class","node "+(n.external?"ext":"repo"));
    const ring=document.createElementNS(SVGNS,"circle");
    ring.setAttribute("class","ring"); ring.setAttribute("r", n.r+4);
    const c=document.createElementNS(SVGNS,"circle");
    c.setAttribute("class","core"); c.setAttribute("r", n.r);
    const t=document.createElementNS(SVGNS,"text");
    t.setAttribute("x", n.r+4); t.setAttribute("y", 4); t.textContent=n.id;
    g.appendChild(ring); g.appendChild(c); g.appendChild(t); svg.appendChild(g);
    g.addEventListener("mousedown", ev=>startDrag(ev,n));
    g.addEventListener("click", ev=>{ ev.stopPropagation(); selectNode(n); });
    nodeEls[n.id]=g;
  });
  applyFilters();
  positions();
}
function positions(){
  edgeEls.forEach(ln=>{ const e=ln.__e,a=byId[e.source],b=byId[e.target];
    ln.setAttribute("x1",a.x);ln.setAttribute("y1",a.y);
    ln.setAttribute("x2",b.x);ln.setAttribute("y2",b.y); });
  NODES.forEach(n=>{ nodeEls[n.id].setAttribute("transform",`translate(${n.x},${n.y})`); });
}

/* --- viewbox zoom/pan --- */
function setView(){ svg.setAttribute("viewBox", `${view.x} ${view.y} ${view.w} ${view.h}`); }
function fit(){
  const vis=NODES.filter(n=>!nodeEls[n.id]||nodeEls[n.id].style.display!=="none");
  const pts=vis.length?vis:NODES;
  if(!pts.length){ view={x:0,y:0,w:1000,h:800}; setView(); return; }
  let xs=pts.map(n=>n.x), ys=pts.map(n=>n.y);
  let minx=Math.min(...xs)-60,maxx=Math.max(...xs)+60,
      miny=Math.min(...ys)-60,maxy=Math.max(...ys)+60;
  view={x:minx,y:miny,w:Math.max(200,maxx-minx),h:Math.max(200,maxy-miny)};
  setView();
}
svg.addEventListener("wheel",ev=>{ ev.preventDefault();
  const s=ev.deltaY>0?1.1:0.9;
  const pt=toSvg(ev.clientX,ev.clientY);
  view.x=pt.x-(pt.x-view.x)*s; view.y=pt.y-(pt.y-view.y)*s;
  view.w*=s; view.h*=s; setView();
},{passive:false});
function toSvg(px,py){ const r=svg.getBoundingClientRect();
  return {x:view.x+(px-r.left)/r.width*view.w, y:view.y+(py-r.top)/r.height*view.h}; }

/* --- drag (node + background pan) --- */
let drag=null, pan=null;
function startDrag(ev,n){ ev.stopPropagation(); ev.preventDefault(); drag={n}; }
svg.addEventListener("mousedown",ev=>{ pan={x:ev.clientX,y:ev.clientY,vx:view.x,vy:view.y};
  svg.classList.add("panning"); });
window.addEventListener("mousemove",ev=>{
  if(drag){ const p=toSvg(ev.clientX,ev.clientY); drag.n.x=p.x; drag.n.y=p.y; positions(); }
  else if(pan){ const r=svg.getBoundingClientRect();
    view.x=pan.vx-(ev.clientX-pan.x)/r.width*view.w;
    view.y=pan.vy-(ev.clientY-pan.y)/r.height*view.h; setView(); }
});
window.addEventListener("mouseup",()=>{ drag=null; pan=null;
  svg.classList.remove("panning"); });
svg.addEventListener("click",()=>clearSelect());

/* --- filters --- */
const confEl=document.getElementById("conf"), hideExtEl=document.getElementById("hideext"),
      showImpEl=document.getElementById("showimports"), searchEl=document.getElementById("search");
function repoScope(){
  // in repo mode, the visible set is the focused repo + the neighbours it is
  // connected to by *visible* edges (respects the import / min-conf filters)
  if(viewMode!=="repo" || !focusRepo) return null;
  const showImp=showImpEl.checked, minc=+confEl.value/100;
  const keep=new Set([focusRepo]);
  EDGES.forEach(e=>{
    if(e.source!==focusRepo && e.target!==focusRepo) return;
    if(!showImp && e.import_only) return;
    if(e.confidence<minc) return;
    keep.add(e.source); keep.add(e.target);
  });
  return keep;
}
function applyFilters(){
  const minc=+confEl.value/100, hideExt=hideExtEl.checked,
        showImp=showImpEl.checked, q=searchEl.value.trim().toLowerCase();
  const scope=repoScope();
  const degree={}; NODES.forEach(n=>degree[n.id]=0);
  edgeEls.forEach(ln=>{ const e=ln.__e;
    let show=e.confidence>=minc;
    if(!showImp && e.import_only) show=false;
    if(hideExt && (byId[e.target].external||byId[e.source].external)) show=false;
    if(scope && !(scope.has(e.source)&&scope.has(e.target))) show=false;
    ln.style.display=show?"":"none";
    if(show){ degree[e.source]++; degree[e.target]++; }
  });
  NODES.forEach(n=>{
    const g=nodeEls[n.id]; let show=true;
    if(hideExt && n.external) show=false;
    if(scope && !scope.has(n.id)) show=false;
    // drop external leaf nodes that lost all their edges (import noise)
    if(n.external && degree[n.id]===0 && !(scope&&scope.has(n.id))) show=false;
    g.style.display=show?"":"none";
    g.classList.toggle("dim", q && !n.id.toLowerCase().includes(q));
  });
  const shown=NODES.filter(n=>nodeEls[n.id].style.display!=="none").length,
        edg=edgeEls.filter(l=>l.style.display!=="none").length;
  document.getElementById("stat").textContent = shown+" services · "+edg+" deps";
}
confEl.addEventListener("input",()=>{ document.getElementById("confv").textContent=confEl.value+"%"; applyFilters(); });
hideExtEl.addEventListener("change",applyFilters);
showImpEl.addEventListener("change",()=>{ applyFilters(); if(selected) selectNode(selected); });
searchEl.addEventListener("input",applyFilters);

/* --- view tabs (org graph vs single repo) --- */
const reposel=document.getElementById("reposel");
function setMode(mode){
  viewMode=mode;
  document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("active", t.dataset.mode===mode));
  document.getElementById("repopick").style.display = mode==="repo"?"flex":"none";
  if(mode==="repo"){
    if(!focusRepo && reposel.options.length) focusRepo=reposel.value;
    reposel.value=focusRepo;
    applyFilters(); if(byId[focusRepo]) selectNode(byId[focusRepo]); fit();
  } else {
    clearSelect(); applyFilters(); fit();
  }
}
document.querySelectorAll(".tab").forEach(t=>t.addEventListener("click",()=>setMode(t.dataset.mode)));
reposel.addEventListener("change",()=>{ focusRepo=reposel.value;
  applyFilters(); if(byId[focusRepo]) selectNode(byId[focusRepo]); fit(); });
/* --- zoom controls (bottom-right) --- */
function zoomBy(factor){
  const cx=view.x+view.w/2, cy=view.y+view.h/2;
  view.w*=factor; view.h*=factor;
  view.x=cx-view.w/2; view.y=cy-view.h/2; setView();
}
document.getElementById("zin").addEventListener("click",()=>zoomBy(0.8));
document.getElementById("zout").addEventListener("click",()=>zoomBy(1.25));
document.getElementById("zfit").addEventListener("click",fit);
document.getElementById("reconcile").addEventListener("click",async()=>{
  const s=await (await fetch("api/reconcile",{method:"POST"})).json();
  await boot();
  document.getElementById("stat").textContent =
    `reconciled: ${s.resolved} resolved, ${s.merged} merged`;
});

/* --- selection + blast radius --- */
function blast(id){ const seen=new Set(); let front=[id];
  while(front.length){ const nx=[]; front.forEach(f=>(adjIn[f]||[]).forEach(p=>{
    if(!seen.has(p)&&p!==id){seen.add(p);nx.push(p);} })); front=nx; }
  return [...seen]; }
/* upstream dependents grouped by hop distance (GitNexus-style depth grouping) */
function blastDepths(id){ const depth={}, seen=new Set([id]); let front=[id], d=0;
  while(front.length){ d++; const nx=[];
    front.forEach(f=>(adjIn[f]||[]).forEach(p=>{
      if(!seen.has(p)){ seen.add(p); depth[p]=d; nx.push(p); } })); front=nx; }
  return depth; }
function clearSelect(){ selected=null;
  document.getElementById("detail").style.display="none";
  document.getElementById("hint").style.display="";
  NODES.forEach(x=>{ const g=nodeEls[x.id]; if(g){ g.classList.remove("dim","sel"); }});
  edgeEls.forEach(ln=>ln.classList.remove("hot","mute"));
  applyFilters(); }
/* default highlight for a selected node: dim non-neighbours, heat its edges */
function applySelectionHighlight(n,outs,ins){
  const near=new Set([n.id, ...outs.map(e=>e.target), ...ins.map(e=>e.source)]);
  NODES.forEach(x=>{ const g=nodeEls[x.id];
    g.classList.toggle("dim", !near.has(x.id));
    g.classList.toggle("sel", x.id===n.id); });
  edgeEls.forEach(ln=>{ const e=ln.__e; ln.classList.remove("mute");
    ln.classList.toggle("hot", e.source===n.id||e.target===n.id); });
}
/* isolate the callers of one endpoint: light only their edges into nodeId */
function spotlightCallers(nodeId, callerIds){
  const cs=new Set(callerIds);
  edgeEls.forEach(ln=>{ const e=ln.__e;
    const on=(e.target===nodeId && cs.has(e.source));
    ln.classList.toggle("hot", on); ln.classList.toggle("mute", !on); });
  NODES.forEach(x=>{ const g=nodeEls[x.id];
    g.classList.toggle("dim", x.id!==nodeId && !cs.has(x.id)); });
}
function esc(s){ return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function verbBadge(m){ const v=(m||'ANY').toLowerCase();
  const cls=['get','post','put','patch','delete','soap','any'].includes(v)?v:'any';
  return `<span class="verb ${cls}">${esc((m||'ANY').toUpperCase())}</span>`; }
function epRow(method, path, matched, note, kind){
  const prefix = kind==='prefix';
  const title = matched===null ? '' : (matched
      ? (prefix ? 'matched by path suffix — base-path/gateway prefix differs' : 'matched to a provider route')
      : 'no matching provider route');
  const dot = matched===null ? '' : `<i class="dot ${matched?'ok':'gap'}" title="${title}"></i>`;
  const approx = prefix ? '<span class="mini" title="'+title+'">≈</span>' : '';
  return `<div class="ep">${verbBadge(method)}<code>${esc(path||'/')}</code>${dot}${approx}${note?`<span class="mini">${esc(note)}</span>`:''}</div>`;
}
/* group: a peer repo + the endpoints exchanged with it. titleHtml is trusted HTML. */
function confBadge(c){ return c==null?'':
  `<span class="confb ${c>=0.8?'hi':'lo'}">${Math.round(c*100)}%</span>`; }
function group(titleHtml, tag, conf, rowsHtml){
  return `<div class="grp"><div class="grp-h"><b>${titleHtml}</b>
    <span style="display:flex;gap:6px;align-items:center">${tag}${confBadge(conf)}</span></div>
    ${rowsHtml?`<div class="grp-eps">${rowsHtml}</div>`:''}</div>`;
}
/* a clickable link to another service — the core "impact link" for navigation */
function rlink(id){
  const cls = byId[id] && byId[id].external ? 'rlink' : 'rlink';
  return `<span class="${cls}" onclick="goRepo('${String(id).replace(/'/g,"\\'")}')">${esc(id)}</span>`;
}
function goRepo(id){
  const n=byId[id]; if(!n) return;
  if(viewMode==="repo" && !n.external){ focusRepo=id; reposel.value=id; applyFilters(); fit(); }
  selectNode(n);
}

function selectNode(n){
  selected=n;
  document.getElementById("hint").style.display="none";
  const d=document.getElementById("detail"); d.style.display="";
  const showImp=showImpEl.checked;
  const outs=EDGES.filter(e=>e.source===n.id && (showImp||!e.import_only)),
        ins=EDGES.filter(e=>e.target===n.id && (showImp||!e.import_only)),
        br=blast(n.id);
  const empty='<div class="empty">none</div>';

  // Provides — the API this service serves
  const provides = (n.provides||[]);
  const provHtml = provides.length ? provides.map(p=>
      `<div class="ep">${verbBadge(p.method)}<code>${esc(p.path)}</code>
        <span class="badge ${p.consumed?'used':'unused'}">${p.consumed?'consumed':'unused'}</span>
        <span class="mini">${esc(p.framework||'')}${p.file?' · '+esc(p.file)+':'+p.line:''}</span></div>`
    ).join('') : empty;

  const tierChip=t=> t==='config'
      ? '<span class="tag" style="border-color:var(--accent);color:var(--accent)">config</span>'
      : t==='inferred' ? '<span class="tag" style="color:var(--warn)">inferred</span>' : '';

  // Calls — outbound edges, each with the endpoints hit on that target
  const callsHtml = outs.length ? outs.map(e=>{
    const rows = (e.surfaces||[]).map(s=>epRow(s.method,s.path,s.matched, s.evidence, s.match_kind)).join('');
    return group(`<span class="arrow">→</span> ${rlink(e.target)}`,
      `${tierChip(e.tier)}<span class="tag ${byId[e.target]&&byId[e.target].external?'ext':''}">${e.types.join(', ')}</span>`,
      e.confidence, rows);
  }).join('') : empty;

  // Called by — pivoted on the ENDPOINT each caller hits, not the caller. Two
  // services calling the same host on different endpoints land in different
  // groups, so a shared host never reads as "these callers are coupled".
  const epMap = new Map();   // "VERB /path" -> {method,path,callers:[]}
  const noEp = [];           // inbound edges with no http surface (kafka, unresolved)
  ins.forEach(e=>{
    const hs = (e.surfaces||[]).filter(s=>(s.method||s.path));
    if(!hs.length){ noEp.push(e); return; }
    hs.forEach(s=>{
      const k=(s.method||'ANY')+' '+(s.path||'/');
      let g=epMap.get(k); if(!g){ g={method:s.method,path:s.path,callers:[]}; epMap.set(k,g); }
      if(!g.callers.includes(e.source)) g.callers.push(e.source);
    });
  });
  const epGroups=[...epMap.values()];
  const calledHtml = (epGroups.length||noEp.length) ? (
    (epGroups.length?`<div class="mini" style="margin:-2px 0 6px">grouped by endpoint · hover a group to isolate its callers</div>`:'')
    + epGroups.map((g,i)=>`<div class="grp epgrp" id="epgrp-${i}">
        <div class="grp-h"><b>${verbBadge(g.method)}<code>${esc(g.path||'/')}</code></b>
          <span class="mini">${g.callers.length} caller${g.callers.length>1?'s':''}</span></div>
        <div class="grp-eps">${g.callers.map(c=>`<div class="ep"><span class="arrow">←</span> ${rlink(c)}</div>`).join('')}</div>
      </div>`).join('')
    + noEp.map(e=>group(`<span class="arrow">←</span> ${rlink(e.source)}`,
        `<span class="tag">${e.types.join(', ')}</span>`, e.confidence, '')).join('')
  ) : empty;

  // Impact — who must be coordinated if this service changes (grouped by hop depth)
  const depth = blastDepths(n.id);
  const byDepth = {};
  Object.keys(depth).forEach(r=>{ (byDepth[depth[r]]=byDepth[depth[r]]||[]).push(r); });
  const direct = (byDepth[1]||[]);
  const levels = Object.keys(byDepth).map(Number).sort((a,b)=>a-b);
  const impactHtml = levels.length ? `<div class="impact">
      ${levels.map(lv=>`
        <div class="lvl">${lv===1?'Directly affected — must update together'
                                 :'Hop '+lv+' — transitive blast radius'}</div>
        ${byDepth[lv].map(r=>`<div class="ep">${lv===1?'⚠':'↳'} ${rlink(r)}</div>`).join('')}
      `).join('')}
      <div class="cmd">deplar impact ${esc(n.id)}</div>
    </div>` : '<div class="empty">nothing depends on this — safe to change in isolation</div>';

  const aliasesHtml = (n.aliases||[]).length
    ? n.aliases.map(a=>`<div class="navrow" style="cursor:default">
        <code>${esc(a.alias)}</code><span class="r"><span class="tag">${esc(a.source)}</span></span></div>`).join('')
    : '<div class="empty">none</div>';

  const blastTone = br.length>=4?'var(--bad)':br.length>=1?'var(--warn)':'var(--ok)';
  const dotColor = n.external?'var(--ext)':'var(--accent)';
  const langTags = (!n.external && (n.languages||[]).length)
    ? `<div style="display:flex;gap:5px;flex-wrap:wrap;margin-top:8px">${
        n.languages.map(l=>`<span class="tag">${esc(l)}</span>`).join('')}</div>` : '';
  const cmd = `deplar workspace ${n.id} --out ./workspace`;

  d.innerHTML = `
    <div class="insp-head">
      <div style="min-width:0">
        <div class="insp-title">
          <span class="hdot" style="background:${dotColor}"></span>
          <h2>${esc(n.id)}</h2>
          ${n.external?'<span class="tag ext">external</span>':''}
        </div>
        <div class="sub" style="margin-top:4px">${n.external?'unresolved / external dependency':
            (n.path?esc(n.path):(n.languages.join(', ')||'—'))}</div>
        ${langTags}
      </div>
      <button class="iconbtn" id="inspclose" title="Close" aria-label="Close">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
      </button>
    </div>
    <div class="metrics">
      <div class="metric"><span class="v">${outs.length}</span><span class="l">fan-out</span></div>
      <div class="metric"><span class="v">${ins.length}</span><span class="l">fan-in</span></div>
      <div class="metric"><span class="v" style="color:${blastTone}">${br.length}</span><span class="l">blast</span></div>
    </div>
    <div class="insp-body">
    ${n.external?'':`<h3>Aliases <span class="n">${(n.aliases||[]).length}</span></h3>
    <div>${aliasesHtml}</div>
    <h3>Provides — API endpoints <span class="n">${provides.length}</span></h3>
    <div>${provHtml}</div>`}
    <h3>Calls <span class="n">${outs.length}</span></h3>
    <div>${callsHtml}</div>
    <h3>Called by <span class="n">${ins.length}</span></h3>
    <div>${calledHtml}</div>
    <h3>Impact — if you change this <span class="n">${Object.keys(depth).length}</span></h3>
    ${impactHtml}
    ${n.external?'':`<h3>Code symbols <span class="n">${n.symbols.length}</span>
      <span class="sym-toggle" id="symtog">show</span></h3>
    <div id="symbox" style="display:none">${n.symbols.length?n.symbols.slice(0,40).map(s=>
        `<div class="ep"><code>${esc(s.signature||s.name)}</code></div>
         <div class="mini" style="margin:-2px 0 4px 4px">${esc(s.kind)} · ${esc(s.file)}:${s.line}</div>`).join(''):empty}</div>
    <h3>Coordinated workspace</h3>
    <div class="cmd" style="display:flex;align-items:center;gap:8px">
      <code style="flex:1">${esc(cmd)}</code>
      <button class="iconbtn" id="copycmd" title="Copy command" aria-label="Copy" data-cmd="${esc(cmd)}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>
      </button></div>`}
    </div>
  `;
  const tog=document.getElementById("symtog");
  if(tog) tog.onclick=()=>{ const b=document.getElementById("symbox");
    const on=b.style.display==="none"; b.style.display=on?"":"none"; tog.textContent=on?"hide":"show"; };
  const cl=document.getElementById("inspclose"); if(cl) cl.onclick=clearSelect;
  const cp=document.getElementById("copycmd");
  if(cp) cp.onclick=()=>{ try{ navigator.clipboard.writeText(cp.dataset.cmd); cp.classList.add("ok");
    setTimeout(()=>cp.classList.remove("ok"),1200);}catch(e){} };

  applySelectionHighlight(n,outs,ins);
  // hover an endpoint group -> spotlight only the callers of that endpoint
  epGroups.forEach((g,i)=>{ const el=document.getElementById("epgrp-"+i); if(!el) return;
    el.addEventListener("mouseenter",()=>spotlightCallers(n.id,g.callers));
    el.addEventListener("mouseleave",()=>applySelectionHighlight(n,outs,ins));
  });
}
boot();
</script>
</body>
</html>
"""
