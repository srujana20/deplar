"""Endpoint fan-out — which external calls each provided endpoint reaches.

Surface matching answers "who calls B's `POST /charge`". This module answers the
mirror question inside a single repo: *when someone calls my `POST /create-pnr`,
which external APIs does that handler fan out to?* — e.g. a PSS controller whose
`createPnr` handler ends up calling `doli POST /create-pnr` and `payments POST
/charge`, two layers down through its service/client classes.

The join uses data the symbol extractor already produces: method line-spans
(to anchor an endpoint to its handler method and an outbound call to the method
that makes it) and call sites (caller -> callee_name) for the intra-repo call
graph. Reachability from the handler over that graph collects the outbound calls
it can hit.

Resolution is name-based, so it is approximate — Spring DI through interfaces,
overloads and same-named methods can both over- and under-connect. Every result
carries a hop count and a confidence that decays with distance; callers should
treat 2+-hop links as hints, not proof.
"""
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from deplar.scanner.endpoints import endpoint_key


@dataclass
class FanoutCall:
    target: str            # provider repo/host the handler reaches
    method: str            # outbound HTTP verb
    path: str              # outbound path
    key: str               # canonical "VERB /path"
    hops: int              # call-graph distance handler -> call site (0 = direct)
    confidence: float      # decays with hops
    matched: bool          # was the outbound call bound to a provider route
    via: List[str] = field(default_factory=list)  # handler..sink method chain

    def to_dict(self) -> dict:
        return asdict(self)


def _basename(f: str) -> str:
    return (f or "").replace("\\", "/").split("/")[-1]


def _confidence(hops: int) -> float:
    return round(max(0.4, 0.9 - 0.15 * hops), 2)


class _Encloser:
    """Innermost method/function symbol whose span contains a (file, line)."""

    def __init__(self, symbols):
        self._by_file: Dict[str, list] = {}
        for s in symbols:
            if s.kind in ("method", "function"):
                self._by_file.setdefault(_basename(s.file), []).append(s)

    def at(self, file: str, line: int) -> Optional[str]:
        best = None
        for s in self._by_file.get(_basename(file), []):
            if s.start_line <= line <= s.end_line:
                if best is None or (s.end_line - s.start_line) < (best.end_line - best.start_line):
                    best = s
        return best.qualified_name if best else None


def _call_graph(symbols, calls) -> Dict[str, set]:
    """caller-qualified -> set(callee-qualified), resolving callee_name by name."""
    name_to_q: Dict[str, set] = {}
    for s in symbols:
        if s.kind in ("method", "function"):
            name_to_q.setdefault(s.name, set()).add(s.qualified_name)
    adj: Dict[str, set] = {}
    for c in calls:
        for tgt in name_to_q.get(c.callee_name, ()):
            if tgt != c.caller:
                adj.setdefault(c.caller, set()).add(tgt)
    return adj


def _reach(adj: Dict[str, set], start: str, max_hops: int):
    """BFS distances + parents from `start`, capped at max_hops."""
    dist = {start: 0}
    parent: Dict[str, Optional[str]] = {start: None}
    frontier = [start]
    for _ in range(max_hops):
        nxt = []
        for n in frontier:
            for m in adj.get(n, ()):
                if m not in dist:
                    dist[m] = dist[n] + 1
                    parent[m] = n
                    nxt.append(m)
        frontier = nxt
        if not frontier:
            break
    return dist, parent


def _chain(parent: Dict[str, Optional[str]], q: str) -> List[str]:
    out = []
    while q is not None:
        out.append(q)
        q = parent.get(q)
    return list(reversed(out))


def compute_fanout(repo: str, symbols, calls, routes, out_edges,
                   max_hops: int = 4) -> Dict[str, List[dict]]:
    """Map each provided endpoint key -> the external calls its handler reaches.

    `out_edges` are the resolved DependencyEdges whose from_repo == repo; their
    http surfaces carry `evidence` as "File.ext:line" (the call site) plus the
    resolved target repo (edge.to_repo).
    """
    enc = _Encloser(symbols)
    adj = _call_graph(symbols, calls)

    # sink method (qualified) -> outbound calls made there
    sinks: Dict[str, list] = {}
    for e in out_edges:
        for s in e.surfaces:
            if s.get("channel") != "http":
                continue
            ev = s.get("evidence", "")
            base, _, ln = ev.rpartition(":")
            if not ln.isdigit():
                continue
            q = enc.at(base, int(ln))
            if q:
                sinks.setdefault(q, []).append((e.to_repo, s))
    if not sinks:
        return {}

    result: Dict[str, List[dict]] = {}
    for r in routes:
        handler = enc.at(str(r.source_file), r.line_number)
        if not handler:
            continue
        dist, parent = _reach(adj, handler, max_hops)
        # best (lowest-hop) fan-out call per (target, endpoint key)
        best: Dict[tuple, FanoutCall] = {}
        for q, d in dist.items():
            for target, s in sinks.get(q, []):
                k = (target, s.get("key", ""))
                fc = FanoutCall(
                    target=target, method=s.get("method", "ANY"),
                    path=s.get("path", ""), key=s.get("key", ""),
                    hops=d, confidence=_confidence(d),
                    matched=bool(s.get("matched")),
                    via=[qn.split(".")[-1] for qn in _chain(parent, q)],
                )
                if k not in best or d < best[k].hops:
                    best[k] = fc
        if best:
            ep = endpoint_key(r.method, r.path)
            result.setdefault(ep, [])
            result[ep].extend(
                sorted((c.to_dict() for c in best.values()),
                       key=lambda c: (c["hops"], c["target"], c["key"])))
    return result
