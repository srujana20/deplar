import datetime as dt
import json
from pathlib import Path
from typing import List, Set

import networkx as nx

from deplar.scanner.resolver import DependencyEdge


class DependencyGraph:
    def __init__(self):
        self.g = nx.DiGraph()

    def add_dependency(self, edge: DependencyEdge):
        self.g.add_edge(
            edge.from_repo,
            edge.to_repo,
            dep_types=edge.dep_types,
            confidence=edge.confidence,
            evidence=edge.evidence,
        )

    def get_dependencies(self, repo: str) -> List[str]:
        return list(self.g.successors(repo))

    def get_dependents(self, repo: str) -> List[str]:
        return list(self.g.predecessors(repo))

    def blast_radius(self, repo: str, depth: int = 2) -> Set[str]:
        visited = set()
        frontier = {repo}
        for _ in range(depth):
            next_frontier = set()
            for node in frontier:
                for dep in self.g.predecessors(node):
                    if dep not in visited:
                        next_frontier.add(dep)
            visited |= frontier
            frontier = next_frontier
        visited.discard(repo)
        return visited

    def summary(self) -> dict:
        in_degree = sorted(self.g.in_degree(), key=lambda x: x[1], reverse=True)
        return {
            "total_nodes": self.g.number_of_nodes(),
            "total_edges": self.g.number_of_edges(),
            "most_depended_on": [n for n, _ in in_degree[:5]],
            "orphans": [n for n, d in self.g.in_degree() if d == 0],
        }

    def to_dict(self, repo_name: str = "", repo_path: str = "") -> dict:
        deps = []
        for u, v, data in self.g.edges(data=True):
            # convert evidence strings to objects
            evidence = []
            for e in data.get("evidence", []):
                if ":" in e:
                    file, line = e.rsplit(":", 1)
                    evidence.append({
                        "file": file,
                        "line": int(line) if line.isdigit() else 0,
                        "snippet": ""
                    })
                else:
                    evidence.append({"file": e, "line": 0, "snippet": ""})

            deps.append({
                "from":       u,
                "to":         v,
                "types":      data.get("dep_types", []),
                "confidence": data.get("confidence", 0.0),
                "evidence":   evidence,
                "metadata":   {},
            })

        summary = self.summary()
        high_conf = sum(1 for _, _, d in self.g.edges(data=True)
                        if d.get("confidence", 0) >= 0.8)

        return {
            "version":    "1.0",
            "scanned_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "repo": {
                "name":             repo_name,
                "path":             repo_path,
                "primary_language": "",
            },
            "dependencies": deps,
            "summary": {
                "total_deps":          len(deps),
                "high_confidence":     high_conf,
                "services_called":     summary["most_depended_on"],
                "services_calling_me": [],
            },
        }

    def save(self, path: Path, repo_name: str = "", repo_path: str = ""):
        path = Path(path)
        path.write_text(
            json.dumps(self.to_dict(repo_name, repo_path), indent=2)
        )

    def load(self, path: Path):
        data = json.loads(Path(path).read_text())
        for dep in data["dependencies"]:
            self.g.add_edge(
                dep["from"], dep["to"],
                dep_types=dep["types"],
                confidence=dep["confidence"],
                evidence=dep["evidence"],
            )