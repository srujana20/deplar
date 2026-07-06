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

    edge_list = []
    for e in edges:
        ensure(e.from_repo, external=e.from_repo not in repo_names)
        ensure(e.to_repo, external=e.to_repo not in repo_names)
        nodes[e.from_repo]["out"] += 1
        nodes[e.to_repo]["in"] += 1
        edge_list.append({
            "source": e.from_repo, "target": e.to_repo,
            "types": e.dep_types, "confidence": round(e.confidence, 2),
        })

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
  :root { color-scheme: light dark; --bg:#0f1115; --panel:#171a21; --line:#2a2f3a;
          --txt:#e6e8ec; --muted:#9aa3b2; --accent:#6ea8fe; --ext:#8a5cf6;
          --ok:#3ecf8e; --warn:#e0a03a; }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f7f8fa; --panel:#fff; --line:#e3e6ea; --txt:#1c2129;
            --muted:#5c6470; --accent:#2f6fed; }
  }
  * { box-sizing: border-box; }
  html,body { margin:0; height:100%; font-family: ui-sans-serif, system-ui, sans-serif;
              background:var(--bg); color:var(--txt); }
  #app { display:flex; height:100%; }
  #graph { flex:1; position:relative; }
  svg { width:100%; height:100%; display:block; cursor:grab; }
  svg.panning { cursor:grabbing; }
  .edge { stroke:var(--line); stroke-width:1.2; }
  .edge.low { stroke-dasharray:4 3; opacity:.6; }
  .edge.hi { stroke-width:2; }
  .node circle { stroke:var(--bg); stroke-width:2; cursor:pointer; }
  .node.repo circle { fill:var(--accent); }
  .node.ext circle { fill:var(--ext); }
  .node.dim { opacity:.15; }
  .node text { fill:var(--txt); font-size:11px; pointer-events:none;
               paint-order:stroke; stroke:var(--bg); stroke-width:3px; }
  #bar { position:absolute; top:12px; left:12px; right:12px; display:flex; gap:10px;
         align-items:center; flex-wrap:wrap; z-index:5; }
  #bar input[type=search]{ padding:7px 10px; border-radius:8px; border:1px solid var(--line);
         background:var(--panel); color:var(--txt); min-width:200px; }
  .pill { background:var(--panel); border:1px solid var(--line); border-radius:8px;
          padding:6px 10px; font-size:12px; color:var(--muted); display:flex;
          gap:8px; align-items:center; }
  button { background:var(--accent); color:#fff; border:0; border-radius:8px;
           padding:7px 12px; font-size:12px; cursor:pointer; }
  button.ghost { background:var(--panel); color:var(--txt); border:1px solid var(--line); }
  #side { width:340px; background:var(--panel); border-left:1px solid var(--line);
          padding:18px; overflow:auto; }
  #side h2 { font-size:16px; margin:0 0 2px; }
  #side .sub { color:var(--muted); font-size:12px; margin-bottom:14px; }
  #side h3 { font-size:12px; text-transform:uppercase; letter-spacing:.04em;
             color:var(--muted); margin:16px 0 6px; }
  #side ul { list-style:none; margin:0; padding:0; }
  #side li { font-size:13px; padding:3px 0; border-bottom:1px solid var(--line); }
  #side .empty { color:var(--muted); font-size:13px; }
  code { font-family: ui-monospace, monospace; font-size:12px; }
  .tag { font-size:11px; padding:1px 6px; border-radius:6px; border:1px solid var(--line);
         color:var(--muted); }
  .conf-hi { color:var(--ok); } .conf-lo { color:var(--warn); }
  .cmd { background:var(--bg); border:1px solid var(--line); border-radius:8px;
         padding:8px; font-family:ui-monospace,monospace; font-size:11px;
         word-break:break-all; margin-top:6px; }
</style>
</head>
<body>
<div id="app">
  <div id="graph">
    <div id="bar">
      <input id="search" type="search" placeholder="Search repos…">
      <label class="pill">min conf
        <input id="conf" type="range" min="0" max="100" value="0"> <span id="confv">0%</span>
      </label>
      <label class="pill"><input id="hideext" type="checkbox"> hide external</label>
      <button class="ghost" id="fit">Fit</button>
      <button id="reconcile" style="display:none">Reconcile</button>
      <span class="pill" id="stat"></span>
    </div>
    <svg id="svg"></svg>
  </div>
  <div id="side">
    <div class="empty" id="hint">Click a node to inspect its dependencies,
      blast radius and API surface.</div>
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
  layout();
  render();
  fit();
}

