import json
from pathlib import Path
from deplar.graph.manifest import DepsManifest
from deplar.graph.store import DependencyGraph
from deplar.scanner.resolver import DependencyEdge
import tempfile


def _make_deps_json(tmp_path) -> Path:
    g = DependencyGraph()
    g.add_dependency(DependencyEdge(
        from_repo="order-service",
        to_repo="payments-service",
        dep_types=["http"],
        confidence=1.0,
        evidence=["OrderService.java:47"],
    ))
    out = tmp_path / "deps.json"
    g.save(out, repo_name="order-service", repo_path="/tmp/order-service")
    return out


def test_manifest_loads(tmp_path):
    path = _make_deps_json(tmp_path)
    manifest = DepsManifest.load(path)
    assert manifest.version == "1.0"
    assert manifest.repo.name == "order-service"
    assert len(manifest.dependencies) == 1


def test_manifest_dependency_fields(tmp_path):
    path = _make_deps_json(tmp_path)
    manifest = DepsManifest.load(path)
    dep = manifest.dependencies[0]
    assert dep.to_repo == "payments-service"
    assert dep.confidence == 1.0
    assert "http" in dep.types


def test_evidence_parsed(tmp_path):
    path = _make_deps_json(tmp_path)
    manifest = DepsManifest.load(path)
    dep = manifest.dependencies[0]
    assert dep.evidence[0].file == "OrderService.java"
    assert dep.evidence[0].line == 47