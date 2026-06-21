import typer
from rich.console import Console
from rich.table import Table
from rich.progress import track
from pathlib import Path
from deplar.scanner.walker import RepoWalker
from deplar.scanner.ast_parser import ASTParser
from deplar.scanner.network_detector import NetworkDetector
from deplar.scanner.resolver import DependencyResolver
from deplar.graph.store import DependencyGraph

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
    graph.save(out_path)

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
    console.print(f"\n[bold]Dependency map[/bold]\n")
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


if __name__ == "__main__":
    app()