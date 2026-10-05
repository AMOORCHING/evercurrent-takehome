"""Command line entry point: `digest ingest`, `digest run`, `digest eval`."""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from pathlib import Path
from typing import Annotated

import typer

from digest.attach.aliases import resolving_apply
from digest.attach.calibrate import RANKERS, RankerUnavailable, build_ranker
from digest.attach.llm_render import RENDERER_MODEL_ENV, RENDERERS, RendererUnavailable, build_renderer
from digest.attach.phase import DEFAULT_WEIGHTS_PATH, PhaseRanker, load_phase_graph, load_phase_weights
from digest.attach.deciders import (
    DECIDER_MODEL_ENV,
    DECIDERS,
    JEV_KEY_ENV,
    Cascade,
    DeciderUnavailable,
    build_decider,
    save_decisions,
)
from digest.core.assemble import SlackExport
from digest.core.extract import (
    API_KEY_ENV,
    MODEL_ENV,
    ExtractorUnavailable,
    LLMExtractor,
    ReplayExtractor,
)
from digest.core.rank import load_inbox
from digest.core.render import save_digest
from digest.db import DEFAULT_DB_PATH, connect, create_schema, load_graph_seed
from digest.eval import (
    CONFIGURATIONS,
    METRICS,
    collect_runs,
    results_markdown,
    score_rows,
    unresolved_names,
    write_reliability_plots,
)
from digest.demo import demo as build_demo
from digest.explain import ExplainError, explain as explain_chain
from digest.models import Digest, Extractor, Ranker, Renderer, User
from digest.pipeline import core_apply, ingest as ingest_export

app = typer.Typer(help="Daily digest prototype.", no_args_is_help=True)


@app.command()
def ingest(
    path: Annotated[Path, typer.Argument(help="Slack export JSON. gold.json and graph_seed.json sit beside it.")],
    replay: Annotated[
        bool,
        typer.Option(help="Read gold deltas instead of calling a model. Without it, the model is "
                     f"named by {MODEL_ENV} and the API key read from {API_KEY_ENV}."),
    ] = False,
    db: Annotated[Path, typer.Option(help="SQLite database file.")] = DEFAULT_DB_PATH,
    decider: Annotated[
        str,
        typer.Option(help=f"Decider backend gating the extractor: {', '.join(DECIDERS)}. "
                     f"jev reads {JEV_KEY_ENV}, llm reads "
                     f"{DECIDER_MODEL_ENV} and {API_KEY_ENV}."),
    ] = "passthrough",
    escalate_to: Annotated[
        str,
        typer.Option(help="Second-stage decider that settles the escalated middle band; "
                     "its answer (drop or extract) is final. Same choices as --decider."),
    ] = "",
    aliases: Annotated[
        bool,
        typer.Option("--aliases", help="A4: resolve surface names through the aliases table "
                     "before applying a delta. A name that resolves nowhere is recorded in "
                     "the unresolved table (listed by `digest eval`) and that delta dropped."),
    ] = False,
) -> None:
    """Load a Slack export into signals, deltas, and the graph."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    export = SlackExport.model_validate_json(path.read_text())
    extractor: Extractor
    if replay:
        extractor = ReplayExtractor(path.parent / "gold.json")
    else:
        try:
            extractor = LLMExtractor.from_env(export)
        except ExtractorUnavailable as e:
            typer.echo(f"digest ingest: {e}, or pass --replay", err=True)
            raise typer.Exit(code=1)
    try:
        escalation = build_decider(escalate_to, export) if escalate_to else None
        cascade = Cascade(build_decider(decider, export), extractor, escalation=escalation)
    except DeciderUnavailable as e:
        typer.echo(f"digest ingest: {e}", err=True)
        raise typer.Exit(code=1)

    conn = connect(db)
    try:
        create_schema(conn)
        load_graph_seed(conn, path.parent / "graph_seed.json")
        result = ingest_export(conn, export, cascade, apply_fn=resolving_apply if aliases else core_apply)
        save_decisions(conn, cascade, decider)
    finally:
        conn.close()
    typer.echo(
        f"{result.signals} new or changed signals, {result.deltas} deltas applied, "
        f"{result.skipped} threads skipped"
    )
    if decider != "passthrough":
        routes = list(cascade.routes.values())
        typer.echo(
            f"decider {decider}: {len(routes)} decided, {routes.count('drop')} dropped, "
            f"{routes.count('escalate')} escalated"
        )
        if escalate_to:
            settled = list(cascade.escalations.values())
            typer.echo(
                f"escalation {escalate_to}: {len(settled)} re-decided, "
                f"{settled.count('drop')} dropped, {settled.count('extract')} extracted"
            )


def _run(
    conn: sqlite3.Connection, user: User, date: dt.date, ranker: Ranker, renderer: Renderer
) -> str:
    """Rank and render one user's inbox for one date, and store it over any earlier run."""
    body = renderer.render(user, ranker.rank(user, load_inbox(conn, user.id, date)))
    with conn:
        save_digest(conn, Digest(user_id=user.id, date=date, body=body))
    return body


