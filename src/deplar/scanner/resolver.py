import re
from dataclasses import dataclass, field
from typing import List

from deplar.scanner.ast_parser import FeignClientEdge, ImportEdge
from deplar.scanner.endpoints import endpoint_key
from deplar.scanner.network_detector import NetworkCallEdge, is_namespace_noise

# provenance tier ranking — the edge takes the strongest of its signals.
_TIER_RANK = {"call-site": 4, "config": 3, "inferred": 2, "import": 1, "": 0}


def _stronger_tier(a: str, b: str) -> str:
    return a if _TIER_RANK.get(a, 0) >= _TIER_RANK.get(b, 0) else b


# Import of a *value holder* — a class/module that carries constants, enums,
# DTOs, config or types, never a callable service. These are not dependencies
# even when their name overlaps a repo (e.g. `com.airline.pss.PnrConstants`
# would otherwise bind to the `pss` repo). Matched against the final segment of
# the import path across Java (a.b.C), Python (a.b.c) and TS (a/b/c).
_VALUE_HOLDER_SUFFIXES = (
    "constants", "constant", "const", "enums", "enum",
    "dtos", "dto", "models", "model", "entities", "entity",
    "configuration", "configs", "config", "settings", "properties",
    "exceptions", "exception", "errors", "error",
    "utils", "util", "helpers", "helper", "mappers", "mapper",
    "types", "type", "schemas", "schema", "vo", "pojo",
)


def _is_value_holder(module: str) -> bool:
    seg = re.split(r"[./\\]", (module or "").strip())[-1].lower()
    if not seg:
        return False
    return any(seg == s or seg.endswith(s) for s in _VALUE_HOLDER_SUFFIXES)


@dataclass
class DependencyEdge:
    from_repo: str
    to_repo: str
    dep_types: List[str]
    confidence: float
    evidence: List[str] = field(default_factory=list)
    # HTTP surfaces the consumer hits on the provider; each is
    # {"channel","method","path","key","matched","match_kind","evidence"}.
    # match_kind: "exact" | "prefix" (segment-suffix fallback) | "none".
    surfaces: List[dict] = field(default_factory=list)
    # strongest provenance tier across this edge's signals (see network_detector)
    tier: str = ""


def _normalize(name: str) -> str:
    """com.company.payments.Client -> payments"""
    name = name.strip().lower()
    # strip java package prefixes
    parts = name.split(".")
    # take the most meaningful part (last non-generic word)
    stopwords = {"client", "service", "impl", "api", "util", "helper"}
    meaningful = [p for p in parts if p not in stopwords]
    return meaningful[-1] if meaningful else parts[-1]


def _extract_service_from_url(url: str) -> str:
    """https://payments-service.internal/v1/charge -> payments-service"""
    # remove scheme
    url = re.sub(r'^https?://', '', url)
    # remove env var wrappers
    url = re.sub(r'^\$ENV:', '', url)
    # take hostname only
    host = url.split("/")[0].split(":")[0]
    # strip .internal, .svc.cluster.local etc
    host = re.sub(r'\.(internal|local|svc\.cluster\.local)$', '', host)
    return host or url


class DependencyResolver:
    def resolve(
        self,
        repo_name: str,
        import_edges: List[ImportEdge],
        feign_edges: List[FeignClientEdge],
        network_edges: List[NetworkCallEdge],
    ) -> List[DependencyEdge]:

        # bucket by (from, to) key
        buckets: dict[str, DependencyEdge] = {}

        def add(to: str, dep_type: str, confidence: float, evidence: str,
                surface: dict | None = None, tier: str = ""):
            to = _normalize(to) if dep_type == "import" else to
            key = f"{repo_name}::{to}"
            if key not in buckets:
                buckets[key] = DependencyEdge(
                    from_repo=repo_name,
                    to_repo=to,
                    dep_types=[],
                    confidence=0.0,
                    evidence=[],
                )
            e = buckets[key]
            if dep_type not in e.dep_types:
                e.dep_types.append(dep_type)
            e.confidence = max(e.confidence, confidence)
            e.evidence.append(evidence)
            e.tier = _stronger_tier(e.tier, tier)
            if surface and surface not in e.surfaces:
                e.surfaces.append(surface)

        def surface(method: str, path: str, evidence: str) -> dict:
            return {"channel": "http", "method": (method or "ANY"), "path": path,
                    "key": endpoint_key(method, path), "matched": False,
                    "evidence": evidence}

        for e in import_edges:
            # a constants/enum/DTO/config/type import is not a service dependency
            if _is_value_holder(e.imported_module):
                continue
            add(e.imported_module, "import", 0.6,
                f"{e.source_file.name}:{e.line_number}", tier="import")

        for e in feign_edges:
            target = e.client_name or _extract_service_from_url(e.url_pattern)
            ev = f"{e.source_file.name}:{e.line_number}"
            if e.surfaces:
                for verb, spath in e.surfaces:
                    add(target, "feign", 0.95, ev, surface(verb, spath, ev),
                        tier="call-site")
            else:
                add(target, "feign", 0.95, ev, tier="call-site")

        for e in network_edges:
            # safety net: a namespace/schema URI is never a real target
            if is_namespace_noise(e.target):
                continue
            target = _extract_service_from_url(e.target)
            ev = f"{e.source_file.name}:{e.line_number}"
            sfc = (surface(e.method, e.path, ev)
                   if e.call_type == "http" and e.path else None)
            add(target, e.call_type, e.confidence, ev, sfc,
                tier=getattr(e, "tier", "call-site"))

        # filter out noise — stdlib, generic names
        noise = {"os", "sys", "re", "json", "path", "typing",
                  "dataclasses", "pathlib", "unknown", repo_name}
        return [e for e in buckets.values() if e.to_repo not in noise]