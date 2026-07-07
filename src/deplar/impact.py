"""Impact analysis.

Produces a structured, machine- and agent-readable report of what a change to a
repo (optionally a specific symbol) would affect — its dependents, transitive
blast radius, emitted events, and cross-repo call sites — before any code is
touched. This is the report an impact-analysis agent would consume as its first
step in an agentic loop.
"""
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from deplar.graph.symbol_store import SymbolStore
from deplar.scanner.endpoints import endpoint_key


def _split_endpoint(spec: Optional[str]) -> tuple:
    """Parse a "METHOD /path" or bare "/path" endpoint spec into (method, path)."""
    if not spec:
        return "", ""
    parts = spec.strip().split(None, 1)
    if len(parts) == 2 and parts[0].isalpha():
        return parts[0], parts[1]
    return "", spec.strip()


@dataclass
class ImpactReport:
    target: str
    symbol: Optional[str]
    endpoint: Optional[str] = None      # normalized key the change is scoped to
    direct_dependents: List[dict] = field(default_factory=list)
    blast_radius: List[str] = field(default_factory=list)
    events_emitted: List[str] = field(default_factory=list)
    symbol_definitions: List[dict] = field(default_factory=list)
    cross_repo_callers: List[dict] = field(default_factory=list)
    recommended_workspace: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class ImpactAnalyzer:
    def __init__(self, store: SymbolStore):
        self.store = store

    def analyze(self, target: str, symbol: Optional[str] = None,
                depth: int = 3, endpoint: Optional[str] = None) -> ImpactReport:
        dependents = self.store.get_dependents(target)

        # Attach the HTTP endpoints each dependent calls on `target`, so a
        # reviewer can see *which contract* couples them — not just that they do.
        for d in dependents:
            d["endpoints"] = sorted({
                s.get("key", "") for s in d.get("surfaces", [])
                if s.get("channel") == "http" and s.get("key")
            })

        # Endpoint-scoped impact: keep only dependents that call the changed
        # surface. This is what makes "flag consumers of the changed endpoint"
        # surgical instead of flagging every consumer of the repo.
        want = endpoint_key(*_split_endpoint(endpoint)) if endpoint else None
        if want:
            dependents = [d for d in dependents if want in d.get("endpoints", [])]

        blast = self.store.blast_radius(target, depth=depth)
        if want:
            # transitive radius is only meaningful through the matched dependents
            direct = {d["repo"] for d in dependents}
            blast = [b for b in blast if b in direct] or []

        # Kafka topics this repo emits show up as kafka-typed dependencies.
        events = [
            d["repo"] for d in self.store.get_dependencies(target)
            if "kafka" in d.get("types", [])
        ]

        report = ImpactReport(
            target=target,
            symbol=symbol,
            endpoint=want,
            direct_dependents=dependents,
            blast_radius=blast,
            events_emitted=sorted(events),
        )

        if symbol:
            report.symbol_definitions = self.store.search_symbols(symbol)
            callers = self.store.get_callers(symbol)
            # cross-repo callers are the ones that matter most for coordination
            report.cross_repo_callers = [c for c in callers if c["repo"] != target]

        report.recommended_workspace = (
            f"deplar workspace {target} --out ./workspace --branch deplar/change"
            + (" --transitive" if len(blast) > len(dependents) else "")
        )
        return report

    @staticmethod
    def render_markdown(report: ImpactReport) -> str:
        lines = [f"# Impact report — `{report.target}`"]
        if report.symbol:
            lines.append(f"> Change scoped to symbol `{report.symbol}`")
        if report.endpoint:
            lines.append(f"> Change scoped to endpoint `{report.endpoint}`")
        lines.append("")

        lines.append("## Directly affected (must update together)")
        if report.direct_dependents:
            for d in report.direct_dependents:
                types = ", ".join(d["types"])
                lines.append(f"- **{d['repo']}** ({types}, {d['confidence']:.0%})")
                for ep in d.get("endpoints", []):
                    lines.append(f"    - calls `{ep}`")
        else:
            lines.append("- none detected")

        lines.append("\n## Transitive blast radius")
        if report.blast_radius:
            for r in report.blast_radius:
                lines.append(f"- {r}")
        else:
            lines.append("- no downstream dependents")

        if report.events_emitted:
            lines.append("\n## Events emitted (consumers may depend on these)")
            for e in report.events_emitted:
                lines.append(f"- {e}")

        if report.symbol:
            lines.append(f"\n## Definitions of `{report.symbol}`")
            if report.symbol_definitions:
                for s in report.symbol_definitions:
                    lines.append(
                        f"- `{s['qualified_name']}` {s['signature']} "
                        f"— {s['repo']}/{s['file']}:{s['start_line']}"
                    )
            else:
                lines.append("- not found in the symbol index")

            lines.append(f"\n## Cross-repo call sites of `{report.symbol}`")
            if report.cross_repo_callers:
                for c in report.cross_repo_callers:
                    lines.append(
                        f"- {c['repo']}/{c['file']}:{c['line']} "
                        f"(in {c['caller']}: {c['callee']})"
                    )
            else:
                lines.append("- none in the index")

        lines.append("\n## Recommended next step")
        lines.append(f"```\n{report.recommended_workspace}\n```")
        return "\n".join(lines)
