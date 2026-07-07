import re
from dataclasses import dataclass, field
from typing import List

from deplar.scanner.ast_parser import FeignClientEdge, ImportEdge
from deplar.scanner.endpoints import endpoint_key
from deplar.scanner.network_detector import NetworkCallEdge


@dataclass
class DependencyEdge:
    from_repo: str
    to_repo: str
    dep_types: List[str]
    confidence: float
    evidence: List[str] = field(default_factory=list)
    # HTTP surfaces the consumer hits on the provider; each is
    # {"channel","method","path","key","matched","evidence"}.
    surfaces: List[dict] = field(default_factory=list)


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
                surface: dict | None = None):
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
            if surface and surface not in e.surfaces:
                e.surfaces.append(surface)

        def surface(method: str, path: str, evidence: str) -> dict:
            return {"channel": "http", "method": (method or "ANY"), "path": path,
                    "key": endpoint_key(method, path), "matched": False,
                    "evidence": evidence}

        for e in import_edges:
            add(e.imported_module, "import", 0.6,
                f"{e.source_file.name}:{e.line_number}")

        for e in feign_edges:
            target = e.client_name or _extract_service_from_url(e.url_pattern)
            ev = f"{e.source_file.name}:{e.line_number}"
            if e.surfaces:
                for verb, spath in e.surfaces:
                    add(target, "feign", 0.95, ev, surface(verb, spath, ev))
            else:
                add(target, "feign", 0.95, ev)

        for e in network_edges:
            target = _extract_service_from_url(e.target)
            ev = f"{e.source_file.name}:{e.line_number}"
            sfc = (surface(e.method, e.path, ev)
                   if e.call_type == "http" and e.path else None)
            add(target, e.call_type, e.confidence, ev, sfc)

        # filter out noise — stdlib, generic names
        noise = {"os", "sys", "re", "json", "path", "typing",
                  "dataclasses", "pathlib", "unknown", repo_name}
        return [e for e in buckets.values() if e.to_repo not in noise]