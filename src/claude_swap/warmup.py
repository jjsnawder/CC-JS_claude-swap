"""Warmup: keep every managed account's 5-hour window running, staggered.

An account whose 5-hour window has lapsed reports **no reset stamp at all**
(``oauth.build_usage_result`` writes ``resets_at`` only when the API sends
one), so "when does this account come back" is unknowable until the account
is actually used. This module closes that gap by sending a tiny headless
Claude Code hello — a real ``claude -p`` session with the account's own
config dir, never a raw API call with its OAuth token — and spaces those
hellos so the accounts' reset times end up spread across the day instead of
bunched wherever history left them.

Three separable parts, all unit-testable without a network or a child
process:

* :func:`window_state` — cold detection off a usage dict.
* :func:`plan_warmups` — the pure stagger planner. No I/O, no clock of its
  own, no knowledge of accounts beyond the numbers and window states it is
  handed.
* :class:`PingRunner` / :class:`SubprocessPingRunner` — the one seam that
  spawns a process. Injected everywhere, so tests never run ``claude``.

:func:`warm_now` glues them together for the CLI (``cswap warm``) and the
TUI dashboard; :class:`~claude_swap.autoswitch.AutoSwitchEngine` drives the
same pieces asynchronously from its tick.

Nothing here ever logs, returns, or embeds credential material, an
environment block, or a config-dir path: a warmup failure is reported as an
exit code plus the first :data:`STDERR_CAP` characters of the child's
stderr.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from claude_swap import oauth, paths, poll_policy
from claude_swap.session import AUTH_OVERRIDE_ENV_VARS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from claude_swap.switcher import ClaudeAccountSwitcher
    from claude_swap.usage_store import UsageEntry

_logger = logging.getLogger("claude-swap")

#: The 5-hour window this module staggers, in seconds.
PERIOD_S = 5 * 60 * 60.0

#: Hard ceiling on one hello. Two minutes is ~40x the measured round trip;
#: past it the child is killed by PID (our own handle) rather than left to
#: hold a slot's in-flight marker forever.
PING_TIMEOUT_S = 120.0

#: Model alias for a plain 5-hour warmup (cheapest thing that starts a
#: window). Model-window warmups override it with the window's own label.
DEFAULT_MODEL = "haiku"

#: At most one model-window hello per (account, label) per this long. A
#: Fable hello is noise against a weekly window, but it is still a Fable
#: call — this is what keeps an API quirk (a window that never reports a
#: stamp) from looping it every tick.
MODEL_PING_INTERVAL_S = 24 * 60 * 60.0

#: Minimum time between two hellos to the same account, whatever the
#: outcome. This is the ONLY thing that stops a re-ping loop, because the
#: post-ping refetch cannot be forced: ``UsageStore.reserve`` with
#: ``respect_plans=False`` still gates on ``poll_due or stale``
#: (``usage_store._row_eligible``), so an account fetched earlier in the same
#: tick is neither and its row keeps the pre-hello (cold) stamp until either
#: its poll plan comes due or ``SERVE_TTL_S`` elapses. Without this cooldown
#: a cold account would be re-pinged every tick across that gap. 15 minutes
#: comfortably covers the 3-minute serve TTL and any candidate poll plan.
PING_COOLDOWN_S = 15 * 60.0

#: Per-account backoff after a failed hello, and the escalation after
#: ``FAILURE_STRIKES`` consecutive failures.
FAILURE_BACKOFF_S = 30 * 60.0
FAILURE_STRIKES = 3
FAILURE_LONG_BACKOFF_S = 24 * 60 * 60.0

#: How much of the child's stderr a failure carries. Enough to name the
#: cause, short enough that a stack trace or a dumped config cannot ride
#: along into an event log.
STDERR_CAP = 200

# Redaction applied to the child's stderr BEFORE it is capped and quoted.
# The tail of a failing `claude` run is the one useful diagnostic we have, so
# it is quoted verbatim — which means it is also the one place a token or a
# slot path could ride out of this module into an event log, a JSON payload
# or a pasted issue. Order matters: token shapes first (a bearer header is
# also base64-ish), then paths, then the generic long-run catch-all.
_REDACTIONS = (
    (re.compile(r"sk-ant-[\w-]+"), "<token>"),
    (re.compile(r"(?i)bearer\s+\S+"), "<token>"),
    # Any path-looking run naming a session profile or a config home.
    (re.compile(r"\S*(?:sessions|\.claude)\S*"), "<path>"),
    (re.compile(r"[A-Za-z0-9+/=_-]{40,}"), "<redacted>"),
)


def redact_child_output(text: str) -> str:
    """Strip token- and path-shaped runs from a child's output, then cap it.

    Deliberately over-eager: a false positive costs a word of diagnostic, a
    false negative costs a credential rotation in a PUBLIC repo.
    """
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text.strip()[:STDERR_CAP]


def safe_error(e: BaseException) -> str:
    """A reportable one-liner for an exception, with no payload.

    ``OSError`` puts the filename in ``str(e)`` — for us that is a slot or
    config-dir path — and ``SessionError`` messages embed both. Only the
    type survives, plus ``strerror`` for an OSError (``"Permission denied"``
    names the cause and contains no path). Full text goes to the debug log.
    """
    if isinstance(e, OSError) and e.strerror:
        return f"{type(e).__name__}: {e.strerror}"
    return type(e).__name__


#: Subdirectory of the backup root used as the hello's cwd. Empty by
#: design: ``--no-session-persistence`` means no transcript is written, and
#: giving the child a directory of our own keeps it out of whatever
#: directory the TUI/CLI happened to be started from.
WARMUP_DIRNAME = "warmup"


# ---------------------------------------------------------------------------
# Cold detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowState:
    """What one account's usage says about its warmup needs.

    ``five_hour_cold`` is the whole point: the 5-hour stamp is absent (never
    used, or reset and untouched since) or already past. ``reset_ts`` is the
    stamp when it is genuinely in the future — the planner's phase input.
    """

    five_hour_cold: bool = False
    reset_ts: float | None = None
    cold_models: tuple[str, ...] = ()
    at_limit: bool = False
    readable: bool = False


def window_state(
    usage: dict | str | None,
    now: float,
    models: Sequence[str] = (),
) -> WindowState:
    """Cold/at-limit classification for one account's usage snapshot.

    Reads ``usage["five_hour"]["resets_at"]`` RAW rather than going through
    ``autoswitch._five_hour_reset_ts``, which folds "past" into "unknown".
    Here the two mean the same thing — but they mean it for different
    reasons, and the rule ("no stamp, or a stamp that has elapsed, means the
    window is not running") is worth keeping explicit at the one place that
    decides whether to spend a token on it.

    A non-dict ``usage`` (a sentinel, or unknown) is NOT cold: an account
    whose usage cannot be read is never pinged. ``readable`` says which case
    the caller is looking at.
    """
    if not isinstance(usage, dict):
        return WindowState()
    five = usage.get("five_hour")
    raw_reset = five.get("resets_at") if isinstance(five, dict) else None
    reset_ts = poll_policy.parse_reset_ts(raw_reset)
    if reset_ts is not None and reset_ts <= now:
        reset_ts = None
    windows = oauth.relevant_windows(usage, models)
    at_limit = any(pct >= 100.0 for _, pct, _ in windows)
    # relevant_windows() returns 5h/7d first, then the scoped per-model
    # windows in order — slice past the account-wide pair rather than
    # matching on the label, which a display name could collide with.
    base = len(oauth.relevant_windows(usage, ()))
    cold_models = tuple(
        name
        for name, _, resets_at in windows[base:]
        if (ts := poll_policy.parse_reset_ts(resets_at)) is None or ts <= now
    )
    return WindowState(
        five_hour_cold=reset_ts is None,
        reset_ts=reset_ts,
        cold_models=cold_models,
        at_limit=at_limit,
        readable=True,
    )


def model_alias(label: str) -> str:
    """CLI ``--model`` alias for a scoped window's display name.

    Verified against claude 2.1.x: ``--model fable`` resolves to
    ``claude-fable-5-1``. Lowercasing the display name is the whole mapping.
    """
    return label.strip().lower() or DEFAULT_MODEL


# ---------------------------------------------------------------------------
# The stagger planner (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WarmupAccount:
    """One eligible account as the planner sees it: a number and a state.

    ``state.cold_models`` must ALREADY be filtered by the caller's per-day
    guard — the planner has no state file and no memory.
    """

    number: str
    state: WindowState


@dataclass(frozen=True)
class WarmupDecision:
    """``ping_now`` (with the model to use) or ``wait_until`` an instant."""

    number: str
    action: str  # "ping_now" | "wait_until"
    model: str = DEFAULT_MODEL
    at_ts: float | None = None
    reason: str = ""

    @property
    def is_now(self) -> bool:
        return self.action == "ping_now"


def _largest_gap_midpoint(phases: Sequence[float], period: float) -> float:
    """Midpoint of the largest circular gap between ``phases`` (mod period).

    A single phase yields the antipode. Ties keep the FIRST gap in ascending
    phase order, so the bootstrap is deterministic for a given ``now``. Which
    ACCOUNT lands in which slot still depends on ``now mod period`` (at 280
    minutes the third and fourth cold accounts swap); what is invariant is
    the resulting phase SET — evenly spaced by ``period / N``, which is the
    property that matters.
    """
    ordered = sorted(p % period for p in phases)
    best_start = ordered[0]
    best_gap = period
    if len(ordered) > 1:
        best_gap = -1.0
        for i, start in enumerate(ordered):
            end = ordered[i + 1] if i + 1 < len(ordered) else ordered[0] + period
            gap = end - start
            if gap > best_gap:
                best_gap = gap
                best_start = start
    return (best_start + best_gap / 2.0) % period


def _signed_offset(value: float, target: float, period: float) -> float:
    """``value - target`` folded into ``[-period/2, period/2)``."""
    d = (value - target) % period
    if d >= period / 2.0:
        d -= period
    return d


def plan_warmups(
    now: float,
    accounts: Sequence[WarmupAccount],
    *,
    period: float = PERIOD_S,
    stagger: bool = True,
) -> list[WarmupDecision]:
    """Decide, for each eligible account, whether to hello now or later.

    ``accounts`` is the ELIGIBLE set E — the caller has already dropped
    disabled, API-key, quarantined, token-dead, in-backoff, at-limit,
    unreadable, in-flight and warmup-backed-off slots. ``spacing =
    period / |E|`` and ``tolerance = spacing / 2``.

    Warm accounts contribute a phase (``reset_ts mod period``). Cold
    accounts are processed in slot order: the first one, with no warm phase
    to hang off, pings immediately as the bootstrap anchor; every later one
    aims its NEW reset (``now + period``) at the midpoint of the largest gap
    between the phases known so far, and pings now only when it is already
    within ``tolerance`` of that midpoint. A scheduled account then counts
    as warm at its planned phase, so the next cold account plans against it
    rather than against the same gap.

    Worst case for a cold account with at least one warm peer is
    ``period * (1 - 1/(2N))`` — 4h22 at N=4 — since the wait is at most a
    full period minus half a spacing. A cold account is fully usable while
    it waits (a switch onto it starts a fresh window), so the wait costs
    reset-nearness, never quota.

    ``stagger=False`` degrades to plain keep-alive: every cold account pings
    now.

    A 5-hour-WARM account with a cold model window pings immediately: its
    5-hour phase is already running, so an extra hello cannot disturb it.
    """
    if not accounts:
        return []
    spacing = period / len(accounts)
    tolerance = spacing / 2.0

    phases: list[float] = []
    decisions: list[WarmupDecision] = []
    for acct in accounts:
        if not acct.state.five_hour_cold and acct.state.reset_ts is not None:
            phases.append(acct.state.reset_ts % period)

    for acct in accounts:
        state = acct.state
        model = (
            model_alias(state.cold_models[0]) if state.cold_models else DEFAULT_MODEL
        )
        if not state.five_hour_cold:
            if state.cold_models:
                decisions.append(
                    WarmupDecision(
                        number=acct.number,
                        action="ping_now",
                        model=model,
                        reason="model-window-cold",
                    )
                )
            continue
        if not stagger or not phases:
            decisions.append(
                WarmupDecision(
                    number=acct.number,
                    action="ping_now",
                    model=model,
                    reason="keep-alive" if not stagger else "bootstrap",
                )
            )
            phases.append((now + period) % period)
            continue
        target = _largest_gap_midpoint(phases, period)
        d = _signed_offset((now + period) % period, target, period)
        if abs(d) <= tolerance:
            decisions.append(
                WarmupDecision(
                    number=acct.number,
                    action="ping_now",
                    model=model,
                    reason="on-phase",
                )
            )
            phases.append((now + period) % period)
        else:
            wait = (target - ((now + period) % period)) % period
            decisions.append(
                WarmupDecision(
                    number=acct.number,
                    action="wait_until",
                    model=model,
                    at_ts=now + wait,
                    reason="staggered",
                )
            )
            phases.append(target)
    return decisions


# ---------------------------------------------------------------------------
# The runner (the one seam that spawns a process)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PingResult:
    """Outcome of one hello. Carries no environment and no paths."""

    ok: bool
    model: str = DEFAULT_MODEL
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    duration_s: float = 0.0
    error: str = ""
    #: Nothing was spawned: the account's role changed between planning and
    #: spawning (see ``AutoSwitchEngine._ping_worker``). Not a failure — it
    #: must not earn a backoff strike.
    skipped: bool = False

    def summary(self) -> str:
        if self.skipped:
            return self.error or "skipped"
        if self.ok:
            cost = f", ${self.cost_usd:.4f}" if self.cost_usd is not None else ""
            return (
                f"{self.model}: {self.input_tokens} in / "
                f"{self.output_tokens} out{cost}"
            )
        return f"{self.model}: {self.error}" if self.error else f"{self.model}: failed"


class PingRunner(Protocol):
    """The spawn seam. Fakes implement exactly this in tests."""

    def ping(
        self,
        config_dir: Path,
        model: str,
        cwd: Path,
        timeout_s: float = PING_TIMEOUT_S,
    ) -> PingResult:  # pragma: no cover - protocol
        ...


#: The measured-minimal hello, in order. ``--model`` and its alias are
#: spliced in at build time. Changing ANY of these changes the token cost:
#: dropping ``--tools ""``/``--setting-sources ""`` alone took one hello
#: from ~430 input tokens to 21,626 cache-creation tokens ($0.0008 →
#: $0.044). ``--bare`` is deliberately absent: it disables OAuth entirely.
PING_ARGS_HEAD = ("-p", "hi", "--model")
PING_ARGS_TAIL = (
    "--max-turns", "1",
    "--no-session-persistence",
    "--output-format", "json",
    "--system-prompt", "Reply OK.",
    "--tools", "",
    "--setting-sources", "",
    "--strict-mcp-config",
    "--no-chrome",
    "--max-budget-usd", "0.10",
)


def ping_argv(claude_bin: str, model: str) -> list[str]:
    """The exact command line one hello runs. Pinned by test."""
    return [claude_bin, *PING_ARGS_HEAD, model, *PING_ARGS_TAIL]


def ping_env(config_dir: Path) -> dict[str, str]:
    """Child environment: ours, minus the auth overrides, plus an EXPLICIT
    ``CLAUDE_CONFIG_DIR``.

    Both halves matter. An exported ``ANTHROPIC_API_KEY`` would make the
    hello bill an API key instead of starting the account's window (the same
    hijack ``cswap run`` scrubs), and inheriting our own
    ``CLAUDE_CONFIG_DIR`` would point every account's hello at whatever
    profile this process happens to be running under.
    """
    env = {
        k: v for k, v in os.environ.items() if k not in AUTH_OVERRIDE_ENV_VARS
    }
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def _parse_ping_json(stdout: str, model: str, duration_s: float) -> PingResult:
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return PingResult(
            ok=False, model=model, duration_s=duration_s,
            error="claude did not return JSON",
        )
    if not isinstance(payload, dict):
        return PingResult(
            ok=False, model=model, duration_s=duration_s,
            error="claude did not return a JSON object",
        )
    ok = payload.get("subtype") == "success" and not payload.get("is_error")
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}

    def _int(key: str) -> int:
        value = usage.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    cost = payload.get("total_cost_usd")
    return PingResult(
        ok=bool(ok),
        model=model,
        # Cache-creation tokens are billed input; folding them in here is
        # what makes a regression in the command line visible as a number.
        input_tokens=_int("input_tokens") + _int("cache_creation_input_tokens"),
        output_tokens=_int("output_tokens"),
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        duration_s=duration_s,
        error="" if ok else str(payload.get("subtype") or "claude reported an error"),
    )


class SubprocessPingRunner:
    """Spawns the installed ``claude`` binary, hidden and de-prioritized.

    Windows gets ``CREATE_NO_WINDOW`` (no console flash on a dev box —
    warmups run while somebody is working) plus
    ``BELOW_NORMAL_PRIORITY_CLASS``; POSIX gets ``start_new_session=True``
    so a Ctrl-C in the parent's terminal does not reach the hello. stdin is
    ``DEVNULL``: a hello must never block waiting for input.
    """

    def ping(
        self,
        config_dir: Path,
        model: str,
        cwd: Path,
        timeout_s: float = PING_TIMEOUT_S,
    ) -> PingResult:
        claude_bin = shutil.which("claude")
        if not claude_bin:
            return PingResult(
                ok=False, model=model,
                error="'claude' was not found on PATH",
            )
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
            )
        else:
            kwargs["start_new_session"] = True
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                ping_argv(claude_bin, model),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(cwd),
                env=ping_env(config_dir),
                text=True,
                **kwargs,
            )
        except OSError as e:
            return PingResult(
                ok=False, model=model, error=f"could not start claude: {e.strerror}"
            )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            # By PID, on our own handle — never a name-based kill.
            proc.kill()
            try:
                proc.communicate(timeout=10)
            except Exception:  # pragma: no cover - kill raced the exit
                pass
            return PingResult(
                ok=False, model=model, duration_s=time.monotonic() - started,
                error=f"timed out after {timeout_s:.0f}s",
            )
        duration_s = time.monotonic() - started
        rc = proc.returncode
        if rc != 0:
            tail = redact_child_output(stderr or "")
            return PingResult(
                ok=False, model=model, duration_s=duration_s,
                error=f"rc={rc}" + (f": {tail}" if tail else ""),
            )
        result = _parse_ping_json(stdout or "", model, duration_s)
        if result.ok:
            return result
        tail = redact_child_output(stderr or "")
        return PingResult(
            ok=False, model=model, duration_s=duration_s,
            error=result.error + (f": {tail}" if tail else ""),
        )


# ---------------------------------------------------------------------------
# Wiring shared by the engine, the CLI and the dashboard
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Live-hello registry (process-wide)
# ---------------------------------------------------------------------------
#
# A hello is not just a network call: :func:`config_dir_for` runs the whole
# ``cswap run`` preparation, whose consume gate POSTs a refresh OUTSIDE the
# backup lock and then persists the successor by fingerprint CAS. Anything
# that ACTIVATES that slot meanwhile reads generation N under ``lock_file``
# and installs it as the live login, and the gate lands N+1 a moment later —
# the default login is left holding a spent refresh token and finds out at
# its next expiry.
#
# The engine guards its own decision with ``_warm_inflight``, but that set
# dies with the engine. The real sequence is cross-surface: `p` on the auto
# screen, `escape` back to the dashboard (which stops the engine while the
# daemon hello thread runs on for seconds), then `enter` to switch onto that
# very slot. Hence a registry that belongs to the PROCESS, not to a screen.
#
# Scope, stated plainly: this is in-process only. A `cswap switch` in another
# terminal cannot see a hello this process is running. Closing that would
# need an on-disk lease beside the account store, which is a bigger change
# than the window justifies — a hello lasts seconds.
_HELLOS_LOCK = threading.Lock()
_HELLOS: dict[str, int] = {}


def active_hellos() -> frozenset[str]:
    """Account numbers with a warmup hello in flight right now."""
    with _HELLOS_LOCK:
        return frozenset(_HELLOS)


@contextmanager
def hello_in_flight(number: str):
    """Register ``number`` as being warmed for the duration of the block.

    Counted, not a plain set: two surfaces may legitimately overlap on one
    account (an engine tick and a `cswap warm` in the same process), and the
    first one to finish must not clear the flag for the other.
    """
    number = str(number)
    with _HELLOS_LOCK:
        _HELLOS[number] = _HELLOS.get(number, 0) + 1
    try:
        yield
    finally:
        with _HELLOS_LOCK:
            remaining = _HELLOS.get(number, 1) - 1
            if remaining > 0:
                _HELLOS[number] = remaining
            else:
                _HELLOS.pop(number, None)


def perform_hello(
    switcher: "ClaudeAccountSwitcher",
    number: str,
    model: str,
    active: str | None,
    runner: PingRunner,
    cwd: Path,
    timeout_s: float = PING_TIMEOUT_S,
) -> PingResult:
    """Resolve the account's config dir and send one hello, registered.

    The registration spans the SLOT PREPARATION as well as the child
    process: the preparation is where the token rotates, so a registry that
    only covered ``runner.ping`` would leave the dangerous half unguarded.
    Both the engine's worker thread and :func:`warm_now`'s pool go through
    here, so there is exactly one place that can forget.
    """
    with hello_in_flight(number):
        config_dir = config_dir_for(switcher, number, active)
        return runner.ping(config_dir, model, cwd, timeout_s)


def warmup_cwd(switcher: "ClaudeAccountSwitcher") -> Path:
    """``<backup_root>/warmup/``, created on demand. Always empty."""
    path = switcher.backup_dir / WARMUP_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_dir_for(
    switcher: "ClaudeAccountSwitcher", number: str, active: str | None
) -> Path:
    """Where a hello for ``number`` must point ``CLAUDE_CONFIG_DIR``.

    THIS COMPARISON IS THE SAFETY, not a downstream refusal.
    ``setup_session`` does **not** refuse the active login — the refusal
    lives in ``SessionManager.run`` (session.py:546-560) and only fires when
    ``CLAUDE_CONFIG_DIR`` is unset, and warmup never calls ``run``. So if
    ``number == active`` were ever wrong here, ``setup_session`` would
    cheerfully build a slot copy of the live login: a second token family
    rotating against the same account, drifting from the store. The active
    account is therefore pinged through the live config home, and every
    other account goes through the exact ``cswap run`` preparation
    (bootstrap + share copy) so a warmup can never invent a third way to
    materialize a slot's credentials.

    ``paths.get_claude_config_home()`` (not ``get_default_...``) is
    deliberate: it follows ``CLAUDE_CONFIG_DIR`` exactly as
    ``current_account_number()`` does, so "who is active" and "where the
    active login lives" are answered from the same place. Reading the
    default home while the active account was identified from an override
    is precisely how the two would disagree. Callers must additionally have
    passed ``switcher._refuse_session_shell()``, which rules out the one
    environment where that agreement is a lie (a ``cswap run`` shell, where
    the "active" account is the session's, not the machine's).
    """
    if active is not None and str(number) == str(active):
        return paths.get_claude_config_home()
    from claude_swap.session import SessionManager

    # share=True even though the hello runs with `--setting-sources ""` and
    # needs none of it: `_sync_sharing(share=False)` is NOT a no-op. It
    # removes every managed shared item from the manifest and unlinks the
    # manifest itself (session.py:1050-1063), plus `_sync_mcp_servers`'s own
    # removal — so a warmup would strip settings.json / skills / agents /
    # CLAUDE.md out of a slot that a LIVE `cswap run` session is using.
    # Re-copying them is idempotent and cheap; unsharing them is not.
    session_dir, _, _ = SessionManager(switcher).setup_session(
        str(number), share=True
    )
    return session_dir


def quarantined_numbers(switcher: "ClaudeAccountSwitcher") -> set[str]:
    """Quarantined slots per the auto-switch state file (best effort)."""
    from claude_swap.autoswitch import STATE_FILENAME

    try:
        raw = json.loads(
            (switcher.backup_dir / STATE_FILENAME).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return set()
    quarantine = raw.get("quarantine") if isinstance(raw, dict) else None
    return set(quarantine) if isinstance(quarantine, dict) else set()


def read_warmup_state(switcher: "ClaudeAccountSwitcher") -> dict:
    """The ``warmup`` section of ``autoswitch_state.json`` (best effort).

    Shared with the engine on purpose: the per-day model guard and the
    anti-loop cooldown are properties of the ACCOUNT, not of whichever
    surface happened to send the last hello.
    """
    from claude_swap.autoswitch import STATE_FILENAME

    try:
        raw = json.loads(
            (switcher.backup_dir / STATE_FILENAME).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    section = raw.get("warmup")
    return section if isinstance(section, dict) else {}


def warmup_record(state: dict, number: str) -> dict:
    """The mutable ``warmup.<num>`` record inside a whole state dict."""
    section = state.get("warmup")
    if not isinstance(section, dict):
        section = {}
        state["warmup"] = section
    record = section.get(number)
    if not isinstance(record, dict):
        record = {}
        section[number] = record
    return record


def stamp_ping_started(
    state: dict, number: str, model: str, label: str | None, now: float
) -> None:
    """Record a hello as STARTED (not as succeeded).

    Both stamps are deliberately written before the outcome is known. The
    per-day model guard exists to stop an API quirk (a window that never
    reports a stamp) from looping a Fable call every tick, and ``lastPingAt``
    is the anti-loop cooldown — neither would do its job if a failed or
    skipped hello reset it.
    """
    record = warmup_record(state, number)
    record["lastPingAt"] = now
    record["lastPingModel"] = model
    if label:
        pings = record.get("modelPings")
        if not isinstance(pings, dict):
            pings = {}
            record["modelPings"] = pings
        pings[label] = now


def note_ping_started(
    switcher: "ClaudeAccountSwitcher",
    number: str,
    model: str,
    label: str | None,
    now: float,
) -> None:
    """``stamp_ping_started`` straight to the state file, for the engineless
    manual path. Best effort: bookkeeping must not block a hello."""
    from claude_swap.autoswitch import STATE_FILENAME, STATE_SCHEMA_VERSION
    from claude_swap.locking import FileLock
    from claude_swap.settings import atomic_write_json

    path = switcher.backup_dir / STATE_FILENAME
    try:
        with FileLock(path.parent / ".autoswitch_state.lock"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                state = {}
            if not isinstance(state, dict):
                state = {}
            state["schemaVersion"] = STATE_SCHEMA_VERSION
            stamp_ping_started(state, number, model, label, now)
            atomic_write_json(path, state)
    except Exception as e:
        _logger.debug("warmup state write failed for %s: %r", number, e)


@dataclass(frozen=True)
class Candidates:
    """Split of the managed slots at one instant.

    ``eligible`` is the planner's E. ``stale`` are slots that LOOK cold but
    whose usage entry is older than the serve TTL: never ping on a stale
    read — refetch them and decide next pass.
    """

    eligible: list[WarmupAccount] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)


def collect_candidates(
    switcher: "ClaudeAccountSwitcher",
    entries: dict[str, "UsageEntry"],
    now: float,
    *,
    models: Sequence[str] = (),
    quarantined: Iterable[str] = (),
    warmup_state: dict | None = None,
    in_flight: Iterable[str] = (),
    require_fresh: bool = True,
    ignore_failure_backoff: bool = False,
    ignore_ping_cooldown: bool = False,
) -> Candidates:
    """Apply every eligibility predicate; returns the planner's input.

    Dropped: not switchable (removed/disabled), API-key (no quota window to
    warm, and ``setup_session`` refuses them), quarantined, token-dead,
    fetch-backed-off, at-limit (a hello would 429 or burn the last of a
    window), unreadable usage, and inside this module's own failure backoff
    or :data:`PING_COOLDOWN_S`. ``cold_models`` is filtered here by the
    once-per-day guard, so the planner stays pure.

    Two states do not DROP an account, they PARK it: a hello in flight and a
    hello inside its cooldown both enter E as virtually warm at the reset
    the hello is creating. Dropping either would shrink N and let the next
    cold account bootstrap into the slot this one is already claiming.

    ``ignore_failure_backoff`` / ``ignore_ping_cooldown`` are for the manual
    path: the user asked for this explicitly, so a half-hour strike backoff
    (or, under ``--all``, a fifteen-minute cooldown) should not silently
    answer "nothing to do".
    """
    quarantined = set(quarantined)
    in_flight = set(in_flight)
    warmup_state = warmup_state if isinstance(warmup_state, dict) else {}
    out = Candidates()
    for number in switcher.switchable_account_numbers():
        if number in quarantined:
            out.skipped[number] = "quarantined"
            continue
        if switcher.account_kind_for(number) == "api_key":
            out.skipped[number] = "api-key"
            continue
        def park(num: str, since: float) -> WarmupAccount:
            """An account whose hello is running or has just run: virtually
            warm at the reset that hello creates, so the planner spaces the
            others around it and hands it no decision of its own."""
            return WarmupAccount(
                num, WindowState(reset_ts=since + PERIOD_S, readable=True)
            )

        if number in in_flight:
            out.eligible.append(park(number, now))
            out.skipped[number] = "in-flight"
            continue
        record = warmup_state.get(number)
        record = record if isinstance(record, dict) else {}
        backoff_until = record.get("backoffUntil")
        if (
            not ignore_failure_backoff
            and isinstance(backoff_until, (int, float))
            and now < backoff_until
        ):
            out.skipped[number] = "warmup-backoff"
            continue
        last_ping_at = record.get("lastPingAt")
        if (
            not ignore_ping_cooldown
            and isinstance(last_ping_at, (int, float))
            and now - last_ping_at < PING_COOLDOWN_S
        ):
            # The refetch after a hello cannot beat the store's serve TTL
            # (see PING_COOLDOWN_S), so the row still reads cold — and
            # fresh. This is what keeps that from re-pinging every tick.
            out.eligible.append(park(number, last_ping_at))
            out.skipped[number] = "ping-cooldown"
            continue
        entry = entries.get(number)
        if entry is None:
            out.skipped[number] = "no-usage"
            continue
        if entry.token_dead():
            out.skipped[number] = "token-dead"
            continue
        if entry.in_backoff(now):
            out.skipped[number] = "fetch-backoff"
            continue
        state = window_state(entry.decision_value(), now, models)
        if not state.readable:
            out.skipped[number] = "usage-unknown"
            continue
        if state.at_limit:
            out.skipped[number] = "at-limit"
            continue
        needs_ping = state.five_hour_cold or bool(state.cold_models)
        if require_fresh and needs_ping and not entry.fresh(now):
            # Never spend a token on a stale read: the stamp may have landed
            # since. Refetch and re-evaluate next pass.
            out.stale.append(number)
            out.skipped[number] = "stale-usage"
            continue
        model_pings = record.get("modelPings")
        model_pings = model_pings if isinstance(model_pings, dict) else {}
        cold_models = tuple(
            label
            for label in state.cold_models
            if not (
                isinstance(model_pings.get(label), (int, float))
                and now - model_pings[label] < MODEL_PING_INTERVAL_S
            )
        )
        if cold_models != state.cold_models:
            state = WindowState(
                five_hour_cold=state.five_hour_cold,
                reset_ts=state.reset_ts,
                cold_models=cold_models,
                at_limit=state.at_limit,
                readable=state.readable,
            )
        out.eligible.append(WarmupAccount(number=number, state=state))
    return out


@dataclass(frozen=True)
class WarmRow:
    """One account's line in a :func:`warm_now` report."""

    number: str
    email: str
    action: str  # "pinged" | "failed" | "would-ping" | "skipped"
    model: str = DEFAULT_MODEL
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.action in ("pinged", "would-ping")


