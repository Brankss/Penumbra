"""Penumbra CLI — `penumbra "query" --privacy high --output report.md`."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

from penumbra import __version__
from penumbra.core import Researcher
from penumbra.exceptions import PenumbraError
from penumbra.types import PrivacyLevel

app = typer.Typer(
    name="penumbra",
    help="Privacy-native deep research agent.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, markup=True)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"penumbra v{__version__}")
        raise typer.Exit()


@app.command()
def research(
    query: Annotated[str, typer.Argument(help="The research question.")],
    privacy: Annotated[
        str,
        typer.Option("--privacy", "-p", help="Privacy level: off | low | medium | high"),
    ] = "medium",
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write the Markdown report to this path."),
    ] = None,
    json_output: Annotated[
        Path | None,
        typer.Option("--json", help="Write the full structured report as JSON."),
    ] = None,
    max_steps: Annotated[
        int,
        typer.Option("--steps", help="Maximum number of planned subqueries."),
    ] = 5,
    sources_per_step: Annotated[
        int,
        typer.Option("--per-step", help="Sources fetched per subquery."),
    ] = 4,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    _version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = None,
) -> None:
    """Run a research query and print/save the resulting report."""
    _setup_logging(verbose)
    try:
        level = PrivacyLevel.parse(privacy)
    except ValueError as e:
        console.print(f"[red]✗[/red] {e}")
        raise typer.Exit(code=2) from None

    console.print(
        Panel.fit(
            f"[bold]Penumbra[/bold] v{__version__}\n"
            f"Query: [cyan]{query}[/cyan]\n"
            f"Privacy: [yellow]{level.name.lower()}[/yellow] · "
            f"Steps: {max_steps} · Sources/step: {sources_per_step}",
            border_style="dim",
        )
    )

    try:
        report = asyncio.run(
            _run(query, level, max_steps, sources_per_step),
        )
    except PenumbraError as e:
        console.print(f"[red]✗ {e}[/red]")
        raise typer.Exit(code=1) from None
    except KeyboardInterrupt:
        console.print("[yellow]Aborted.[/yellow]")
        raise typer.Exit(code=130) from None

    _print_summary_table(report)
    console.print()
    console.print(report.summary)

    if output:
        path = report.save(output)
        console.print(f"\n[green]✓[/green] Markdown report saved to: [bold]{path}[/bold]")
    if json_output:
        path = report.save_json(json_output)
        console.print(f"[green]✓[/green] JSON report saved to: [bold]{path}[/bold]")
    if not output and not json_output:
        console.print("\n[dim]Tip: pass --output report.md to save the full Markdown.[/dim]")


async def _run(
    query: str,
    privacy: PrivacyLevel,
    max_steps: int,
    sources_per_step: int,
):
    async with Researcher(
        privacy=privacy,
        max_steps=max_steps,
        sources_per_step=sources_per_step,
    ) as r:
        return await r.run(query)


def _print_summary_table(report) -> None:  # type: ignore[no-untyped-def]
    table = Table(title="Research complete", show_header=False, border_style="dim")
    table.add_row("Privacy level", report.privacy_level.name.lower())
    table.add_row("Subqueries planned", str(len(report.plan)))
    table.add_row("Sources retrieved", str(len(report.sources)))
    table.add_row("Citations extracted", str(len(report.citations)))
    table.add_row(
        "Cross-verified",
        str(sum(1 for c in report.citations if c.cross_verified)),
    )
    table.add_row("Duration", f"{report.duration_seconds:.1f}s")
    if report.metadata.get("via_tor"):
        table.add_row("Routing", "[green]via Tor[/green]")
    console.print(table)


def main() -> None:
    try:
        app()
    except SystemExit as e:
        sys.exit(e.code if isinstance(e.code, int) else 1)


if __name__ == "__main__":
    main()
