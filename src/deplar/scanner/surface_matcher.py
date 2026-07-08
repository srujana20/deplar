"""Surface matching — join consumer calls to the provider routes they hit.

The reconciler binds an outbound call to the provider *repo* by identity. This
module goes one level deeper: given the routes each repo *provides*, it decides
whether a consumer's specific `(method, path)` actually matches an endpoint the
provider serves. That turns a coarse "A depends on B" into "A calls B's
`GET /v1/users/{}`" — which is what makes downstream impact analysis precise
(only flag A when the endpoint it calls is the one that changed).

It also assembles the per-repo interface manifest (provides + consumes) that is
deplar's first machine-readable picture of a service's contract surface.
"""
from dataclasses import dataclass
from typing import Dict, List

from deplar.scanner.endpoints import endpoint_key, normalize_path
from deplar.scanner.resolver import DependencyEdge
from deplar.scanner.route_detector import RouteEdge


@dataclass
class MatchStats:
    surfaces_total: int = 0
    surfaces_matched: int = 0        # consumer call bound to a provider route
    surfaces_prefix_matched: int = 0  # of those, bound via a segment-suffix fallback
    surfaces_unmatched: int = 0      # provider resolved, but route not found (gap)


def _verb_ok(verbs: set, method: str) -> bool:
    m = (method or "ANY").upper()
    return m == "ANY" or "ANY" in verbs or m in verbs


class RouteIndex:
    """Per-repo index of provided routes: path-template -> set of verbs."""

    def __init__(self):
        self._by_repo: Dict[str, Dict[str, set]] = {}
        self._routes: Dict[str, List[RouteEdge]] = {}

    def add(self, repo: str, route: RouteEdge):
        path = normalize_path(route.path)
        self._by_repo.setdefault(repo, {}).setdefault(path, set()).add(
            route.method.upper() or "ANY")
        self._routes.setdefault(repo, []).append(route)

    def routes_for(self, repo: str) -> List[RouteEdge]:
        return self._routes.get(repo, [])

    def match_kind(self, repo: str, method: str, path: str) -> str:
        """How a consumer's (method, path) binds to a route this repo provides:

            "exact"  — same normalized path, compatible verb
            "prefix" — one path is a segment-boundary suffix of the other, i.e.
                       they differ only by a leading prefix. This is the residual
                       context-path / gateway-rewrite case: a base we couldn't
                       resolve statically left one side longer than the other.
            ""       — no match

        The suffix fallback is scoped to a single repo's own routes (the edge is
        already bound to `repo` by identity), so `/create-pnr` can only match the
        provider it was resolved to — not every service that serves that path.
        """
        by = self._by_repo.get(repo)
        if not by:
            return ""
        p = normalize_path(path)

        verbs = by.get(p)
        if verbs and _verb_ok(verbs, method):
            return "exact"

        # segment-boundary suffix: since every normalized path starts with "/",
        # `longer.endswith(shorter)` can only align on a "/" boundary — so
        # "/x/create-pnr" matches "/create-pnr" but "/foo-create-pnr" does not.
        if p != "/":
            for rp, rverbs in by.items():
                if rp == "/" or not _verb_ok(rverbs, method):
                    continue
                longer, shorter = (p, rp) if len(p) >= len(rp) else (rp, p)
                if longer.endswith(shorter):
                    return "prefix"
        return ""

    def matches(self, repo: str, method: str, path: str) -> bool:
        return bool(self.match_kind(repo, method, path))


class SurfaceMatcher:
    def match(
        self, edges: List[DependencyEdge], index: RouteIndex
    ) -> MatchStats:
        """Annotate each edge's surfaces in place with `matched`."""
        stats = MatchStats()
        for edge in edges:
            for s in edge.surfaces:
                stats.surfaces_total += 1
                kind = index.match_kind(edge.to_repo, s.get("method", "ANY"),
                                        s.get("path", ""))
                s["matched"] = bool(kind)
                s["match_kind"] = kind or "none"
                if kind == "exact":
                    stats.surfaces_matched += 1
                elif kind == "prefix":
                    stats.surfaces_matched += 1
                    stats.surfaces_prefix_matched += 1
                else:
                    stats.surfaces_unmatched += 1
        return stats


def build_interface_manifest(
    repos: List[str],
    edges: List[DependencyEdge],
    index: RouteIndex,
) -> dict:
    """Per-repo picture of what each service provides and consumes.

    {
      "user-service": {
        "provides": {"http": [{"method","path","framework","file","line"}]},
        "consumes": {"http": [{"method","path","target","matched","evidence"}]}
      }, ...
    }
    """
    manifest: dict = {}
    for repo in repos:
        provides = [
            {"method": r.method, "path": normalize_path(r.path),
             "framework": r.framework, "file": r.source_file.name,
             "line": r.line_number}
            for r in index.routes_for(repo)
        ]
        provides.sort(key=lambda p: (p["path"], p["method"]))
        manifest[repo] = {
            "provides": {"http": provides},
            "consumes": {"http": []},
        }

    for edge in edges:
        bucket = manifest.setdefault(
            edge.from_repo,
            {"provides": {"http": []}, "consumes": {"http": []}},
        )
        for s in edge.surfaces:
            if s.get("channel") != "http":
                continue
            bucket["consumes"]["http"].append({
                "method": s.get("method", "ANY"),
                "path": normalize_path(s.get("path", "")),
                "key": endpoint_key(s.get("method", ""), s.get("path", "")),
                "target": edge.to_repo,
                "matched": s.get("matched", False),
                "match_kind": s.get("match_kind", "none"),
                "evidence": s.get("evidence", ""),
            })
    return manifest
