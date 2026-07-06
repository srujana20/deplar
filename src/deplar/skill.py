"""Skillhub — generate reusable Claude skills from the knowledge graph.

`SkillGenerator` distills a repo's dependency + symbol graph into a SKILL.md: a
self-contained brief on how to work in that codebase (what it talks to, its
public surface, gotchas, and how to change it safely). Skills are versioned by
a hash of the graph snapshot they were built from, so a skill can be tied to the
exact state of the code it describes.

`build_skillhub` writes a skill per repo plus a static, self-contained HTML
portal for browsing them.
"""
import html
import json
from pathlib import Path
from typing import List, Optional

from deplar.graph.symbol_store import SymbolStore


class SkillGenerator:
    def __init__(self, store: SymbolStore):
        self.store = store

    def slug(self, repo: str) -> str:
        return "work-in-" + "".join(
            c if c.isalnum() or c == "-" else "-" for c in repo.lower()
        ).strip("-")

    def metadata(self, repo: str) -> dict:
        deps = self.store.get_dependencies(repo)
        dependents = self.store.get_dependents(repo)
        called = [d["repo"] for d in deps if "kafka" not in d.get("types", [])]
        summary = (
            f"How to work in {repo}"
            + (f" — calls {', '.join(called[:3])}" if called else "")
            + (f"; called by {len(dependents)} repo(s)" if dependents else "")
            + "."
        )
        return {
            "name": self.slug(repo),
            "repo": repo,
            "description": summary,
            "version": self.store.snapshot_hash(repo),
        }

    def generate(self, repo: str) -> str:
        meta = self.metadata(repo)
        deps = self.store.get_dependencies(repo)
        dependents = self.store.get_dependents(repo)
        events = sorted(d["repo"] for d in deps if "kafka" in d.get("types", []))
        calls = [d for d in deps if "kafka" not in d.get("types", [])]
        symbols = self.store.symbols_for_repo(
            repo, kinds=["class", "interface", "method", "function"], limit=30)
        notes = self.store.recall(repo)

        lines = [
            "---",
            f"name: {meta['name']}",
            f"description: {meta['description']}",
            f"version: {meta['version']}",
            "---",
            "",
            f"# Working in `{repo}`",
            "",
            "A deplar-generated skill. Load it before editing this repo so you "
            "know what it talks to and what a change will ripple into.",
        ]

        lines.append("\n## What this service calls")
        if calls:
            for d in calls:
                lines.append(f"- {d['repo']} ({', '.join(d['types'])}, "
                             f"{d['confidence']:.0%})")
        else:
            lines.append("- nothing detected")

        lines.append("\n## Who calls this service")
        if dependents:
            for d in dependents:
                lines.append(f"- {d['repo']} ({', '.join(d['types'])})")
        else:
            lines.append("- nothing detected (or not yet scanned)")

        if events:
            lines.append("\n## Events emitted")
            for e in events:
                lines.append(f"- {e} (Kafka)")

        lines.append("\n## Public API surface")
        if symbols:
            for s in symbols:
                sig = s["signature"] or s["name"]
                lines.append(f"- [{s['kind']}] `{sig}` — {s['file']}:{s['start_line']}")
        else:
            lines.append("- no symbols indexed")

        if notes:
            lines.append("\n## Conventions & gotchas")
            for n in notes:
                lines.append(f"- ({n['kind']}) {n['note']}")

        lines.append("\n## Making changes safely")
        lines.append(f"1. Run `deplar impact {repo}` to see the blast radius.")
        lines.append(f"2. Run `deplar workspace {repo}` to check out every "
                     "affected repo together.")
        lines.append("3. After editing, run `deplar verify-workspace ./workspace` "
                     "to test all repos.")
        return "\n".join(lines) + "\n"


def build_skillhub(store: SymbolStore, out_dir: Path) -> List[dict]:
    """Generate a SKILL.md per repo + index.json + a static index.html portal."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    gen = SkillGenerator(store)

    index: List[dict] = []
    for repo in store.list_repos():
        name = repo["name"]
        meta = gen.metadata(name)
        content = gen.generate(name)
        skill_dir = out_dir / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content)
        langs = sorted({
            s["language"] for s in store.symbols_for_repo(name, limit=1000)
            if s.get("language")
        })
        index.append({**meta, "languages": langs,
                      "path": str((skill_dir / "SKILL.md"))})

    (out_dir / "index.json").write_text(json.dumps(index, indent=2))
    (out_dir / "index.html").write_text(_render_portal(index))
    return index


def read_skill_index(skills_dir: Path) -> List[dict]:
    idx = Path(skills_dir) / "index.json"
    if not idx.exists():
        return []
    return json.loads(idx.read_text())


def read_skill(skills_dir: Path, repo: str) -> Optional[str]:
    skill = Path(skills_dir) / repo / "SKILL.md"
    return skill.read_text() if skill.exists() else None


def _render_portal(index: List[dict]) -> str:
    cards = []
    for s in index:
        langs = " ".join(s.get("languages", []))
        cards.append(f"""
    <article class="card" data-search="{html.escape((s['repo'] + ' ' + langs).lower())}">
      <h2>{html.escape(s['repo'])}</h2>
      <p class="desc">{html.escape(s['description'])}</p>
      <div class="meta">
        <span class="langs">{html.escape(langs) or '—'}</span>
        <span class="ver">v{html.escape(s['version'])}</span>
      </div>
    </article>""")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>deplar skillhub</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 0;
          padding: 2rem; max-width: 1000px; margin: 0 auto; }}
  h1 {{ font-size: 1.5rem; }}
  .sub {{ color: #888; margin-bottom: 1.5rem; }}
  #q {{ width: 100%; padding: .7rem 1rem; font-size: 1rem; border-radius: 10px;
        border: 1px solid #8884; margin-bottom: 1.5rem; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
           gap: 1rem; }}
  .card {{ border: 1px solid #8884; border-radius: 12px; padding: 1rem 1.2rem; }}
  .card h2 {{ font-size: 1.05rem; margin: 0 0 .4rem; }}
  .desc {{ color: #999; font-size: .9rem; margin: 0 0 .8rem; }}
  .meta {{ display: flex; justify-content: space-between; font-size: .8rem;
           color: #777; }}
  .ver {{ font-family: ui-monospace, monospace; }}
</style>
</head>
<body>
  <h1>deplar skillhub</h1>
  <p class="sub">{len(index)} skill(s) generated from the dependency knowledge graph.</p>
  <input id="q" type="search" placeholder="Search by repo or language…" autofocus>
  <div class="grid" id="grid">{''.join(cards)}
  </div>
<script>
  const q = document.getElementById('q');
  const cards = [...document.querySelectorAll('.card')];
  q.addEventListener('input', () => {{
    const t = q.value.toLowerCase();
    cards.forEach(c => c.style.display =
      c.dataset.search.includes(t) ? '' : 'none');
  }});
</script>
</body>
</html>
"""
