import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import yaml

from deplar.graph.store import DependencyGraph
from deplar.graph.symbol_store import SymbolStore
from deplar.scanner.ast_parser import ASTParser
from deplar.scanner.network_detector import NetworkDetector
from deplar.scanner.resolver import DependencyEdge, DependencyResolver
from deplar.scanner.symbols import SymbolExtractor, SymbolIndex
from deplar.scanner.walker import RepoWalker


@dataclass
class RepoConfig:
    path: Path
    name: str


@dataclass
class OrgConfig:
    repos: List[RepoConfig]

    @classmethod
    def from_yaml(cls, config_path: Path) -> "OrgConfig":
        data = yaml.safe_load(config_path.read_text())
        repos = []
        for r in data.get("repos", []):
            # resolve path relative to config file location
            repo_path = config_path.parent / r["path"]
            repos.append(RepoConfig(
                path=repo_path.resolve(),
                name=r.get("name", repo_path.name),
            ))
        return cls(repos=repos)

    @classmethod
    def from_directory(cls, dir_path: Path) -> "OrgConfig":
        """Auto-discover repos from a directory of folders."""
        repos = []
        for child in sorted(dir_path.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                repos.append(RepoConfig(path=child, name=child.name))
        return cls(repos=repos)


class OrgScanner:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self._parser = ASTParser()
        self._detector = NetworkDetector()
        self._resolver = DependencyResolver()
        self._symbols = SymbolExtractor()

    def scan_repo(self, config: RepoConfig) -> List[DependencyEdge]:
        edges, _ = self.scan_repo_full(config)
        return edges

    def scan_repo_full(
        self, config: RepoConfig
    ) -> Tuple[List[DependencyEdge], SymbolIndex]:
        walker = RepoWalker(config.path)
        file_map = walker.walk()

        import_edges, feign_edges = self._parser.parse(file_map)
        network_edges = self._detector.detect(file_map)
        symbol_index = self._symbols.extract(file_map, config.name)

        edges = self._resolver.resolve(
            config.name,
            import_edges,
            feign_edges,
            network_edges,
        )
        return edges, symbol_index

    def scan_org(
        self, org_config: OrgConfig, store: Optional[SymbolStore] = None
    ) -> DependencyGraph:
        graph = DependencyGraph()
        all_edges: List[DependencyEdge] = []
        now = dt.datetime.now(dt.timezone.utc).isoformat()

        for repo in org_config.repos:
            if self.verbose:
                print(f"  scanning {repo.name}...")
            try:
                edges, symbol_index = self.scan_repo_full(repo)
                all_edges.extend(edges)
                if store is not None:
                    store.upsert_repo(repo.name, str(repo.path), now)
                    store.replace_symbols(repo.name, symbol_index)
            except Exception as e:
                print(f"  [warn] failed to scan {repo.name}: {e}")

        # cross-repo resolution: normalize to_repo names
        # against known repo names in the org
        known_repos = {r.name.lower() for r in org_config.repos}
        resolved = self._cross_resolve(all_edges, known_repos)

        for edge in resolved:
            graph.add_dependency(edge)

        if store is not None:
            store.replace_dependencies(resolved)

        return graph

    def _cross_resolve(
        self,
        edges: List[DependencyEdge],
        known_repos: set,
    ) -> List[DependencyEdge]:
        """
        Boost confidence on edges where to_repo matches
        a known repo name in the org. These are confirmed
        cross-service dependencies.
        """
        resolved = []
        for edge in edges:
            # check if to_repo fuzzy-matches a known repo
            to_lower = edge.to_repo.lower().replace("-", "").replace("_", "")
            for known in known_repos:
                known_norm = known.lower().replace("-", "").replace("_", "")
                if to_lower in known_norm or known_norm in to_lower:
                    # normalize to the known repo name and boost confidence
                    edge.to_repo = known
                    edge.confidence = min(1.0, edge.confidence + 0.2)
                    break
            resolved.append(edge)
        return resolved