/* --- force layout (animated, cooling) --- */
let alpha=1, raf=null;
function layout(){ alpha=1; if(!raf) tick(); }
function tick(){
  const k=90, rep=9000;
  for(let i=0;i<NODES.length;i++){
    const a=NODES[i];
    for(let j=i+1;j<NODES.length;j++){
      const b=NODES[j];
      let dx=a.x-b.x, dy=a.y-b.y, d2=dx*dx+dy*dy||1, d=Math.sqrt(d2);
      const f=rep/d2; const fx=dx/d*f, fy=dy/d*f;
      a.vx+=fx; a.vy+=fy; b.vx-=fx; b.vy-=fy;
    }
    a.vx += (500-a.x)*0.002; a.vy += (400-a.y)*0.002;   // gravity
  }
  EDGES.forEach(e=>{
    const a=byId[e.source], b=byId[e.target];
    let dx=b.x-a.x, dy=b.y-a.y, d=Math.sqrt(dx*dx+dy*dy)||1;
    const f=(d-k)*0.02; const fx=dx/d*f, fy=dy/d*f;
    a.vx+=fx; a.vy+=fy; b.vx-=fx; b.vy-=fy;
  });
  NODES.forEach(n=>{
    if(n.fx!=null){ n.x=n.fx; n.y=n.fy; n.vx=0; n.vy=0; return; }
    n.vx*=0.85; n.vy*=0.85; n.x+=n.vx*alpha; n.y+=n.vy*alpha;
  });
  positions();
  alpha*=0.985;
  if(alpha>0.02) raf=requestAnimationFrame(tick); else raf=null;
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
  if(!NODES.length){ view={x:0,y:0,w:1000,h:800}; setView(); return; }
  let xs=NODES.map(n=>n.x), ys=NODES.map(n=>n.y);
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
function startDrag(ev,n){ ev.stopPropagation(); ev.preventDefault();
  drag={n, }; n.fx=n.x; n.fy=n.y; alpha=Math.max(alpha,0.3); if(!raf)tick(); }
svg.addEventListener("mousedown",ev=>{ pan={x:ev.clientX,y:ev.clientY,vx:view.x,vy:view.y};
  svg.classList.add("panning"); });
window.addEventListener("mousemove",ev=>{
  if(drag){ const p=toSvg(ev.clientX,ev.clientY); drag.n.fx=p.x; drag.n.fy=p.y; }
  else if(pan){ const r=svg.getBoundingClientRect();
    view.x=pan.vx-(ev.clientX-pan.x)/r.width*view.w;
    view.y=pan.vy-(ev.clientY-pan.y)/r.height*view.h; setView(); }
});
window.addEventListener("mouseup",()=>{ if(drag){drag.n.fx=null;drag.n.fy=null;}
  drag=null; pan=null; svg.classList.remove("panning"); });
svg.addEventListener("click",()=>clearSelect());

/* --- filters --- */
const confEl=document.getElementById("conf"), hideExtEl=document.getElementById("hideext"),
      searchEl=document.getElementById("search");
function applyFilters(){
  const minc=+confEl.value/100, hideExt=hideExtEl.checked,
        q=searchEl.value.trim().toLowerCase();
  edgeEls.forEach(ln=>{ const e=ln.__e;
    let show=e.confidence>=minc;
    if(hideExt && (byId[e.target].external||byId[e.source].external)) show=false;
    ln.style.display=show?"":"none"; });
  NODES.forEach(n=>{
    const g=nodeEls[n.id]; let show=true;
    if(hideExt && n.external) show=false;
    g.style.display=show?"":"none";
    g.classList.toggle("dim", q && !n.id.toLowerCase().includes(q));
  });
}
confEl.addEventListener("input",()=>{ document.getElementById("confv").textContent=confEl.value+"%"; applyFilters(); });
hideExtEl.addEventListener("change",applyFilters);
searchEl.addEventListener("input",applyFilters);
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
  document.getElementById("hint").style.display=""; }
function selectNode(n){
  selected=n;
  document.getElementById("hint").style.display="none";
  const d=document.getElementById("detail"); d.style.display="";
  const outs=EDGES.filter(e=>e.source===n.id),
        ins=EDGES.filter(e=>e.target===n.id),
        br=blast(n.id);
  const conf=c=>`<span class="${c>=0.8?'conf-hi':'conf-lo'}">${Math.round(c*100)}%</span>`;
  const li=(txt)=>`<li>${txt}</li>`;
  const list=(arr,fn)=>arr.length?arr.map(fn).join(""):'<li class="empty">none</li>';
  d.innerHTML = `
    <h2>${n.id} ${n.external?'<span class="tag">external</span>':''}</h2>
    <div class="sub">${n.external?'unresolved / external dependency':
        (n.languages.join(', ')||'—')}</div>
    ${n.external?'':`<h3>Aliases</h3><ul>${list(n.aliases,a=>li(
        `<code>${a.alias}</code> <span class="tag">${a.source}</span>`))}</ul>`}
    <h3>Calls (${outs.length})</h3>
    <ul>${list(outs,e=>li(`→ ${e.target} <span class="tag">${e.types.join(', ')}</span> ${conf(e.confidence)}`))}</ul>
    <h3>Called by (${ins.length})</h3>
    <ul>${list(ins,e=>li(`← ${e.source} <span class="tag">${e.types.join(', ')}</span> ${conf(e.confidence)}`))}</ul>
    <h3>Blast radius (${br.length})</h3>
    <ul>${list(br,r=>li('⚠ '+r))}</ul>
    ${n.external?'':`<h3>API surface (${n.symbols.length})</h3>
    <ul>${list(n.symbols.slice(0,30),s=>li(
        `<code>${s.signature||s.name}</code><br><span class="tag">${s.kind}</span> ${s.file}:${s.line}`))}</ul>
    <h3>Coordinated workspace</h3>
    <div class="cmd">deplar workspace ${n.id} --out ./workspace</div>`}
  `;
  NODES.forEach(x=>nodeEls[x.id].classList.toggle("dim",
    x.id!==n.id && !outs.some(e=>e.target===x.id) && !ins.some(e=>e.source===x.id)));
}
boot();
</script>
</body>
</html>
"""
