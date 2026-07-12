"""localplaud command-line interface."""

from __future__ import annotations

import json
import time
from pathlib import Path

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


@app.command("prepare-independent")
def prepare_independent(
    force: bool = typer.Option(
        False, "--force", help="Re-scan even when the migration marker already exists."
    ),
):
    """Preserve Plaud imports and requeue cloud-derived files for local ASR."""
    from .db.migrations import prepare_independent_mode
    from .db.models import Base
    from .db.session import get_engine

    engine = get_engine()
    Base.metadata.create_all(engine)
    counts = prepare_independent_mode(engine, force=force)
    console.print(
        "[green]✓[/] Independent-mode preparation: "
        f"[bold]{counts['requeued']}[/] requeued, "
        f"{counts['summaries']} legacy summaries relabelled, "
        f"{counts['chunks']} stale chunks removed."
    )


@app.command("acceptance-check")
def acceptance_check(
    file_id: str = typer.Argument(help="Recording ID to audit."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
):
    """Audit one recording against the subscription-independence product gate."""
    from .acceptance import subscription_independence_report
    from .db.session import init_db

    init_db()
    try:
        report = subscription_independence_report(file_id)
    except LookupError as exc:
        console.print(f"[red]✗[/] {exc}")
        raise typer.Exit(1) from exc
    if json_output:
        console.print_json(json.dumps(report, ensure_ascii=False))
    else:
        table = Table(title=f"Subscription independence · {file_id}")
        table.add_column("Check")
        table.add_column("Result")
        table.add_column("Evidence")
        for item in report["checks"]:
            table.add_row(
                item["name"],
                "[green]PASS[/]" if item["passed"] else "[red]FAIL[/]",
                item["detail"],
            )
        console.print(table)
        console.print("[green]PASS[/]" if report["passed"] else "[red]FAIL[/]")
    if not report["passed"]:
        raise typer.Exit(1)


@app.command("benchmark-recording")
def benchmark_recording_command(
    file_id: str = typer.Argument(help="Recording ID with a local canonical transcript."),
    reference: str = typer.Option(..., "--reference", help="Private reference JSON path."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
):
    """Measure transcript, speaker, timestamp, and execution quality."""
    from .benchmark import benchmark_recording, load_reference
    from .db.session import init_db

    init_db()
    try:
        report = benchmark_recording(file_id, load_reference(reference))
    except (LookupError, OSError, ValueError) as exc:
        console.print(f"[red]✗ Benchmark failed:[/] {exc}")
        raise typer.Exit(1) from exc
    if json_output:
        console.print_json(json.dumps(report, ensure_ascii=False))
        return
    table = Table(title=f"Recording benchmark · {file_id}")
    table.add_column("Metric")
    table.add_column("Value")
    for name, value in (
        ("CER", report["accuracy"]["cer"]),
        ("WER", report["accuracy"]["wer"]),
        ("DER", report["speakers"]["der"]),
        ("Speech character insertion", report["hallucination"]["speech_character_insertion_rate"]),
        ("Speech word insertion", report["hallucination"]["speech_word_insertion_rate"]),
        ("Non-speech hallucination", report["hallucination"]["non_speech_character_rate"]),
        ("Boundary MAE (s)", report["timestamps"]["boundary_mae_seconds"]),
        ("Real-time factor", report["execution"]["real_time_factor"]),
        ("Peak memory (MB)", report["execution"]["peak_memory_mb"]),
    ):
        table.add_row(name, "not recorded" if value is None else f"{value:.4f}")
    console.print(table)
    if report["execution"]["peak_memory_mb"] is None:
        console.print("[yellow]Peak memory is not recorded by current stage telemetry.[/]")


@app.command("benchmark-suite")
def benchmark_suite_command(
    manifest: str = typer.Argument(help="Private benchmark suite manifest JSON path."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    output: str | None = typer.Option(None, "--output", "-o", help="Write sanitized report JSON."),
):
    """Run and gate a private multi-recording quality suite."""
    from .benchmark import benchmark_suite, load_suite_manifest
    from .db.session import init_db

    init_db()
    try:
        suite, base_dir = load_suite_manifest(manifest)
        report = benchmark_suite(suite, base_dir)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        console.print(f"[red]✗ Benchmark suite failed:[/] {exc}")
        raise typer.Exit(1) from exc
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if output:
        Path(output).write_text(rendered + "\n", encoding="utf-8")
    if json_output:
        console.print_json(rendered)
    else:
        table = Table(title=f"Benchmark suite · {report['suite']}")
        table.add_column("Metric")
        table.add_column("Aggregate")
        table.add_column("Gate")
        thresholds = {item["metric"]: item for item in report["thresholds"]}
        for name in (
            "cer",
            "wer",
            "der",
            "speech_character_insertion_rate",
            "speech_word_insertion_rate",
            "non_speech_character_rate",
            "boundary_mae_seconds",
            "real_time_factor",
            "peak_memory_mb",
        ):
            value = report["aggregates"][name]
            gate = thresholds.get(name)
            gate_text = "—"
            if gate:
                gate_text = f"{'PASS' if gate['passed'] else 'FAIL'} ≤ {gate['maximum']:.4f}"
            table.add_row(name, "not recorded" if value is None else f"{value:.4f}", gate_text)
        console.print(table)
        counts = report["case_counts"]
        console.print(
            f"{counts['completed']}/{counts['total']} cases completed · "
            + ("[green]PASS[/]" if report["passed"] else "[red]FAIL[/]")
        )
    if not report["passed"]:
        raise typer.Exit(1)


@auth_app.command("check")
def auth_check():
    """Verify your Plaud session works (whoami against the configured provider)."""
    from .plaud import make_plaud_client
    from .plaud.common import PlaudAuthError

    settings = get_settings()
    try:
        with make_plaud_client(settings.plaud) as client:
            me = client.check_auth()
        console.print(
            f"[green]✓[/] Authenticated to Plaud cloud "
            f"([bold]{settings.plaud.provider}[/] provider)."
        )
        data = me.get("data", me) if isinstance(me, dict) else None
        if isinstance(data, dict):
            for key in ("id", "email", "nickname"):
                if data.get(key):
                    console.print(f"  {key}: [dim]{data[key]}[/]")
    except PlaudAuthError as exc:
        console.print(f"[red]✗ Auth failed:[/] {exc}")
        raise typer.Exit(1) from exc
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗ Error:[/] {exc}")
        raise typer.Exit(1) from exc


@auth_app.command("login")
def auth_login():
    """Sign in to the official Plaud Open API (one-time browser OAuth).

    Uses native S256 PKCE, opens your browser, and saves an auto-refreshing
    token set compatible with the official Plaud CLI. No Node.js is required.
    """
    settings = get_settings()
    from .plaud.oauth import OAuthError, native_login

    console.print("Opening Plaud authorization in your browser…")
    try:
        tokens_path = native_login(
            settings.plaud.official,
            show_manual_url=lambda url: console.print(
                f"Could not open a browser. Open this URL manually:\n[link={url}]{url}[/link]"
            ),
        )
    except OAuthError as exc:
        console.print(f"[red]✗ Login failed:[/] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"[green]✓[/] Signed in; tokens saved to [bold]{tokens_path}[/].")
    auth_check()


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
        try:
            poll_once(settings)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]poll error:[/] {exc}")
        if once:
            break
        time.sleep(settings.poller.interval_seconds)


@app.command()
def work(
    once: bool = typer.Option(False, "--once", help="Process the backlog once and exit."),
    force: bool = typer.Option(
        False, "--force", help="Recompute all stages, ignoring cached artifacts."
    ),
):
    """Run the local pipeline on downloaded recordings."""
    from .worker.pipeline import process_pending

    settings = get_settings()
    while True:
        try:
            n = process_pending(settings, force=force)
            console.print(f"Processed {n} file(s).")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]work error:[/] {exc}")
        if once:
            break
        time.sleep(max(30, settings.poller.interval_seconds // 2))


@app.command()
def export(
    file_id: str = typer.Argument(..., help="Recording id (see `localplaud ls`)."),
    out: str = typer.Option(
        None, "--out", "-o", help="Output path (default: alongside the audio)."
    ),
):
    """Export a recording's transcript + summaries to a Markdown file."""
    from .exporter import export_to_file

    try:
        path = export_to_file(file_id, out)
    except ValueError as exc:
        console.print(f"[red]✗[/] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"[green]✓[/] Exported to [bold]{path}[/]")


@app.command()
def run():
    """Run everything: poll on a schedule, process continuously, serve the UI."""
    import threading
    from datetime import datetime

    from apscheduler.schedulers.background import BackgroundScheduler

    from .db.session import init_db
    from .poller.poll import poll_once
    from .worker.pipeline import process_pending

    settings = get_settings()
    init_db()

    # A non-blocking lock guarantees cycles never overlap even if one runs
    # longer than the interval (ASR can take minutes) — a second firing simply
    # skips rather than double-downloading / double-processing.
    lock = threading.Lock()

    def cycle():
        if not lock.acquire(blocking=False):
            return
        try:
            poll_once(settings)
            process_pending(settings, limit=settings.pipeline.files_per_cycle)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]cycle error:[/] {exc}")
        finally:
            lock.release()

    scheduler = BackgroundScheduler()
    # next_run_time fires the first cycle immediately, under the same
    # single-instance governance as the interval (no separate racing thread).
    scheduler.add_job(
        cycle,
        "interval",
        seconds=settings.poller.interval_seconds,
        id="cycle",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(),
    )
    scheduler.start()
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
            transcript = (
                r.local_transcript
                if get_settings().pipeline.artifact_mode == "independent"
                else r.transcript
            )
            table.add_row(
                r.id[:10],
                r.display_title[:32],
                r.status.value,
                "✓" if transcript else "",
                "✓" if any(s.source == "local" for s in r.summaries) else "",
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
def status():
    """Show a count of recordings by local processing status."""
    from sqlalchemy import func, select

    from .db.models import FileStatus, PlaudFile
    from .db.session import session_scope

    table = Table(title="Pipeline status")
    table.add_column("status")
    table.add_column("count", justify="right")
    with session_scope() as session:
        counts = dict(
            session.execute(select(PlaudFile.status, func.count()).group_by(PlaudFile.status)).all()
        )
    for st in FileStatus:
        table.add_row(st.value, str(counts.get(st, 0)))
    console.print(table)


@app.command()
def reprocess(
    file_id: str = typer.Argument(..., help="Recording id to re-run the pipeline on."),
    force: bool = typer.Option(
        False,
        "--force/--resume",
        help="Resume from existing artifacts (default) or recompute all stages.",
    ),
):
    """Re-run the local pipeline on one recording."""
    from .db.models import FileStatus, PlaudFile
    from .db.session import session_scope
    from .worker.pipeline import process_file, processing_claim_active, reset_pipeline_retry

    with session_scope() as session:
        r = session.get(PlaudFile, file_id)
        if r is None:
            console.print(f"[red]✗[/] no such file: {file_id}")
            raise typer.Exit(1)
        if not r.audio_path:
            console.print(f"[red]✗[/] {file_id} has no downloaded audio")
            raise typer.Exit(1)
        if processing_claim_active(r):
            console.print(f"[yellow]✗[/] {file_id} is already processing")
            raise typer.Exit(1)
        r.status = FileStatus.downloaded
        reset_pipeline_retry(r)
    process_file(file_id, force=force)
    console.print(f"[green]✓[/] reprocessed {file_id}")


@app.command()
def doctor():
    """Check the environment: ffmpeg, configured providers, and Plaud auth."""
    settings = get_settings()
    table = Table(title="localplaud doctor")
    table.add_column("check")
    table.add_column("result")

    def row(name: str, ok: bool, detail: str = ""):
        mark = "[green]✓[/]" if ok else "[red]✗[/]"
        table.add_row(name, f"{mark} {detail}".strip())

    from .worker.convert import ffmpeg_available

    row(
        "ffmpeg",
        ffmpeg_available(),
        "on PATH" if ffmpeg_available() else "missing (needed to transcode)",
    )

    from .worker.diarize import health as diarization_health

    diarize_ok, diarize_detail = diarization_health(settings.diarize)
    row(f"diarization:{settings.diarize.provider}", diarize_ok, diarize_detail)

    try:
        from .asr.registry import build_provider

        p = build_provider(settings.asr.provider, settings.asr)
        health = getattr(p, "health", None)
        if callable(health):
            ok, detail = health()
            row(f"asr:{settings.asr.provider}", ok, detail)
        else:
            row(f"asr:{settings.asr.provider}", p.available())
    except Exception as exc:  # noqa: BLE001
        row(f"asr:{settings.asr.provider}", False, str(exc)[:60])

    try:
        from .llm.base import build_llm

        provider = build_llm(settings.llm)
        health = getattr(provider, "health", None)
        if callable(health):
            ok, detail = health()
            row(f"llm:{settings.llm.provider}", ok, detail)
        else:
            row(f"llm:{settings.llm.provider}", provider.available())
    except Exception as exc:  # noqa: BLE001
        row(f"llm:{settings.llm.provider}", False, str(exc)[:60])

    try:
        from .embeddings.base import build_embedder

        provider = build_embedder(settings.embeddings)
        health = getattr(provider, "health", None)
        if callable(health):
            ok, detail = health()
            row(f"embeddings:{settings.embeddings.provider}", ok, detail)
        else:
            row(f"embeddings:{settings.embeddings.provider}", provider.available())
    except Exception as exc:  # noqa: BLE001
        row(f"embeddings:{settings.embeddings.provider}", False, str(exc)[:60])

    if settings.plaud.provider == "official":
        from .plaud.oauth import OfficialTokenStore

        st = OfficialTokenStore(
            settings.plaud.official.tokens_path, settings.plaud.official.refresh_url
        ).status()
        row(
            "plaud auth (official)",
            st["ok"],
            st["detail"] if st["ok"] else "run `localplaud auth login`",
        )
    elif settings.plaud.provider == "mcp":
        try:
            from .plaud import make_plaud_client

            with make_plaud_client(settings.plaud) as client:
                client.check_auth()
            row("plaud auth (mcp)", True, "official MCP OAuth and read tool verified")
        except Exception as exc:  # noqa: BLE001
            row("plaud auth (mcp)", False, str(exc)[:60])

    console.print(table)


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