@dataclass(frozen=True)
class WarmSummary:
    rows: list[WarmRow] = field(default_factory=list)
    dry_run: bool = False

    @property
    def attempted(self) -> int:
        return sum(1 for r in self.rows if r.action != "skipped")

    @property
    def failures(self) -> int:
        return sum(1 for r in self.rows if r.action == "failed")

    def exit_code(self) -> int:
        """0 ok · 1 at least one failure · 2 nothing to do."""
        if self.failures:
            return 1
        return 0 if self.attempted else 2


def warm_now(
    switcher: "ClaudeAccountSwitcher",
    *,
    models: Sequence[str] = (),
    runner: PingRunner | None = None,
    dry_run: bool = False,
    force_all: bool = False,
    now: float | None = None,
    max_workers: int = 4,
) -> WarmSummary:
    """Hello every cold eligible account NOW, then refetch what was pinged.

    The manual path (``cswap warm``, the dashboard's ``p``): no stagger and
    no planner, but the SAME shared warmup state as the engine.
    ``force_all`` hellos every eligible account, cold or not — not the
    default; kept as a flag.

    Runs in a BOUNDED pool (``max_workers``), never one thread per account:
    this executes on a dev box somebody is working on.
    """
    # Inside a `cswap run N` shell the "active" account is the SESSION's,
    # not the machine's, so the real default login reads as non-active and
    # `config_dir_for` would hand it to `setup_session` — a slot copy of the
    # live login. Upstream guards every live-store mutation this way; a
    # warmup rotates credentials, so it is one. Raises SwitchError, which
    # the CLI already renders and exits 1 on.
    switcher._refuse_session_shell()
    now = time.time() if now is None else now
    runner = runner or SubprocessPingRunner()
    entries = switcher.usage_entries_by_account(fetch=set())
    candidates = collect_candidates(
        switcher,
        entries,
        now,
        models=models,
        quarantined=quarantined_numbers(switcher),
        warmup_state=read_warmup_state(switcher),
        require_fresh=False,
        # The user asked for this by name: a strike backoff must not answer
        # "nothing to do", and --all is the explicit override for the
        # anti-loop cooldown as well.
        ignore_failure_backoff=True,
        ignore_ping_cooldown=force_all,
    )
    active = switcher.current_account_number()
    targets: list[tuple[str, str]] = []
    labels: dict[str, str | None] = {}
    rows: list[WarmRow] = []
    for acct in candidates.eligible:
        state = acct.state
        if not force_all and not state.five_hour_cold and not state.cold_models:
            rows.append(
                WarmRow(
                    number=acct.number,
                    email=switcher.account_email(acct.number),
                    action="skipped",
                    detail="already warm",
                )
            )
            continue
        model = (
            model_alias(state.cold_models[0]) if state.cold_models else DEFAULT_MODEL
        )
        targets.append((acct.number, model))
        labels[acct.number] = state.cold_models[0] if state.cold_models else None
    if not targets:
        return WarmSummary(rows=rows, dry_run=dry_run)
    if dry_run:
        rows.extend(
            WarmRow(
                number=number,
                email=switcher.account_email(number),
                action="would-ping",
                model=model,
            )
            for number, model in targets
        )
        return WarmSummary(rows=rows, dry_run=True)

    cwd = warmup_cwd(switcher)
    # Stamped BEFORE the hellos, from the same shared state the engine reads:
    # the per-day model guard and the anti-loop cooldown must bind whether the
    # hello came from a tick or from `cswap warm`.
    for number, model in targets:
        note_ping_started(switcher, number, model, labels.get(number), now)

    def _one(item: tuple[str, str]) -> WarmRow:
        number, model = item
        email = switcher.account_email(number)
        try:
            result = perform_hello(
                switcher, number, model, active, runner, cwd, PING_TIMEOUT_S
            )
        except Exception as e:  # never let one account sink the batch
            # Type only: `setup_session` messages can embed slot and config
            # paths, which this module promises never to emit.
            _logger.debug("warmup slot preparation failed for %s: %r", number, e)
            return WarmRow(
                number=number, email=email, action="failed", model=model,
                detail=f"{safe_error(e)} preparing the account's profile",
            )
        return WarmRow(
            number=number,
            email=email,
            action="pinged" if result.ok else "failed",
            model=model,
            detail=result.summary(),
        )

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(targets)))) as pool:
        rows.extend(pool.map(_one, targets))
    pinged = {r.number for r in rows if r.action == "pinged"}
    if pinged:
        try:
            switcher.usage_entries_by_account(fetch=pinged, scheduled=False)
        except Exception as e:  # the hellos landed; the refetch is a bonus
            _logger.debug("warmup refetch failed: %r", e)
    return WarmSummary(rows=rows, dry_run=False)
