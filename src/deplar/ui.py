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
  :root { color-scheme: light dark; --bg:#0d0f14; --bg2:#12151c; --panel:#161a22;
          --panel2:#1c212b; --line:#272d39; --txt:#e8eaf0; --muted:#8b94a6;
          --accent:#6ea8fe; --ext:#a97cf8;
          --ok:#3ecf8e; --warn:#e0a03a; --bad:#f0736a;
          --get:#3ecf8e; --post:#6ea8fe; --put:#e0a03a; --del:#f0736a; --any:#8b94a6; }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f4f6f9; --bg2:#eef1f5; --panel:#fff; --panel2:#f7f9fc;
            --line:#e3e6ea; --txt:#1c2129; --muted:#69707c; --accent:#2f6fed; }
  }
  * { box-sizing: border-box; }
  html,body { margin:0; height:100%; font-family: ui-sans-serif, system-ui, -apple-system, sans-serif;
              background:var(--bg); color:var(--txt); font-size:14px; }
  #app { display:flex; height:100%; }
  #graph { flex:1; position:relative;
           background:radial-gradient(circle at 30% 20%, var(--bg2), var(--bg) 70%); }
  svg { width:100%; height:100%; display:block; cursor:grab; }
  svg.panning { cursor:grabbing; }
  .edge { stroke:var(--line); stroke-width:1.2; transition:stroke .15s; }
  .edge.low { stroke-dasharray:4 3; opacity:.55; }
  .edge.hi { stroke-width:2; }
  .edge.hot { stroke:var(--accent); stroke-width:2.4; opacity:1; }
  .node circle { stroke:var(--bg); stroke-width:2.5; cursor:pointer; transition:filter .15s; }
  .node.repo circle { fill:var(--accent); }
  .node.ext circle { fill:var(--ext); }
  .node.dim { opacity:.12; }
  .node.sel circle { stroke:#fff; stroke-width:3; filter:drop-shadow(0 0 6px var(--accent)); }
  .node text { fill:var(--txt); font-size:11px; font-weight:500; pointer-events:none;
               paint-order:stroke; stroke:var(--bg); stroke-width:3px; }
  #bar { position:absolute; top:60px; left:14px; right:14px; display:flex; gap:9px;
         align-items:center; flex-wrap:wrap; z-index:5; }
  #bar input[type=search]{ padding:8px 12px; border-radius:9px; border:1px solid var(--line);
         background:var(--panel); color:var(--txt); min-width:210px; outline:none; }
  #bar input[type=search]:focus{ border-color:var(--accent); }
  .pill { background:var(--panel); border:1px solid var(--line); border-radius:9px;
          padding:7px 11px; font-size:12px; color:var(--muted); display:flex;
          gap:8px; align-items:center; box-shadow:0 1px 3px rgba(0,0,0,.15); }
  button { background:var(--accent); color:#fff; border:0; border-radius:9px;
           padding:8px 13px; font-size:12px; font-weight:600; cursor:pointer; }
  button:hover { filter:brightness(1.08); }
  button.ghost { background:var(--panel); color:var(--txt); border:1px solid var(--line); }
  #side { width:392px; background:var(--panel); border-left:1px solid var(--line);
          padding:20px 20px 40px; overflow:auto; }
  #side h2 { font-size:18px; margin:0 0 3px; display:flex; align-items:center; gap:8px; }
  #side .sub { color:var(--muted); font-size:12.5px; margin-bottom:8px; }
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
  /* view tabs */
  #tabs { position:absolute; top:14px; left:14px; z-index:6; display:flex; gap:4px;
          background:var(--panel); border:1px solid var(--line); border-radius:10px;
          padding:4px; box-shadow:0 2px 8px rgba(0,0,0,.2); }
  .tab { padding:6px 14px; border-radius:7px; font-size:12.5px; font-weight:600;
         color:var(--muted); cursor:pointer; border:0; background:transparent; }
  .tab.active { background:var(--accent); color:#fff; }
  #repopick { display:none; }
  #repopick select { padding:7px 10px; border-radius:9px; border:1px solid var(--line);
         background:var(--panel); color:var(--txt); font-size:12px; }
  /* clickable repo links in the panel */
  .rlink { color:var(--accent); cursor:pointer; text-decoration:none; }
  .rlink:hover { text-decoration:underline; }
  .impact { background:rgba(240,115,106,.08); border:1px solid rgba(240,115,106,.3);
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
      <span id="repopick" class="pill">repo
        <select id="reposel"></select>
      </span>
      <input id="search" type="search" placeholder="Search repos…">
      <label class="pill">min conf
        <input id="conf" type="range" min="0" max="100" value="0"> <span id="confv">0%</span>
      </label>
      <label class="pill"><input id="hideext" type="checkbox"> hide external</label>
      <label class="pill"><input id="showimports" type="checkbox"> show imports</label>
      <button class="ghost" id="fit">Fit</button>
      <button id="reconcile" style="display:none">Reconcile</button>
      <span class="pill" id="stat"></span>
    </div>
    <svg id="svg"></svg>
    <div class="legend">
      <span><i class="swatch" style="background:var(--accent)"></i>service</span>
      <span><i class="swatch" style="background:var(--ext)"></i>external</span>
      <span><i class="dot ok"></i>endpoint matched</span>
      <span><i class="dot gap"></i>unmatched / unused</span>
    </div>
  </div>
  <div id="side">
    <div class="empty" id="hint">Click a service to inspect the API it
      <b>provides</b>, the endpoints it <b>calls</b> on others, who <b>calls it</b>,
      and its blast radius.</div>
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
function render(){
  svg.innerHTML="";
  edgeEls=[]; nodeEls={};
  EDGES.forEach(e=>{
    const ln=document.createElementNS(SVGNS,"line");
    ln.setAttribute("class","edge "+(e.confidence>=0.8?"hi":e.confidence<0.5?"low":""));
    ln.__e=e; svg.appendChild(ln); edgeEls.push(ln);
  });
  NODES.forEach(n=>{
    const g=document.createElementNS(SVGNS,"g");
    g.setAttribute("class","node "+(n.external?"ext":"repo"));
    const c=document.createElementNS(SVGNS,"circle");
    c.setAttribute("r", n.r);
    const t=document.createElementNS(SVGNS,"text");
    t.setAttribute("x", n.r+3); t.setAttribute("y", 4); t.textContent=n.id;
    g.appendChild(c); g.appendChild(t); svg.appendChild(g);
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
document.getElementById("fit").addEventListener("click",fit);
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
function clearSelect(){ selected=null;
  document.getElementById("detail").style.display="none";
  document.getElementById("hint").style.display="";
  NODES.forEach(x=>{ const g=nodeEls[x.id]; if(g){ g.classList.remove("dim","sel"); }});
  edgeEls.forEach(ln=>ln.classList.remove("hot"));
  applyFilters(); }
function esc(s){ return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function verbBadge(m){ const v=(m||'ANY').toLowerCase();
  const cls=['get','post','put','patch','delete','soap','any'].includes(v)?v:'any';
  return `<span class="verb ${cls}">${esc((m||'ANY').toUpperCase())}</span>`; }
function epRow(method, path, matched, note){
  const dot = matched===null ? '' : `<i class="dot ${matched?'ok':'gap'}" title="${matched?'matched to a provider route':'no matching provider route'}"></i>`;
  return `<div class="ep">${verbBadge(method)}<code>${esc(path||'/')}</code>${dot}${note?`<span class="mini">${esc(note)}</span>`:''}</div>`;
}
/* group: a peer repo + the endpoints exchanged with it. titleHtml is trusted HTML. */
function group(titleHtml, tag, conf, rowsHtml){
  const c = conf==null?'':`<span class="${conf>=0.8?'conf-hi':'conf-lo'}">${Math.round(conf*100)}%</span>`;
  return `<div class="grp"><div class="grp-h"><b>${titleHtml}</b>
    <span style="display:flex;gap:6px;align-items:center">${tag}${c}</span></div>
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
    const rows = (e.surfaces||[]).map(s=>epRow(s.method,s.path,s.matched, s.evidence)).join('');
    return group(`<span class="arrow">→</span> ${rlink(e.target)}`,
      `${tierChip(e.tier)}<span class="tag ${byId[e.target]&&byId[e.target].external?'ext':''}">${e.types.join(', ')}</span>`,
      e.confidence, rows);
  }).join('') : empty;

  // Called by — inbound edges, each with the endpoints the caller hits on us
  const calledHtml = ins.length ? ins.map(e=>{
    const rows = (e.surfaces||[]).map(s=>epRow(s.method,s.path,s.matched)).join('');
    return group(`<span class="arrow">←</span> ${rlink(e.source)}`,
      `<span class="tag">${e.types.join(', ')}</span>`, e.confidence, rows);
  }).join('') : empty;

  // Impact — who must be coordinated if this service changes
  const direct = ins.map(e=>e.source);
  const transitive = br.filter(r=>!direct.includes(r));
  const impactHtml = (direct.length||transitive.length) ? `<div class="impact">
      <div class="lvl">Directly affected — must update together</div>
      ${direct.length?direct.map(r=>`<div class="ep">⚠ ${rlink(r)}</div>`).join(''):'<div class="empty">none</div>'}
      ${transitive.length?`<div class="lvl">Transitive blast radius</div>`+
        transitive.map(r=>`<div class="ep">↳ ${rlink(r)}</div>`).join(''):''}
      <div class="cmd">deplar impact ${esc(n.id)}</div>
    </div>` : '<div class="empty">nothing depends on this — safe to change in isolation</div>';

  const aliasesHtml = (n.aliases||[]).length
    ? n.aliases.map(a=>`<span class="tag" title="${esc(a.raw)}">${esc(a.alias)} · ${esc(a.source)}</span>`).join(' ')
    : '<span class="empty">none</span>';

  d.innerHTML = `
    <h2>${esc(n.id)} ${n.external?'<span class="tag ext">external</span>':''}</h2>
    <div class="sub">${n.external?'unresolved / external dependency':
        (n.languages.join(', ')||'—')} · <b>${outs.length}</b> out · <b>${ins.length}</b> in</div>
    ${n.external?'':`<div style="margin-top:6px">${aliasesHtml}</div>
    <h3>Provides — API endpoints <span class="n">${provides.length}</span></h3>
    <div>${provHtml}</div>`}
    <h3>Calls <span class="n">${outs.length}</span></h3>
    <div>${callsHtml}</div>
    <h3>Called by <span class="n">${ins.length}</span></h3>
    <div>${calledHtml}</div>
    <h3>Impact — if you change this <span class="n">${direct.length + transitive.length}</span></h3>
    ${impactHtml}
    ${n.external?'':`<h3>Code symbols <span class="n">${n.symbols.length}</span>
      <span class="sym-toggle" id="symtog">show</span></h3>
    <div id="symbox" style="display:none">${n.symbols.length?n.symbols.slice(0,40).map(s=>
        `<div class="ep"><code>${esc(s.signature||s.name)}</code></div>
         <div class="mini" style="margin:-2px 0 4px 4px">${esc(s.kind)} · ${esc(s.file)}:${s.line}</div>`).join(''):empty}</div>
    <h3>Coordinated workspace</h3>
    <div class="cmd">deplar workspace ${esc(n.id)} --out ./workspace</div>`}
  `;
  const tog=document.getElementById("symtog");
  if(tog) tog.onclick=()=>{ const b=document.getElementById("symbox");
    const on=b.style.display==="none"; b.style.display=on?"":"none"; tog.textContent=on?"hide":"show"; };

  const near=new Set([n.id, ...outs.map(e=>e.target), ...ins.map(e=>e.source)]);
  NODES.forEach(x=>{ const g=nodeEls[x.id];
    g.classList.toggle("dim", !near.has(x.id));
    g.classList.toggle("sel", x.id===n.id); });
  edgeEls.forEach(ln=>{ const e=ln.__e;
    ln.classList.toggle("hot", e.source===n.id||e.target===n.id); });
}
boot();
</script>
</body>
</html>
"""
