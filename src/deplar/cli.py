from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from deplar.graph.store import DependencyGraph
from deplar.output.claude_md import ClaudeMdGenerator
from deplar.scanner.ast_parser import ASTParser
from deplar.scanner.network_detector import NetworkDetector
from deplar.scanner.org_scanner import OrgConfig, OrgScanner
from deplar.scanner.resolver import DependencyResolver
from deplar.scanner.walker import RepoWalker

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

    # 5. Build graph
    graph = DependencyGraph()
    for edge in dep_edges:
        graph.add_dependency(edge)

    # 6. Save
    out_path = Path(output)
    graph.save(out_path, repo_name=repo_name, repo_path=str(path))

    # 7. Print summary table
    summary = graph.summary()
    console.print()

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("[dim]Files scanned[/dim]",    str(file_map.total()))
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
):
    """Generate a CLAUDE.md dependency context file for a repo."""
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

    generator = ClaudeMdGenerator(g, repo_name)
    out_path = generator.write(Path(output))

    console.print(f"\n✓ Generated [bold]{out_path}[/bold]\n")
    console.print(generator.generate())


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

    with console.status("Scanning repos..."):
        graph = scanner.scan_org(org_config)

    # save
    out_path = Path(output)
    graph.save(out_path)

    # print summary
    summary = graph.summary()
    console.print()

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("[dim]Repos scanned[/dim]",      str(len(org_config.repos)))
    table.add_row("[dim]Total dependencies[/dim]",  str(summary["total_edges"]))
    table.add_row("[dim]Services in graph[/dim]",   str(summary["total_nodes"]))
    if summary["most_depended_on"]:
        table.add_row("[dim]Most depended on[/dim]",
                      ", ".join(summary["most_depended_on"][:3]))
    if summary["orphans"]:
        table.add_row("[dim]Orphan services[/dim]",
                      ", ".join(summary["orphans"][:3]))
    console.print(table)
    console.print(f"\n  Saved to [bold]{out_path}[/bold]\n")



if __name__ == "__main__":
    app()