@app.command()
def run(
    user: Annotated[str, typer.Option(help="User ID.")],
    date: Annotated[str, typer.Option(help="Digest date, YYYY-MM-DD.")],
    db: Annotated[Path, typer.Option(help="SQLite database file.")] = DEFAULT_DB_PATH,
    ranker: Annotated[
        str,
        typer.Option(help=f"Ranker: {', '.join(RANKERS)}. calibrated keeps every item that "
                     "clears its section's cost-ratio threshold instead of a top five (A2)."),
    ] = "fixed",
    phase: Annotated[
        bool,
        typer.Option("--phase", help="A3 stage-aware ranker: scores by stage distance to the "
                     "stages you own in the active process, raised by proximity to its gate; "
                     f"{DEFAULT_WEIGHTS_PATH} breaks ties. Replaces --ranker."),
    ] = False,
    render: Annotated[
        str,
        typer.Option(help=f"Renderer: {', '.join(RENDERERS)}. llm rewrites the cards into "
                     "two-line entries and falls back to the template on timeout, error, or "
                     "a response that drops or adds an item (A5). Reads "
                     f"{RENDERER_MODEL_ENV} and {API_KEY_ENV}."),
    ] = "template",
) -> None:
    """Print one person's digest for one date."""
    try:
        day = dt.date.fromisoformat(date)
    except ValueError:
        typer.echo(f"digest run: --date must be YYYY-MM-DD, got {date!r}", err=True)
        raise typer.Exit(code=1)
    if phase and ranker != "fixed":
        typer.echo("digest run: --phase replaces the ranker; drop --ranker", err=True)
        raise typer.Exit(code=1)
    try:
        chosen: Ranker = build_ranker(ranker)
        renderer: Renderer = build_renderer(render)
    except (RankerUnavailable, RendererUnavailable) as e:
        typer.echo(f"digest run: {e}", err=True)
        raise typer.Exit(code=1)

    conn = connect(db)
    try:
        create_schema(conn)
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user,)).fetchone()
        if row is None:
            typer.echo(f"digest run: unknown user {user!r}; has `digest ingest` run?", err=True)
            raise typer.Exit(code=1)
        if phase:
            try:
                chosen = PhaseRanker(load_phase_graph(conn), load_phase_weights())
            except (OSError, ValueError) as e:
                typer.echo(f"digest run: cannot load {DEFAULT_WEIGHTS_PATH}: {e}", err=True)
                raise typer.Exit(code=1)
        body = _run(conn, User.model_validate(dict(row)), day, chosen, renderer)
    finally:
        conn.close()
    typer.echo(body, nl=False)


