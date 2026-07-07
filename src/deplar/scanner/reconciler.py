"""Corpus reconciliation.

Matches consumer references (dependency-edge targets) against the provider
identity catalog built by identity.py, across the whole corpus. This is what
lets a reference to `orders-api.internal` in repo 9 resolve to repo 11 once
repo 11 declares that identity — with no folder-name guessing or manual mapping.

It is iterative by construction: rebuild the catalog from every repo scanned so
far, then re-resolve every edge. Running it after adding an 11th repo back-fills
edges that were left dangling when repos 1-10 were scanned.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from deplar.scanner.identity import normalize_identity, stem
from deplar.scanner.resolver import DependencyEdge, _stronger_tier


@dataclass
class Match:
    repo: str
    confidence: float
    source: str
    reason: str        # exact | stem | subset


@dataclass
class ReconcileStats:
    resolved: int = 0        # edges rebound to a canonical repo
    dropped_self: int = 0    # self-referential edges removed
    merged: int = 0          # duplicate edges collapsed
    unresolved: int = 0      # edges left as external / unknown


class AliasCatalog:
    """Index of normalized identity -> repos that advertise it."""

    def __init__(self):
        self._exact: Dict[str, List[Tuple[str, float, str]]] = {}
        self._stem: Dict[str, List[Tuple[str, float, str]]] = {}

    @classmethod
    def from_aliases(cls, aliases: List[dict]) -> "AliasCatalog":
        cat = cls()
        for a in aliases:
            cat.add(a["repo"], a["alias"], a.get("confidence", 0.8),
                    a.get("source", "manual"))
        return cat

    def add(self, repo: str, alias: str, confidence: float, source: str):
        if not alias:
            return
        self._exact.setdefault(alias, []).append((repo, confidence, source))
        self._stem.setdefault(stem(alias), []).append((repo, confidence, source))

    @staticmethod
    def _best(candidates: List[Tuple[str, float, str]], exclude: str
              ) -> Optional[Tuple[str, float, str]]:
        pool = [c for c in candidates if c[0] != exclude]
        return max(pool, key=lambda c: c[1]) if pool else None

    def resolve(self, target: str, exclude_repo: str = "") -> Optional[Match]:
        """Best repo whose declared identity matches `target`."""
        norm = normalize_identity(target)
        if not norm:
            return None

        hit = self._best(self._exact.get(norm, []), exclude_repo)
        if hit:
            return Match(hit[0], min(1.0, hit[1]), hit[2], "exact")

        hit = self._best(self._stem.get(stem(norm), []), exclude_repo)
        if hit:
            return Match(hit[0], min(0.95, hit[1] * 0.95), hit[2], "stem")

        # conservative token-subset: target tokens ⊆ an alias's tokens (or vice
        # versa), guarding against trivial one-char/generic overlaps.
        nt = set(t for t in norm.split("-") if len(t) >= 3)
        if nt:
            best: Optional[Match] = None
            for alias, cands in self._exact.items():
                at = set(t for t in alias.split("-") if len(t) >= 3)
                if not at:
                    continue
                if nt <= at or at <= nt:
                    hit = self._best(cands, exclude_repo)
                    if hit and (best is None or hit[1] > best.confidence):
                        best = Match(hit[0], min(0.7, hit[1] * 0.75), hit[2],
                                     "subset")
            if best:
                return best
        return None


# noise that is never a repo (external libs / stdlib slipping through)
_NOISE = {"requests", "httpx", "urllib", "aiohttp", "axios", "fetch", "got",
          "ky", "unknown", ""}


class Reconciler:
    def reconcile(
        self, edges: List[DependencyEdge], catalog: AliasCatalog,
        drop_self: bool = True,
    ) -> Tuple[List[DependencyEdge], ReconcileStats]:
        stats = ReconcileStats()
        merged: Dict[Tuple[str, str], DependencyEdge] = {}

        for edge in edges:
            to = edge.to_repo
            confidence = edge.confidence
            evidence = list(edge.evidence)

            match = catalog.resolve(edge.to_repo, exclude_repo=edge.from_repo)
            if match:
                to = match.repo
                confidence = min(1.0, max(confidence, match.confidence))
                evidence.append(f"resolved:{match.reason}({match.source})")
                stats.resolved += 1
            else:
                # was it a self-reference we couldn't bind elsewhere?
                self_match = catalog.resolve(edge.to_repo)
                if drop_self and self_match and self_match.repo == edge.from_repo:
                    stats.dropped_self += 1
                    continue
                if normalize_identity(edge.to_repo) in _NOISE:
                    stats.unresolved += 1
                elif to != edge.from_repo:
                    stats.unresolved += 1
                else:
                    stats.dropped_self += 1
                    continue

            key = (edge.from_repo, to)
            if key in merged:
                existing = merged[key]
                for t in edge.dep_types:
                    if t not in existing.dep_types:
                        existing.dep_types.append(t)
                existing.confidence = min(1.0, max(existing.confidence, confidence))
                existing.evidence.extend(evidence)
                existing.tier = _stronger_tier(existing.tier, getattr(edge, "tier", ""))
                for s in edge.surfaces:
                    if s not in existing.surfaces:
                        existing.surfaces.append(s)
                stats.merged += 1
            else:
                merged[key] = DependencyEdge(
                    from_repo=edge.from_repo, to_repo=to,
                    dep_types=list(edge.dep_types), confidence=confidence,
                    evidence=evidence, surfaces=[dict(s) for s in edge.surfaces],
                    tier=getattr(edge, "tier", ""),
                )

        return list(merged.values()), stats
