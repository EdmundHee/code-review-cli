"""Rich live dashboard for the poll loop (``ghcr tui``).

Splits the terminal into four panels — monitoring, latest DeepSeek response,
cost per PR, and a job-event log — fed by the in-process ``EventBus``.

Threading model: the poll loop runs in a daemon worker thread and owns the
``StateStore`` (sqlite connections are thread-bound). The main thread only
renders. The dashboard therefore never touches the DB after the worker starts;
it seeds once up front and tracks 24h spend live from ``PrOutcome`` events.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .config import Config
from .events import (
    AgentEvent,
    ConfigReloaded,
    CycleStarted,
    DeepSeekDone,
    EventBus,
    LogLine,
    PrOutcome,
    RepoDone,
    RepoListed,
)
from .github import GhClient
from .models import ACTION_SKIP_SEEN
from .poller import baseline_if_first_run, preflight
from .state import StateStore

_REVIEW_OUTCOMES = ("review", "reviewed")


def _blank_repo() -> dict:
    """Default per-repo render slot. ``polled_at`` is the UTC time the repo last
    finished a poll (drives the "time since last poll" freshness column)."""
    return {"open_prs": None, "last": "—", "polled_at": None}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hhmmss(iso_or_dt) -> str:
    """Best-effort HH:MM:SS from an ISO string or datetime; '' on failure."""
    try:
        dt = iso_or_dt if isinstance(iso_or_dt, datetime) else datetime.fromisoformat(iso_or_dt)
        return dt.strftime("%H:%M:%S")
    except Exception:
        return ""


def _fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def _outcome_detail(pr_number: int, action: str, head_sha: str, cost_usd: float) -> str:
    """One-line per-PR outcome, e.g. '#2 review a1b2c3d $0.0180'.

    Includes the short head SHA so the dashboard shows *which* commit was acted
    on; the SHA is omitted when unknown (older rows / events without it).
    """
    sha = (head_sha or "")[:7]
    sha_part = f" {sha}" if sha else ""
    return f"#{pr_number} {action}{sha_part} ${cost_usd:.4f}"


class DashboardState:
    """Render state, mutated only via ``apply`` under a lock."""

    def __init__(self, cfg: Config):
        self.lock = threading.Lock()
        self.started_at = _utcnow()
        self.bot_login = cfg.github.bot_login
        self.model = cfg.deepseek.model
        self.budget = cfg.budgets.daily_usd_budget
        self.interval_s = cfg.poll_interval_seconds
        self.repos: dict[str, dict] = {r: _blank_repo() for r in cfg.repos}
        # The repo currently being polled (set on RepoListed, cleared on RepoDone)
        # so the dashboard can highlight exactly one active row, none while idle.
        self.active_repo: str | None = None
        self.latest: DeepSeekDone | None = None
        self.cost_rows: deque = deque(maxlen=50)
        self.events: deque = deque(maxlen=200)
        self.spent_24h = 0.0
        # Live sub-agents (lenses + scorers) for the PR currently under review.
        self.agents_pr: tuple | None = None
        self.agents: dict[str, dict] = {}

    # -- seeding (main thread, before the worker starts) -----------------
    def seed(self, rows, spent_24h: float) -> None:
        self.spent_24h = spent_24h
        for row in reversed(list(rows)):  # oldest first so newest ends up last
            repo, num = row["repo"], row["pr_number"]
            outcome, cost = row["outcome"], float(row["cost_usd"] or 0.0)
            sha = row["head_sha"] if "head_sha" in row.keys() else ""
            detail = _outcome_detail(num, outcome, sha, cost)
            if repo in self.repos:
                self.repos[repo]["last"] = detail
            if outcome in _REVIEW_OUTCOMES or cost > 0:
                self.cost_rows.append({"pr": num, "repo": repo, "cost": cost, "outcome": outcome})
            self.events.append(f"{_hhmmss(row['created_at'])} {repo} {detail}")

    # -- live events (worker thread) -------------------------------------
    def apply(self, evt: object) -> None:
        with self.lock:
            reduce(self, evt)


def reduce(state: DashboardState, evt: object) -> None:
    """Fold one event into ``state``. Caller holds the lock. No Rich here."""
    if isinstance(evt, CycleStarted):
        state.interval_s = evt.interval_s
        state.events.append(f"{_hhmmss(_utcnow())} cycle: {evt.repo_count} repo(s) every {evt.interval_s}s")
    elif isinstance(evt, ConfigReloaded):
        state.interval_s = evt.interval_s
        state.budget = evt.budget
        for repo in evt.repos:  # add newly-configured repos
            state.repos.setdefault(repo, _blank_repo())
        for repo in [r for r in state.repos if r not in evt.repos]:  # drop removed ones
            del state.repos[repo]
        state.events.append(f"{_hhmmss(_utcnow())} config reloaded: {len(evt.repos)} repo(s)")
    elif isinstance(evt, RepoListed):
        if evt.repo not in state.repos:
            state.repos[evt.repo] = _blank_repo()
        state.repos[evt.repo]["open_prs"] = evt.open_prs
        state.active_repo = evt.repo  # start of this repo's poll → highlight it
    elif isinstance(evt, RepoDone):
        if evt.repo not in state.repos:
            state.repos[evt.repo] = _blank_repo()
        state.repos[evt.repo]["polled_at"] = _utcnow()  # reset freshness clock
        if state.active_repo == evt.repo:
            state.active_repo = None  # done → drop the highlight (idle until next)
    elif isinstance(evt, AgentEvent):
        key = (evt.repo, evt.pr_number)
        if key != state.agents_pr:  # a new PR started → reset the agent board
            state.agents_pr = key
            state.agents = {}
        state.agents[evt.agent] = {"status": evt.status, "detail": evt.detail}
        if evt.status == "failed":  # surface failures in the history log too
            state.events.append(f"{_hhmmss(_utcnow())} {evt.repo}#{evt.pr_number} {evt.agent} failed: {evt.detail}")
    elif isinstance(evt, DeepSeekDone):
        state.latest = evt
        state.events.append(
            f"{_hhmmss(_utcnow())} deepseek {evt.repo}#{evt.pr_number} "
            f"{evt.prompt_tokens}→{evt.completion_tokens} tok ({evt.latency_s:.1f}s)"
        )
    elif isinstance(evt, PrOutcome):
        if evt.repo not in state.repos:
            state.repos[evt.repo] = _blank_repo()
        # skip_seen fires every poll cycle for any already-reviewed PR and writes
        # no DB row — it carries no new info. Letting it through would clobber the
        # recorded review line ("#2 review $0.018") with "#2 skip_seen $0.0000" and
        # spam the event log every interval. Drop it; the review stays on display.
        if evt.action == ACTION_SKIP_SEEN:
            return
        mark = "✓" if evt.action in _REVIEW_OUTCOMES else evt.action
        detail = _outcome_detail(evt.pr_number, evt.action, evt.head_sha, evt.cost_usd)
        state.repos[evt.repo]["last"] = detail
        if evt.action in _REVIEW_OUTCOMES or evt.cost_usd > 0:
            state.cost_rows.append(
                {"pr": evt.pr_number, "repo": evt.repo, "cost": evt.cost_usd, "outcome": evt.action}
            )
            state.spent_24h += evt.cost_usd
        state.events.append(f"{_hhmmss(_utcnow())} {evt.repo} {detail} {mark}")
    elif isinstance(evt, LogLine):
        state.events.append(f"{evt.ts} {evt.name}: {evt.message}")


# -- rendering ----------------------------------------------------------------

def _header(state: DashboardState) -> Panel:
    up = _fmt_uptime((_utcnow() - state.started_at).total_seconds())
    spent = state.spent_24h
    color = "red" if spent >= state.budget else "green"
    text = Text.assemble(
        ("ghcr", "bold cyan"), " · ",
        (state.bot_login, "bold"), " · ",
        (state.model, "magenta"), " · ",
        f"up {up}", " · ",
        ("24h ", "dim"), (f"${spent:.3f}/${state.budget:.2f}", color),
    )
    return Panel(text, border_style="cyan")


def _activity(state: DashboardState, repo: str) -> str:
    """The activity / freshness cell for a repo row.

    While ``repo`` is the active poll target AND a multi-pass review is emitting
    agent events for one of *its* PRs, show live progress ("reviewing #N ·
    done/total") so a minutes-long review reads as *working*, not as the
    ambiguous "polling…" that's indistinguishable from a hang. The agents board
    lags one PR behind ``active_repo`` (it resets only on the next PR's first
    agent event), so the ``ap[0] == repo`` guard stops a freshly-active repo from
    borrowing the previous repo's progress. Otherwise: bare "polling…" (active
    but pre-lens), time-since-last-poll, or "—" if never polled.
    """
    if repo == state.active_repo:
        ap = state.agents_pr
        if ap is not None and ap[0] == repo and state.agents:
            done = sum(1 for a in state.agents.values() if a["status"] == "done")
            return f"reviewing #{ap[1]} · {done}/{len(state.agents)}"
        return "polling…"
    polled_at = state.repos[repo]["polled_at"]
    if polled_at is not None:
        return f"{_fmt_uptime((_utcnow() - polled_at).total_seconds())} ago"
    return "—"


def _monitoring(state: DashboardState) -> Panel:
    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(style="bold", no_wrap=True)       # repo
    t.add_column(justify="right", no_wrap=True)     # open PR count
    t.add_column(justify="right", no_wrap=True)     # poll freshness / live progress
    t.add_column(ratio=1, overflow="ellipsis")      # last outcome
    for repo, st in state.repos.items():
        prs = "?" if st["open_prs"] is None else str(st["open_prs"])
        # Full-row reverse highlight marks the repo under active poll; cleared on
        # RepoDone so nothing is highlighted during the inter-cycle sleep.
        row_style = "reverse" if repo == state.active_repo else ""
        t.add_row(repo, f"{prs} open", _activity(state, repo), st["last"], style=row_style)
    return Panel(t, title="MONITORING", border_style="blue", title_align="left")


def _deepseek(state: DashboardState) -> Panel:
    d = state.latest
    if d is None:
        body: object = Text("waiting for first review…", style="dim italic")
    else:
        head = Text(
            f"{d.repo} #{d.pr_number} · {d.prompt_tokens}→{d.completion_tokens} tok "
            f"· {d.latency_s:.1f}s",
            style="bold green",
        )
        title = Text(d.title or "", style="dim")
        snippet = Text(d.snippet, overflow="fold")
        body = Group(head, title, Text(""), snippet)
    return Panel(body, title="DEEPSEEK RESPONSE (latest)", border_style="green", title_align="left")


_AGENT_GLYPH = {"running": "⠿", "done": "✓", "failed": "✗"}
_AGENT_STYLE = {"running": "yellow", "done": "green", "failed": "red"}


def _agents(state: DashboardState) -> Panel:
    title = "AGENTS"
    if state.agents_pr:
        repo, num = state.agents_pr
        title += f" · {repo.split('/')[-1]}#{num}"
    if not state.agents:
        return Panel(Text("no active agents", style="dim italic"), title=title, border_style="magenta", title_align="left")
    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(no_wrap=True)
    t.add_column(no_wrap=True)
    t.add_column(ratio=1, overflow="ellipsis")
    for agent, st in state.agents.items():
        status = st["status"]
        t.add_row(
            Text(_AGENT_GLYPH.get(status, "·"), style=_AGENT_STYLE.get(status, "")),
            Text(agent, style="bold" if status == "running" else ""),
            Text(st["detail"], style="dim"),
        )
    done = sum(1 for s in state.agents.values() if s["status"] == "done")
    return Panel(
        t, title=title, border_style="magenta", title_align="left",
        subtitle=f"{done}/{len(state.agents)} done", subtitle_align="right",
    )


def _cost(state: DashboardState) -> Panel:
    t = Table.grid(padding=(0, 1), expand=True)
    t.add_column(no_wrap=True)
    t.add_column(ratio=1, overflow="ellipsis")
    t.add_column(justify="right", no_wrap=True)
    for row in reversed(state.cost_rows):  # newest first (region crops to top)
        mark = "✓" if row["outcome"] in _REVIEW_OUTCOMES else "·"
        t.add_row(f"#{row['pr']}", row["repo"], f"${row['cost']:.4f} {mark}")
    return Panel(
        t, title="COST PER PR", border_style="yellow", title_align="left",
        subtitle=f"24h ${state.spent_24h:.3f} / ${state.budget:.2f}", subtitle_align="right",
    )


def _history(state: DashboardState) -> Panel:
    lines = list(state.events)[-100:]
    body = Text("\n".join(reversed(lines)), overflow="crop")  # newest on top
    return Panel(body, title="JOB EVENT HISTORY", border_style="white", title_align="left")


def build_layout(state: DashboardState) -> Layout:
    with state.lock:
        header = _header(state)
        mon, ds, agents, cost, hist = (
            _monitoring(state), _deepseek(state), _agents(state), _cost(state), _history(state)
        )
    layout = Layout()
    layout.split_column(Layout(header, name="header", size=3), Layout(name="body"))
    layout["body"].split_row(Layout(name="left"), Layout(name="right"))
    layout["left"].split_column(Layout(mon, name="monitoring"), Layout(cost, name="cost"))
    layout["right"].split_column(
        Layout(ds, name="deepseek"), Layout(agents, name="agents"), Layout(hist, name="history")
    )
    return layout


# -- logging bridge -----------------------------------------------------------

class TuiLogHandler(logging.Handler):
    """Route log records into the job-event panel via the bus."""

    def __init__(self, bus: EventBus):
        super().__init__()
        self.bus = bus

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.bus.publish(LogLine(
                ts=datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                level=record.levelname, name=record.name, message=record.getMessage(),
            ))
        except Exception:
            pass  # never let logging crash the dashboard


def _reconfigure_logging(cfg: Config, bus: EventBus) -> None:
    """Detach stdout (it would corrupt the TUI); send logs to the panel + a file."""
    import os

    log_dir = os.path.dirname(os.path.expanduser(cfg.db_path)) or "."
    os.makedirs(log_dir, exist_ok=True)
    fh = logging.FileHandler(os.path.join(log_dir, "ghcr.log"))
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(TuiLogHandler(bus))
    root.addHandler(fh)
    root.setLevel(getattr(logging, cfg.log_level, logging.INFO))


# -- entrypoint ---------------------------------------------------------------

def run_tui(cfg: Config, config_path: str | None = None) -> int:
    from .cli import _build  # lazy: avoid import cycle (cli imports tui in cmd_tui)

    bus = EventBus()
    state = DashboardState(cfg)
    bus.subscribe(state.apply)

    # Preflight in the main thread so identity errors print cleanly (pre-TUI).
    gh = GhClient(cfg.github.gh_path, cfg.github.token, cfg.github.request_timeout_seconds)
    preflight(gh, cfg)

    # Seed once from the DB in the main thread, then never touch it here again.
    seed_store = StateStore(cfg.db_path)
    cutoff = _utcnow() - timedelta(hours=24)
    state.seed(seed_store.recent(20), seed_store.usd_spent_since(cutoff))
    seed_store.close()

    _reconfigure_logging(cfg, bus)

    holder: dict = {}

    def _serve() -> None:
        # Build clients (incl. the StateStore) in THIS thread — sqlite is thread-bound.
        w_gh, _ds, store, orch = _build(cfg, bus=bus)
        baseline_if_first_run(w_gh, store, cfg)
        from .poller import PollLoop
        loop = PollLoop(orch, w_gh, cfg, bus=bus, config_path=config_path)
        holder["loop"] = loop
        loop.run_forever(install_signals=False)

    worker = threading.Thread(target=_serve, name="ghcr-poll", daemon=True)
    worker.start()

    try:
        with Live(build_layout(state), screen=True, refresh_per_second=4) as live:
            while worker.is_alive():
                live.update(build_layout(state))
                time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        loop = holder.get("loop")
        if loop is not None:
            loop.request_stop()
        worker.join(timeout=5)
    return 0