@app.command()
def explain(
    user: Annotated[str, typer.Option(help="User ID.")],
    date: Annotated[str, typer.Option(help="Digest date, YYYY-MM-DD.")],
    item: Annotated[int, typer.Option(help="Item number as the printed digest counts cards, from 1.")],
    db: Annotated[Path, typer.Option(help="SQLite database file.")] = DEFAULT_DB_PATH,
    ranker: Annotated[
        str,
        typer.Option(help=f"Ranker the digest was run with: {', '.join(RANKERS)}. Item numbers "
                     "match `digest run` under the same ranker flags and the template renderer."),
    ] = "fixed",
    phase: Annotated[
        bool,
        typer.Option("--phase", help="The digest was run with the A3 stage-aware ranker. "
                     "Replaces --ranker."),
    ] = False,
) -> None:
    """Print one digest item's chain: item, delta, signal, decider probabilities and routing."""
    try:
        day = dt.date.fromisoformat(date)
    except ValueError:
        typer.echo(f"digest explain: --date must be YYYY-MM-DD, got {date!r}", err=True)
        raise typer.Exit(code=1)
    if phase and ranker != "fixed":
        typer.echo("digest explain: --phase replaces the ranker; drop --ranker", err=True)
        raise typer.Exit(code=1)
    try:
        chosen: Ranker = build_ranker(ranker)
    except RankerUnavailable as e:
        typer.echo(f"digest explain: {e}", err=True)
        raise typer.Exit(code=1)

    conn = connect(db)
    try:
        create_schema(conn)
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user,)).fetchone()
        if row is None:
            typer.echo(f"digest explain: unknown user {user!r}; has `digest ingest` run?", err=True)
            raise typer.Exit(code=1)
        if phase:
            try:
                chosen = PhaseRanker(load_phase_graph(conn), load_phase_weights())
            except (OSError, ValueError) as e:
                typer.echo(f"digest explain: cannot load {DEFAULT_WEIGHTS_PATH}: {e}", err=True)
                raise typer.Exit(code=1)
        try:
            trace = explain_chain(conn, User.model_validate(dict(row)), day, item, chosen)
        except ExplainError as e:
            typer.echo(f"digest explain: {e}", err=True)
            raise typer.Exit(code=1)
    finally:
        conn.close()
    typer.echo(trace, nl=False)


@app.command()
def demo(
    data: Annotated[Path, typer.Option(help="Directory with slack.json, graph_seed.json, "
                                       "gold.json and phase_weights.yaml.")] = Path("data"),
    results: Annotated[Path, typer.Option(help="results.md to print at the end.")] = Path("results.md"),
) -> None:
    """Walk the pipeline in replay mode with no API keys: a planted silo case, the digests it
    reaches, --phase digests either side of the EVT gate, an explain trace, and results.md.

    Runs on an in-memory database and writes nothing, so it works from a fresh clone.
    """
    try:
        body = build_demo(data, results)
    except (OSError, ValueError) as e:
        typer.echo(f"digest demo: {e}", err=True)
        raise typer.Exit(code=1)
    typer.echo(body, nl=False)


@app.command("eval")
def eval_(
    data: Annotated[Path, typer.Option(help="Directory with slack.json, graph_seed.json and gold.json.")] = Path("data"),
    out: Annotated[Path, typer.Option(help="Results file to write.")] = Path("results.md"),
) -> None:
    """Score every configuration against gold labels and write results.md.

    Each configuration runs in its own in-memory database, so no database file is touched. The
    llm configuration calls the model, and is reported as not run unless DIGEST_EXTRACTOR_MODEL
    and OPENAI_API_KEY are set.
    """
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    runs = collect_runs(data, CONFIGURATIONS)
    rows = score_rows(runs, CONFIGURATIONS, METRICS)
    plots = write_reliability_plots(runs, out.parent / "calibration")
    body = results_markdown(
        rows, CONFIGURATIONS, METRICS, gold_path=data / "gold.json",
        plots=[p.relative_to(out.parent).as_posix() for p in plots],
        unresolved=unresolved_names(runs),
    )
    out.write_text(body)
    typer.echo(body, nl=False)


if __name__ == "__main__":
    app()
