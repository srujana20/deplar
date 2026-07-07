import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from deplar.graph.store import DependencyGraph
from deplar.graph.symbol_store import SymbolStore
from deplar.output.claude_md import ClaudeMdGenerator
from deplar.scanner.ast_parser import ASTParser
from deplar.scanner.network_detector import NetworkDetector
from deplar.scanner.org_scanner import OrgConfig, OrgScanner
from deplar.scanner.resolver import DependencyResolver
from deplar.scanner.symbols import SymbolExtractor
from deplar.scanner.walker import RepoWalker
from deplar.worktree import WorktreeManager

app = typer.Typer(
    name="deplar",
    help="Dependency radar for multi-repo codebases.",
    add_completion=False,
)
console = Console()


@app.command()
def scan(
    repo_path: str = typer.Argument(..., help="Path to the repo to scan"),
    output: str = typer.Option("deps.json", "--output", "-o"),
    name: str = typer.Option("", "--name", "-n", help="Repo name (defaults to folder name)"),
    db: str = typer.Option("deplar.db", "--db", help="SQLite symbol/graph store"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Scan a repo and build a dependency map."""
    path = Path(repo_path).resolve()
    repo_name = name or path.name

    console.print(f"\n[bold]deplar[/bold] scanning [cyan]{repo_name}[/cyan]\n")

    # 1. Walk
    console.print("  [dim]→ walking files...[/dim]")
    walker = RepoWalker(path)
    file_map = walker.walk()
    if verbose:
        for lang, files in file_map.files.items():
            console.print(f"    {lang}: {len(files)} files")

    # 2. Parse imports
    console.print("  [dim]→ parsing imports...[/dim]")
    parser = ASTParser()
    import_edges, feign_edges = parser.parse(file_map)

    # 3. Detect network calls
    console.print("  [dim]→ detecting network calls...[/dim]")
    detector = NetworkDetector()
    network_edges = detector.detect(file_map)

    # 4. Resolve
    console.print("  [dim]→ resolving dependencies...[/dim]")
    resolver = DependencyResolver()
    dep_edges = resolver.resolve(repo_name, import_edges, feign_edges, network_edges)

    # 5. Extract symbols (classes, methods, call sites)
    console.print("  [dim]→ extracting symbols...[/dim]")
    symbol_index = SymbolExtractor().extract(file_map, repo_name)

    # 6. Build graph
    graph = DependencyGraph()
    for edge in dep_edges:
        graph.add_dependency(edge)

    # 7. Save deps.json + populate the SQLite symbol/graph store
    out_path = Path(output)
    graph.save(out_path, repo_name=repo_name, repo_path=str(path))

    import datetime as _dt

    from deplar.scanner.identity import extract_identities
    store = SymbolStore(Path(db))
    store.upsert_repo(repo_name, str(path),
                      _dt.datetime.now(_dt.timezone.utc).isoformat())
    store.replace_symbols(repo_name, symbol_index)
    store.replace_dependencies(dep_edges)
    store.replace_aliases(repo_name,
                          [a.as_dict() for a in extract_identities(path, repo_name)])
    store.close()

    # 7. Print summary table
    summary = graph.summary()
    console.print()

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("[dim]Files scanned[/dim]",    str(file_map.total()))
    table.add_row("[dim]Symbols indexed[/dim]",  str(len(symbol_index.symbols)))
    table.add_row("[dim]Dependencies found[/dim]", str(summary["total_edges"]))
    table.add_row("[dim]Services detected[/dim]",  str(summary["total_nodes"]))
    if summary["most_depended_on"]:
        table.add_row("[dim]Most depended on[/dim]",
                      ", ".join(summary["most_depended_on"][:3]))
    console.print(table)

    # High confidence vs needs review
    high = sum(1 for e in dep_edges if e.confidence >= 0.8)
    low  = sum(1 for e in dep_edges if e.confidence < 0.5)
    console.print()
    console.print(f"  ✓ [green]High confidence[/green]  {high}")
    if low:
        console.print(f"  ⚠ [yellow]Needs review[/yellow]    {low}")

    console.print(f"\n  Saved to [bold]{out_path}[/bold]\n")

    if summary["total_edges"] == 0:
        console.print("[red]No dependencies found. Check the repo path or language support.[/red]")
        raise typer.Exit(1)


@app.command()
def map(
    graph: str = typer.Argument("deps.json", help="Path to deps.json"),
):
    """Print a dependency map from a saved graph."""
    import json
    data = json.loads(Path(graph).read_text())
    console.print("\n[bold]Dependency map[/bold]\n")
    deps_by_from: dict = {}
    for dep in data["dependencies"]:
        deps_by_from.setdefault(dep["from"], []).append(dep)
    for repo, deps in deps_by_from.items():
        console.print(f"[cyan]{repo}[/cyan]")
        for d in deps:
            conf = f"[green]{d['confidence']:.0%}[/green]" if d['confidence'] >= 0.8 \
                   else f"[yellow]{d['confidence']:.0%}[/yellow]"
            types = ", ".join(d["types"])
            console.print(f"  → [white]{d['to']}[/white] ({types}) {conf}")
        console.print()


@app.command("claude-md")
def claude_md(
    repo_path: str = typer.Argument(..., help="Path to the repo"),
    graph: str = typer.Option("deps.json", "--graph", "-g"),
    output: str = typer.Option("CLAUDE.md", "--output", "-o"),
    name: str = typer.Option("", "--name", "-n"),
    db: str = typer.Option("deplar.db", "--db",
                           help="SQLite store for symbol/memory sections (v2)"),
):
    """Generate a CLAUDE.md dependency context file for a repo.

    If the symbol store exists, includes the public API surface (with line
    numbers) and any learned patterns (v2)."""
    from deplar.graph.store import DependencyGraph

    path = Path(repo_path).resolve()
    repo_name = name or path.name

    g = DependencyGraph()
    graph_path = Path(graph)

    # If the provided graph path is not absolute and doesn't exist as-is,
    # try resolving it relative to the repo path. This allows calls like
    # `deplar claude-md tests/fixtures/sample_repo --graph deps.json`
    # to pick up `tests/fixtures/sample_repo/deps.json`.
    if not graph_path.exists():
        candidate = Path(repo_path) / graph_path
        if candidate.exists():
            graph_path = candidate

    if not graph_path.exists():
        console.print(f"[red]Graph file not found: {graph_path}[/red]")
        console.print("Run [bold]deplar scan .[/bold] first.")
        raise typer.Exit(1)

    g.load(graph_path)

    store = None
    if Path(db).exists():
        store = SymbolStore(Path(db))

    generator = ClaudeMdGenerator(g, repo_name, store=store)
    content = generator.generate()
    out_path = Path(output)
    out_path.write_text(content)
    if store is not None:
        store.close()

    console.print(f"\n✓ Generated [bold]{out_path}[/bold]\n")
    console.print(content)


@app.command()
def validate(
    graph: str = typer.Argument("deps.json", help="Path to deps.json"),
):
    """Validate a deps.json file against the schema."""
    import json
    from pathlib import Path

    import jsonschema

    schema_path = Path(__file__).parent.parent.parent / "schemas" / "deps.schema.json"
    if not schema_path.exists():
        console.print("[red]Schema file not found.[/red]")
        raise typer.Exit(1)

    schema = json.loads(schema_path.read_text())
    data = json.loads(Path(graph).read_text())

    try:
        jsonschema.validate(instance=data, schema=schema)
        console.print(f"[green]✓ {graph} is valid[/green]")
    except jsonschema.ValidationError as e:
        console.print(f"[red]✗ Validation failed:[/red] {e.message}")
        raise typer.Exit(1)

@app.command("scan-org")
def scan_org(
    repos_dir: str = typer.Argument(..., help="Directory of repos or path to deplar.yaml"),
    config: str = typer.Option("", "--config", "-c", help="Path to deplar.yaml"),
    output: str = typer.Option("org-deps.json", "--output", "-o"),
    db: str = typer.Option("deplar.db", "--db", help="SQLite symbol/graph store"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Scan multiple repos and build a cross-repo dependency graph."""
    path = Path(repos_dir).resolve()

    # load config
    config_path = Path(config) if config else None
    if config_path and config_path.exists():
        org_config = OrgConfig.from_yaml(config_path)
    elif (path / "deplar.yaml").exists():
        org_config = OrgConfig.from_yaml(path / "deplar.yaml")
    elif path.is_dir():
        org_config = OrgConfig.from_directory(path)
    else:
        console.print("[red]No deplar.yaml found and path is not a directory.[/red]")
        raise typer.Exit(1)

    console.print(f"\n[bold]deplar[/bold] scanning org "
                  f"[cyan]({len(org_config.repos)} repos)[/cyan]\n")

    scanner = OrgScanner(verbose=verbose)
    store = SymbolStore(Path(db))

    with console.status("Scanning repos..."):
        graph = scanner.scan_org(org_config, store=store)

    store.close()

    # save org dependency graph
    out_path = Path(output)
    graph.save(out_path)

    # save per-repo interface manifest (provides + consumes)
    iface_path = out_path.with_name(
        out_path.stem.replace("deps", "interfaces") + out_path.suffix
        if "deps" in out_path.stem else out_path.stem + "-interfaces" + out_path.suffix
    )
    iface_path.write_text(json.dumps({
        "version": "1.0",
        "repos": scanner.last_manifest,
    }, indent=2))

    # print summary
    summary = graph.summary()
    console.print()

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("[dim]Repos scanned[/dim]",      str(len(org_config.repos)))
    table.add_row("[dim]Total dependencies[/dim]",  str(summary["total_edges"]))
    table.add_row("[dim]Services in graph[/dim]",   str(summary["total_nodes"]))
    ms = scanner.last_match_stats
    if ms:
        table.add_row("[dim]HTTP surfaces matched[/dim]",
                      f"{ms.surfaces_matched}/{ms.surfaces_total}"
                      + (f"  ({ms.surfaces_unmatched} unmatched)"
                         if ms.surfaces_unmatched else ""))
    if summary["most_depended_on"]:
        table.add_row("[dim]Most depended on[/dim]",
                      ", ".join(summary["most_depended_on"][:3]))
    if summary["orphans"]:
        table.add_row("[dim]Orphan services[/dim]",
                      ", ".join(summary["orphans"][:3]))
    console.print(table)
    console.print(f"\n  Saved deps to [bold]{out_path}[/bold]")
    console.print(f"  Saved interfaces to [bold]{iface_path}[/bold]\n")


@app.command()
def query(
    calls: str = typer.Option("", "--calls", help="Repos that this repo calls"),
    dependents: str = typer.Option("", "--dependents", help="Repos that call this repo"),
    blast: str = typer.Option("", "--blast", help="Blast radius of this repo"),
    symbols: str = typer.Option("", "--symbols", help="Search symbols by name"),
    callers: str = typer.Option("", "--callers", help="Find call sites of a symbol"),
    repo: str = typer.Option("", "--repo", help="Scope symbol/caller search to a repo"),
    db: str = typer.Option("deplar.db", "--db", help="SQLite symbol/graph store"),
):
    """Query the dependency + symbol knowledge graph."""
    store_path = Path(db)
    if not store_path.exists():
        console.print(f"[red]Store not found: {db}[/red]  Run [bold]deplar scan[/bold] first.")
        raise typer.Exit(1)
    store = SymbolStore(store_path)

    if calls:
        deps = store.get_dependencies(calls)
        console.print(f"\n[cyan]{calls}[/cyan] calls:")
        for d in deps or []:
            console.print(f"  → {d['repo']} ({', '.join(d['types'])}) {d['confidence']:.0%}")
        if not deps:
            console.print("  [dim]none[/dim]")

    if dependents:
        deps = store.get_dependents(dependents)
        console.print(f"\n[cyan]{dependents}[/cyan] is called by:")
        for d in deps or []:
            console.print(f"  ← {d['repo']} ({', '.join(d['types'])}) {d['confidence']:.0%}")
        if not deps:
            console.print("  [dim]none[/dim]")

    if blast:
        radius = store.blast_radius(blast)
        console.print(f"\n[cyan]{blast}[/cyan] blast radius:")
        for r in radius or []:
            console.print(f"  ⚠ {r}")
        if not radius:
            console.print("  [dim]no downstream dependents[/dim]")

    if symbols:
        results = store.search_symbols(symbols, repo=repo or None)
        console.print(f"\nSymbols matching [cyan]{symbols}[/cyan]:")
        for s in results:
            console.print(
                f"  [{s['kind']}] [white]{s['qualified_name']}[/white] "
                f"[dim]{s['signature']}[/dim] — {s['repo']}/{s['file']}:{s['start_line']}"
            )
        if not results:
            console.print("  [dim]none[/dim]")

    if callers:
        results = store.get_callers(callers, repo=repo or None)
        console.print(f"\nCall sites of [cyan]{callers}[/cyan]:")
        for c in results:
            console.print(
                f"  {c['repo']}/{c['file']}:{c['line']} "
                f"[dim](in {c['caller']}: {c['callee']})[/dim]"
            )
        if not results:
            console.print("  [dim]none[/dim]")

    store.close()


@app.command()
def workspace(
    target: str = typer.Argument(..., help="Repo to build a coordinated workspace for"),
    out: str = typer.Option("./workspace", "--out", "-o", help="Workspace directory"),
    branch: str = typer.Option("deplar/change", "--branch", "-b", help="Branch for worktrees"),
    db: str = typer.Option("deplar.db", "--db", help="SQLite symbol/graph store"),
    transitive: bool = typer.Option(False, "--transitive", "-t",
                                    help="Include the full transitive blast radius"),
    with_dependencies: bool = typer.Option(False, "--with-dependencies",
                                           help="Also include repos the target calls"),
    remove: bool = typer.Option(False, "--remove", help="Tear down the workspace"),
):
    """Check out a repo and all repos affected by changing it as git worktrees
    in one workspace — ready for coordinated, parallel edits."""
    store_path = Path(db)
    if not store_path.exists():
        console.print(f"[red]Store not found: {db}[/red]  Run [bold]deplar scan-org[/bold] first.")
        raise typer.Exit(1)

    store = SymbolStore(store_path)
    manager = WorktreeManager(store)

    if remove:
        console.print(f"\n[bold]Removing worktrees under[/bold] {out}\n")
        for r in manager.remove(Path(out)):
            mark = "[green]✓[/green]" if r.status == "removed" else "[red]✗[/red]"
            console.print(f"  {mark} {r.repo}  [dim]{r.detail}[/dim]")
        store.close()
        return

    affected = manager.affected_repos(
        target, transitive=transitive, include_dependencies=with_dependencies)
    console.print(
        f"\n[bold]deplar[/bold] workspace for [cyan]{target}[/cyan] — "
        f"{len(affected)} repo(s): {', '.join(affected)}\n"
    )

    results = manager.checkout(
        target, Path(out), branch,
        transitive=transitive, include_dependencies=with_dependencies,
    )

    marks = {"created": "[green]✓[/green]", "exists": "[yellow]•[/yellow]",
             "skipped": "[yellow]⚠[/yellow]", "error": "[red]✗[/red]"}
    for r in results:
        console.print(f"  {marks.get(r.status, '?')} [white]{r.repo}[/white] "
                      f"→ {r.worktree_path}  [dim]{r.status}: {r.detail}[/dim]")

    created = sum(1 for r in results if r.status == "created")
    console.print(f"\n  {created} worktree(s) ready in [bold]{out}[/bold]\n")
    store.close()


@app.command()
def impact(
    target: str = typer.Argument(..., help="Repo to analyze the impact of changing"),
    symbol: str = typer.Option("", "--symbol", "-s", help="Scope to a symbol name"),
    endpoint: str = typer.Option("", "--endpoint", "-e",
                                 help="Scope to an HTTP endpoint, e.g. 'PUT /v1/orders/{id}'"),
    depth: int = typer.Option(3, "--depth", help="Blast-radius depth"),
    db: str = typer.Option("deplar.db", "--db", help="SQLite symbol/graph store"),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of markdown"),
    output: str = typer.Option("", "--output", "-o", help="Write report to a file"),
):
    """Produce a structured impact report before touching a repo."""
    from deplar.impact import ImpactAnalyzer

    store_path = Path(db)
    if not store_path.exists():
        console.print(f"[red]Store not found: {db}[/red]  Run [bold]deplar scan-org[/bold] first.")
        raise typer.Exit(1)

    store = SymbolStore(store_path)
    report = ImpactAnalyzer(store).analyze(target, symbol=symbol or None,
                                           depth=depth, endpoint=endpoint or None)
    store.close()

    if json_out:
        import json
        text = json.dumps(report.to_dict(), indent=2)
    else:
        text = ImpactAnalyzer.render_markdown(report)

    if output:
        Path(output).write_text(text)
        console.print(f"✓ Wrote impact report to [bold]{output}[/bold]")
    else:
        console.print(text)


@app.command()
def remember(
    repo: str = typer.Argument(..., help="Repo the note is about"),
    note: str = typer.Argument(..., help="The pattern/convention/gotcha to remember"),
    kind: str = typer.Option("note", "--kind", "-k",
                             help="pattern | convention | gotcha | note"),
    db: str = typer.Option("deplar.db", "--db", help="SQLite symbol/graph store"),
):
    """Persist a learned pattern about a repo (survives across sessions)."""
    store = SymbolStore(Path(db))
    import datetime as _dt
    mem_id = store.remember(repo, note, kind=kind,
                            created_at=_dt.datetime.now(_dt.timezone.utc).isoformat())
    store.close()
    console.print(f"✓ Remembered [dim]#{mem_id}[/dim] ({kind}) for [cyan]{repo}[/cyan]")


@app.command()
def recall(
    repo: str = typer.Argument(..., help="Repo to recall notes for"),
    kind: str = typer.Option("", "--kind", "-k", help="Filter by kind"),
    db: str = typer.Option("deplar.db", "--db", help="SQLite symbol/graph store"),
):
    """Recall everything learned about a repo."""
    store = SymbolStore(Path(db))
    notes = store.recall(repo, kind=kind or None)
    store.close()
    console.print(f"\n[bold]Learned about[/bold] [cyan]{repo}[/cyan]:")
    for n in notes:
        console.print(f"  [dim]#{n['id']}[/dim] ({n['kind']}) {n['note']}")
    if not notes:
        console.print("  [dim]nothing yet[/dim]")


@app.command()
def skill(
    repo: str = typer.Argument(..., help="Repo to generate a SKILL.md for"),
    output: str = typer.Option("SKILL.md", "--output", "-o"),
    db: str = typer.Option("deplar.db", "--db", help="SQLite symbol/graph store"),
):
    """Generate a reusable Claude skill (SKILL.md) for a repo."""
    from deplar.skill import SkillGenerator

    store_path = Path(db)
    if not store_path.exists():
        console.print(f"[red]Store not found: {db}[/red]  Run [bold]deplar scan[/bold] first.")
        raise typer.Exit(1)
    store = SymbolStore(store_path)
    content = SkillGenerator(store).generate(repo)
    store.close()
    Path(output).write_text(content)
    console.print(f"✓ Generated [bold]{output}[/bold]\n")
    console.print(content)


@app.command()
def skillhub(
    out: str = typer.Option("./skillhub", "--out", "-o", help="Skillhub output dir"),
    db: str = typer.Option("deplar.db", "--db", help="SQLite symbol/graph store"),
):
    """Generate a skill per repo plus a static browsable portal."""
    from deplar.skill import build_skillhub

    store_path = Path(db)
    if not store_path.exists():
        console.print(f"[red]Store not found: {db}[/red]  Run [bold]deplar scan-org[/bold] first.")
        raise typer.Exit(1)
    store = SymbolStore(store_path)
    index = build_skillhub(store, Path(out))
    store.close()
    console.print(f"\n[bold]Skillhub[/bold] — {len(index)} skill(s) → {out}")
    for s in index:
        console.print(f"  [green]✓[/green] {s['repo']}  [dim]v{s['version']} "
                      f"({', '.join(s['languages']) or '—'})[/dim]")
    console.print(f"\n  Open [bold]{out}/index.html[/bold] to browse.\n")


@app.command("verify-workspace")
def verify_workspace(
    workspace: str = typer.Argument("./workspace", help="Workspace directory"),
    test_cmd: str = typer.Option("", "--test-cmd", help="Override test command for all repos"),
    timeout: int = typer.Option(600, "--timeout", help="Per-repo timeout (seconds)"),
):
    """Run each repo's tests across a coordinated workspace (validator)."""
    from deplar.validator import WorkspaceValidator

    ws = Path(workspace)
    if not ws.exists():
        console.print(f"[red]Workspace not found: {workspace}[/red]")
        raise typer.Exit(1)

    console.print(f"\n[bold]Validating workspace[/bold] {workspace}\n")
    result = WorkspaceValidator(timeout=timeout).validate(
        ws, test_cmd=test_cmd or None)

    for r in result.repos:
        if r.skipped:
            console.print(f"  [yellow]⊘[/yellow] {r.repo}  [dim]{r.detail}[/dim]")
        elif r.passed:
            console.print(f"  [green]✓[/green] {r.repo}  [dim]{r.command}[/dim]")
        else:
            console.print(f"  [red]✗[/red] {r.repo}  [dim]{r.command} — {r.detail}[/dim]")
            if r.output_tail:
                for line in r.output_tail.splitlines():
                    console.print(f"      [dim]{line}[/dim]")

    console.print()
    if result.ok:
        console.print("  [green]All repos passed.[/green]\n")
    else:
        console.print("  [red]Some repos failed.[/red]\n")
        raise typer.Exit(1)


@app.command()
def reconcile(
    db: str = typer.Option("deplar.db", "--db", help="SQLite symbol/graph store"),
    output: str = typer.Option("", "--output", "-o",
                               help="Re-export the reconciled graph to deps.json"),
):
    """Re-bind dependency references to repos using the full identity catalog.

    Run after scanning a new repo: references left dangling when earlier repos
    were scanned get resolved against the newly-declared identities."""
    from deplar.scanner.reconciler import AliasCatalog, Reconciler

    store_path = Path(db)
    if not store_path.exists():
        console.print(f"[red]Store not found: {db}[/red]  Run [bold]deplar scan[/bold] first.")
        raise typer.Exit(1)

    store = SymbolStore(store_path)
    catalog = AliasCatalog.from_aliases(store.all_aliases())
    edges = store.all_dependencies()
    resolved, stats = Reconciler().reconcile(edges, catalog)

    store.clear_dependencies()
    store.replace_dependencies(resolved)

    if output:
        graph = DependencyGraph()
        for e in resolved:
            graph.add_dependency(e)
        graph.save(Path(output))

    store.close()

    console.print("\n[bold]Reconciliation[/bold]")
    console.print(f"  edges in           {len(edges)}")
    console.print(f"  [green]resolved to a repo   {stats.resolved}[/green]")
    console.print(f"  merged duplicates    {stats.merged}")
    console.print(f"  dropped self-refs    {stats.dropped_self}")
    console.print(f"  left unresolved      {stats.unresolved}")
    console.print(f"  edges out          {len(resolved)}\n")


@app.command()
def alias(
    repo: str = typer.Argument(..., help="Repo to pin an identity to"),
    name: str = typer.Argument(..., help="Name/hostname/URL others refer to it by"),
    remove: bool = typer.Option(False, "--remove", help="Remove this pin instead"),
    reconcile_now: bool = typer.Option(False, "--reconcile",
                                       help="Re-run reconciliation immediately"),
    db: str = typer.Option("deplar.db", "--db", help="SQLite symbol/graph store"),
):
    """Manually pin an identity to a repo (a safety valve for cases the matcher
    misses). Manual pins have full confidence and survive re-scans."""
    store_path = Path(db)
    if not store_path.exists():
        console.print(f"[red]Store not found: {db}[/red]  Run [bold]deplar scan[/bold] first.")
        raise typer.Exit(1)

    store = SymbolStore(store_path)
    if remove:
        ok = store.remove_alias(repo, name)
        console.print(
            f"{'✓ Removed' if ok else '⚠ No such'} pin [white]{name}[/white] "
            f"for [cyan]{repo}[/cyan]"
        )
    else:
        norm = store.add_alias(repo, name)
        if not norm:
            console.print(f"[red]'{name}' normalizes to nothing — nothing pinned.[/red]")
            store.close()
            raise typer.Exit(1)
        console.print(
            f"✓ Pinned [cyan]{repo}[/cyan] is also known as "
            f"[white]{name}[/white] [dim](matches as '{norm}')[/dim]"
        )

    if reconcile_now:
        from deplar.scanner.reconciler import AliasCatalog, Reconciler
        catalog = AliasCatalog.from_aliases(store.all_aliases())
        resolved, stats = Reconciler().reconcile(store.all_dependencies(), catalog)
        store.clear_dependencies()
        store.replace_dependencies(resolved)
        console.print(f"  reconciled: [green]{stats.resolved} resolved[/green], "
                      f"{stats.merged} merged, {stats.dropped_self} self-refs dropped")
    else:
        console.print("  [dim]run `deplar reconcile` to apply.[/dim]")
    store.close()


@app.command()
def identities(
    repo: str = typer.Argument(..., help="Repo to show the identity catalog for"),
    db: str = typer.Option("deplar.db", "--db", help="SQLite symbol/graph store"),
):
    """Show what identities a repo advertises itself as (its catalog)."""
    store_path = Path(db)
    if not store_path.exists():
        console.print(f"[red]Store not found: {db}[/red]")
        raise typer.Exit(1)
    store = SymbolStore(store_path)
    aliases = store.aliases_for_repo(repo)
    store.close()
    console.print(f"\n[cyan]{repo}[/cyan] is known as:")
    for a in aliases:
        console.print(f"  [white]{a['alias']}[/white] "
                      f"[dim](from {a['raw']!r} via {a['source']}, "
                      f"{a['confidence']:.0%})[/dim]")
    if not aliases:
        console.print("  [dim]no identities recorded — run deplar scan[/dim]")


@app.command("impact-agent")
def impact_agent(
    change: str = typer.Argument(..., help="Natural-language description of the proposed change"),
    db: str = typer.Option("deplar.db", "--db", help="SQLite symbol/graph store"),
    model: str = typer.Option("claude-opus-4-8", "--model"),
):
    """LLM agent: query the graph for a proposed change and write an impact report."""
    from deplar.agent import ImpactAgent, default_client

    store_path = Path(db)
    if not store_path.exists():
        console.print(f"[red]Store not found: {db}[/red]  Run [bold]deplar scan-org[/bold] first.")
        raise typer.Exit(1)

    store = SymbolStore(store_path)
    try:
        agent = ImpactAgent(store, client=default_client(), model=model)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        store.close()
        raise typer.Exit(1)

    console.print(f"\n[bold]Impact agent[/bold] analyzing: [cyan]{change}[/cyan]\n")
    try:
        with console.status("thinking + querying the graph..."):
            run = agent.run(change, on_event=lambda e: console.print(f"  [dim]→ {e}[/dim]"))
    except Exception as e:
        console.print(f"[red]Agent failed:[/red] {e}")
        store.close()
        raise typer.Exit(1)
    store.close()

    if run.refused:
        console.print("[yellow]The model declined to answer this request.[/yellow]")
        raise typer.Exit(1)
    console.print()
    console.print(run.text)
    console.print(f"\n[dim]({run.iterations} steps, {len(run.tool_calls)} tool calls)[/dim]\n")


@app.command("validate-agent")
def validate_agent(
    change: str = typer.Argument(..., help="Description of the change being validated"),
    workspace: str = typer.Option("./workspace", "--workspace", "-w"),
    test_cmd: str = typer.Option("", "--test-cmd", help="Override test command for all repos"),
    db: str = typer.Option("deplar.db", "--db", help="SQLite symbol/graph store"),
    model: str = typer.Option("claude-opus-4-8", "--model"),
):
    """Dual-agent: planner drafts a coordinated change plan, validator re-queries
    the graph and runs tests across the workspace, then returns a verdict."""
    from deplar.agent import PlannerValidator, default_client

    store_path = Path(db)
    if not store_path.exists():
        console.print(f"[red]Store not found: {db}[/red]  Run [bold]deplar scan-org[/bold] first.")
        raise typer.Exit(1)

    store = SymbolStore(store_path)
    try:
        pv = PlannerValidator(store, client=default_client(), model=model)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        store.close()
        raise typer.Exit(1)

    console.print(f"\n[bold]Planner/validator[/bold] for: [cyan]{change}[/cyan]\n")
    try:
        with console.status("planner + validator working..."):
            run = pv.run(change, Path(workspace), test_cmd=test_cmd or None,
                         on_event=lambda e: console.print(f"  [dim]→ {e}[/dim]"))
    except Exception as e:
        console.print(f"[red]Agent failed:[/red] {e}")
        store.close()
        raise typer.Exit(1)
    store.close()

    console.print("\n[bold]— Change plan (planner) —[/bold]\n")
    console.print(run.plan)
    console.print("\n[bold]— Validation verdict (validator) —[/bold]\n")
    console.print(run.verdict)
    console.print(f"\n[dim](planner {run.planner_iterations} steps, "
                  f"validator {run.validator_iterations} steps)[/dim]\n")


@app.command()
def ui(
    output: str = typer.Option("deplar-ui.html", "--output", "-o"),
    db: str = typer.Option("deplar.db", "--db", help="SQLite symbol/graph store"),
    open_browser: bool = typer.Option(False, "--open", help="Open in a browser"),
):
    """Generate a self-contained interactive dependency-map UI (single HTML file)."""
    from deplar.ui import build_ui_data, render_html

    store_path = Path(db)
    if not store_path.exists():
        console.print(f"[red]Store not found: {db}[/red]  Run [bold]deplar scan-org[/bold] first.")
        raise typer.Exit(1)
    store = SymbolStore(store_path)
    data = build_ui_data(store)
    store.close()

    out_path = Path(output)
    out_path.write_text(render_html(data, served=False))
    console.print(f"\n✓ Wrote [bold]{out_path}[/bold] "
                  f"[dim]({len(data['nodes'])} services, {len(data['edges'])} deps)[/dim]")
    console.print("  Open it in any browser — no server needed.\n")
    if open_browser:
        import webbrowser
        webbrowser.open(out_path.resolve().as_uri())


@app.command()
def serve(
    db: str = typer.Option("deplar.db", "--db", help="SQLite symbol/graph store"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port", "-p"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
):
    """Serve the interactive UI locally with live data + a reconcile action."""
    from deplar.ui import serve as make_server

    store_path = Path(db)
    if not store_path.exists():
        console.print(f"[red]Store not found: {db}[/red]  Run [bold]deplar scan-org[/bold] first.")
        raise typer.Exit(1)

    httpd = make_server(store_path, host=host, port=port)
    url = f"http://{host}:{port}"
    console.print(f"\n[bold]deplar UI[/bold] → [cyan]{url}[/cyan]  [dim](Ctrl-C to stop)[/dim]\n")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        console.print("\nstopped.\n")
    finally:
        httpd.server_close()


@app.command()
def mcp(
    db: str = typer.Option("deplar.db", "--db", help="SQLite symbol/graph store"),
    skills: str = typer.Option("skillhub", "--skills", help="Skillhub registry dir"),
):
    """Run the deplar MCP server (stdio) exposing the knowledge graph to agents."""
    import os

    os.environ["DEPLAR_DB"] = str(Path(db).resolve())
    os.environ["DEPLAR_SKILLS"] = str(Path(skills).resolve())
    from deplar.mcp_server import main as run_mcp

    run_mcp()


if __name__ == "__main__":
    app()