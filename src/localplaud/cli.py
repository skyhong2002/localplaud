"""localplaud command-line interface."""

from __future__ import annotations

import sys
import time

import typer
from rich.console import Console
from rich.table import Table

from .config import get_settings
from .logging import setup_logging

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Self-hosted Plaud clone — mirror your recordings and process them locally.",
)
auth_app = typer.Typer(help="Manage your Plaud session.")
app.add_typer(auth_app, name="auth")
console = Console()


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging.")):
    setup_logging("DEBUG" if verbose else None)


# --------------------------------------------------------------------------- #
# setup
# --------------------------------------------------------------------------- #


@app.command()
def init():
    """Create the local database and data directories."""
    from .db.session import init_db

    init_db()
    settings = get_settings()
    console.print(f"[green]✓[/] Database ready at [bold]{settings.store.database_url}[/]")
    console.print(f"[green]✓[/] Audio dir: [bold]{settings.poller.download_dir}[/]")


@auth_app.command("check")
def auth_check():
    """Verify your Plaud session works (GET /user/me)."""
    from .plaud.client import PlaudAuthError, PlaudClient

    settings = get_settings()
    try:
        with PlaudClient(settings.plaud) as client:
            me = client.check_auth()
        console.print("[green]✓[/] Authenticated to Plaud cloud.")
        uid = me.get("data", me).get("id") if isinstance(me, dict) else None
        if uid:
            console.print(f"  user id: [dim]{uid}[/]")
    except PlaudAuthError as exc:
        console.print(f"[red]✗ Auth failed:[/] {exc}")
        raise typer.Exit(1) from exc
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗ Error:[/] {exc}")
        raise typer.Exit(1) from exc


@auth_app.command("import")
def auth_import(
    curl_file: str = typer.Option(
        None, "--file", "-f", help="File with a 'Copy as cURL' command (default: read stdin)."
    ),
):
    """Parse a browser 'Copy as cURL' into config/.env lines.

    In your browser DevTools → Network, right-click an authenticated request to
    api-*.plaud.ai → Copy → Copy as cURL, then pipe it here.
    """
    from .plaud.auth import parse_curl

    raw = open(curl_file).read() if curl_file else sys.stdin.read()
    if not raw.strip():
        console.print("[red]No input.[/] Paste a cURL command or use --file.")
        raise typer.Exit(1)
    parsed = parse_curl(raw)
    console.print("[bold]Add these to your .env[/] (secrets stay out of config.toml):\n")
    if parsed.get("api_base"):
        console.print(f'LOCALPLAUD_PLAUD__API_BASE="{parsed["api_base"]}"')
    if parsed.get("token"):
        console.print(f'LOCALPLAUD_PLAUD__COOKIE="{parsed["token"]}"')
    elif parsed.get("cookie"):
        console.print(f'LOCALPLAUD_PLAUD__COOKIE="{parsed["cookie"]}"')
    headers = parsed.get("headers", {})
    keep = {k: v for k, v in headers.items() if k.lower().startswith(("x-", "app-", "timezone", "edit-from"))}
    if keep:
        console.print("\n[dim]# Plaud client headers (if auth check fails without them):[/]")
        import json

        console.print(f"LOCALPLAUD_PLAUD__EXTRA_HEADERS='{json.dumps(keep)}'")


# --------------------------------------------------------------------------- #
# sync + processing
# --------------------------------------------------------------------------- #


@app.command()
def poll(
    once: bool = typer.Option(False, "--once", help="Poll a single time and exit."),
):
    """Poll the Plaud cloud and download new/updated recordings."""
    from .poller.poll import poll_once

    settings = get_settings()
    while True:
        poll_once(settings)
        if once:
            break
        time.sleep(settings.poller.interval_seconds)


@app.command()
def work(
    once: bool = typer.Option(False, "--once", help="Process the backlog once and exit."),
):
    """Run the local pipeline on downloaded recordings."""
    from .worker.pipeline import process_pending

    settings = get_settings()
    while True:
        n = process_pending(settings)
        console.print(f"Processed {n} file(s).")
        if once:
            break
        time.sleep(max(30, settings.poller.interval_seconds // 2))


@app.command()
def run():
    """Run everything: poll on a schedule, process continuously, serve the UI."""
    import threading

    from apscheduler.schedulers.background import BackgroundScheduler

    from .poller.poll import poll_once
    from .worker.pipeline import process_pending

    settings = get_settings()
    from .db.session import init_db

    init_db()

    def cycle():
        try:
            poll_once(settings)
            process_pending(settings)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]cycle error:[/] {exc}")

    scheduler = BackgroundScheduler()
    scheduler.add_job(cycle, "interval", seconds=settings.poller.interval_seconds, id="cycle")
    scheduler.start()
    threading.Thread(target=cycle, daemon=True).start()  # kick one now
    console.print("[green]✓[/] poller + worker running; starting web UI…")
    _serve(settings)


# --------------------------------------------------------------------------- #
# query
# --------------------------------------------------------------------------- #


@app.command("ls")
def list_files(limit: int = typer.Option(30, help="Max rows.")):
    """List synced recordings and their local status."""
    from sqlalchemy import select

    from .db.models import PlaudFile
    from .db.session import session_scope

    table = Table(title="Recordings")
    for col in ("id", "filename", "status", "trans", "summary"):
        table.add_column(col)
    with session_scope() as session:
        rows = session.scalars(
            select(PlaudFile).order_by(PlaudFile.start_time_ms.desc()).limit(limit)
        )
        for r in rows:
            table.add_row(
                r.id[:10],
                (r.filename or "")[:32],
                r.status.value,
                "✓" if r.transcript else "",
                "✓" if r.summaries else "",
            )
    console.print(table)


@app.command()
def ask(question: str = typer.Argument(..., help="A question about your recordings.")):
    """Ask a question across all your transcripts (Q&A)."""
    from .worker.qa import answer

    res = answer(question)
    console.print(f"\n[bold]{res['answer']}[/]\n")
    if res["sources"]:
        console.print("[dim]Sources:[/]")
        for s in res["sources"][:5]:
            console.print(f"  • {s['filename']} [dim](score {s['score']:.2f})[/]")


@app.command()
def serve():
    """Serve the web UI only (no polling)."""
    _serve(get_settings())


def _serve(settings):
    import uvicorn

    uvicorn.run(
        "localplaud.api.app:app",
        host=settings.api.host,
        port=settings.api.port,
        log_level="info",
    )


if __name__ == "__main__":
    app()
