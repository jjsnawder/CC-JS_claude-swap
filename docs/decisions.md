# Decisions

Append-only, newest last. One entry per non-obvious choice. When an entry hardens
into a house pattern, promote it to a guide in `~/.claude/guides/` and leave a
pointer here, so this file stays live reasoning and not a second copy of settled
policy.

<!-- Copy this shape:

## YYYY-MM-DD - short title
**Chose:** what was done.
**Rejected:** the alternative - and why it lost.
**Why it matters:** what breaks if someone reverses this without knowing.

-->

## 2026-09-04 - A true GitHub fork, public, on the personal account
**Chose:** `gh repo fork realiti4/claude-swap` renamed to `jjsnawder/CC-JS_claude-swap`
(the `JS` marker = personal account per the house naming rule). It is PUBLIC:
GitHub does not allow a private fork of a public repository.
**Rejected:** a private copy pushed to a fresh repo (loses the fork link, the
Sync-fork button, and one-click upstream PRs; the token-dashboard precedent did
this, but that project never intended to contribute upstream). Editing
site-packages in place (a future `pip install --upgrade` would silently erase it).
**Why it matters:** public means nothing operator-specific may ever be committed
here — no emails, slot rosters, usage numbers, hostnames, or anything from
`~/.claude-swap-backup/`. Those live in the private env repo's cheatsheet.

## 2026-09-04 - Branch model: main mirrors upstream, jeremy is the work branch
**Chose:** `main` = fast-forward-only mirror of `upstream/main`, never committed
to. `jeremy` = every local change, based on the latest upstream RELEASE TAG
(v0.26.0), set as the GitHub default branch. Reconcile = fetch, ff `main`,
rebase `jeremy` onto the new tag, `--force-with-lease` push.
**Rejected:** basing `jeremy` on `upstream/main` (it was already at a 0.27.0b1
beta on fork day; releases are the stable line). Merging upstream into `jeremy`
instead of rebasing (keeps history but the fork's delta becomes unreadable and
cherry-picking for upstream PRs gets harder).
**Why it matters:** the rebase + force-with-lease on `jeremy` is the one history
rewrite this project sanctions; upstream PRs are cut from a topic branch off the
tag with only code commits cherry-picked, so the WAT frame never leaks upstream.

## 2026-09-04 - Editable install into system Python 3.13, no venv
**Chose:** `pip uninstall claude-swap` (PyPI 0.25.0) then `pip install -e .` from
this checkout into the system interpreter where `cswap.exe` already lived. Deviates
from the house "one project = one venv" default on purpose.
**Rejected:** a project venv (the whole point is that the box-wide `cswap` on
PATH runs the fork; a venv would need a second launcher on PATH and two installs
to keep straight). uv (upstream's tool; we already use pip for everything else
on the box and the `uv.lock` is upstream's concern).
**Why it matters:** `pip install claude-swap` or `--upgrade` from any session now
silently replaces the fork — the global CLAUDE.md, the cheatsheet, REBUILD.md,
and the inventory row all carry the prohibition. Reinstalls happen only with no
live `cswap` process (pip cannot replace an in-use launcher).

## 2026-09-04 - Orchestrator: Fable 5.1 main, Opus 5 workers, WAT frame
**Chose:** WAT scaffold (`frameworks/wat-fable/scaffold_wat_project.py`) with the
Fable-main routing variant: `claude-fable-5-1` main, Opus 5 `tool-runner`/`review`,
Sonnet 5 `chore`, Haiku `scout`, `deep` = `claude-fable-5-1` @ `xhigh` as the
fresh-context verifier (standing ruling 2026-08-17). Jeremy's explicit pick.
**Rejected:** Opus 5 main (the WAT default) — Jeremy asked for Fable 5.1 +
Opus 5 subagents by name. Installing the agent roster at user scope
(`--user-agents`) — project scope keeps the fork self-contained.
**Why it matters:** `routing.md` here is the Fable-main variant and must not be
"restored" from an Opus-main project. In this codebase the product code is
upstream's `src/claude_swap/`, not WAT `tools/`; `tools/` holds only house
helpers, and `docs/model-usage.md` is gitignored because the repo is public.

## 2026-09-04 - Where cswap work happens
**Chose:** every code change, upstream reconcile, and reinstall happens in a
session opened in THIS directory. The env repo (`CC-JS_Claude_Environment`) only
records the fact of the fork (cheatsheet, REBUILD, inventory row, memory).
**Rejected:** driving changes from the env repo (its transcripts, plans, and
handoffs would then hold this project's history — "handoff follows the session").
**Why it matters:** the guides all point here; a reconcile run from elsewhere
leaves no plan, decision, or handoff where the next session will look.

## 2026-09-04 - Fable bar and reset-aware ranking are settings, not new defaults
**Chose:** turn on `autoswitch.model = Fable` and `autoswitch.strategy =
consume-first` in Jeremy's settings.json (`cswap config set`), and persist the
95% threshold the same way. No change to the package defaults.
**Rejected:** changing upstream's default so scoped per-model windows always
bind (would flip `test_without_model_setting_the_same_usage_holds` and three
oauth tests, and put a permanent behaviour delta in the fork for something a
one-line config already does).
**Why it matters:** anyone reading the engine and seeing "only 5h/7d by
default" is right — the operator's behaviour comes from settings.json in the
backup root, which is not in this repo and is re-read only when the TUI or
`cswap auto` is relaunched.

## 2026-09-04 - Carry open upstream PR #313 by cherry-pick
**Chose:** cherry-pick the commits of realiti4/claude-swap PR #313 (consume-first
ranking: trigger-scoped reset key, 5h tiebreak, most-used-first) onto `jeremy`
with upstream authorship intact, before building on the ranking key.
**Rejected:** waiting for the merge (consume-first would run with the #305
escape bug meanwhile); re-implementing the fix ourselves (a guaranteed rebase
conflict against the eventual upstream version).
**Why it matters:** at the next reconcile these commits should vanish as
already-applied. If upstream merged a *different* version, resolve toward
upstream and re-apply the fork's 5h guard on top — never keep both.

## 2026-09-04 - One pure ranking-key helper shared by engine and TUI
**Chose:** factor the consume-first sort key out of `_rank_candidates` into a
module-level pure function the TUI "Next best" panel imports, so the display
and the decision cannot disagree (the same pattern `binding_pct` already
follows for the metric).
**Rejected:** re-deriving the order inside `tui/autoview.py` (today's state,
which is exactly why the panel showed a headroom order under a reset strategy).
**Why it matters:** any future strategy tweak lands in one place; a TUI test
that pins the panel to the helper catches drift.

## 2026-09-04 - 5h-hot demotion under consume-first reuses hysteresisPct
**Chose:** under consume-first (proactive triggers only), a candidate whose 5h
window is within `hysteresisPct` (default 10) of the threshold ranks behind
every cool candidate; otherwise soonest weekly reset wins as upstream defines.
No new setting.
**Rejected:** a dedicated margin knob (no evidence yet it needs to differ from
the `best`-strategy hysteresis; add one only when Jeremy asks); excluding hot
accounts outright (an escape must still be able to land on them).
**Why it matters:** this is the one fork-specific behaviour on the ranking
path. It is shaped as an upstream PR (`feat/consume-first-5h-guard`, cites
issue #303) so it can leave the fork.

## 2026-09-04 - TUI strategy hotkey is session-only, like the threshold key
**Chose:** `s` on the auto screen flips `best`/`consume-first` for the session
(engine restart, re-ranked Next best, `(session)` tag, reverted on unmount).
Persistence stays with `cswap config set autoswitch.strategy`.
**Rejected:** writing settings.json from the hotkey (upstream deliberately keeps
`t` session-only; mixing the two models in one screen invites "which one is
live" confusion).
**Why it matters:** the TUI never becomes a second writer of settings.json.

## 2026-09-04 - Auto-screen `n` (Switch now) goes through the engine as a `manual` trigger
**Chose:** a one-shot `request_switch()` on `AutoSwitchEngine`; the next tick
runs with trigger `manual`, which ranks exactly like the strategy's proactive
path but ignores cooldown, hysteresis, the strictly-sooner reset gate and the
no-return bar, while keeping the landing-health gate. Dry-run previews, live
switches.
**Rejected:** calling `switcher.switch_to` from the TUI like the dashboard does
(bypasses freshen/quarantine and leaves the engine's anti-flap state blind to
the move); adding `consume-first` to `switcher.switch(strategy=…)` (duplicates
the ranking outside the engine — the panel/engine drift the shared key just
removed).
**Why it matters:** one decision path, one log, one state file. A manual move
is recorded like any other so the engine cannot immediately undo it.

## 2026-09-06 - Warmup pings are real Claude Code `-p` sessions, not raw API calls
**Chose:** start an account's 5-hour window by spawning the installed `claude`
binary headless (`-p`, one turn, no tools, no settings, no session persistence,
stub system prompt) with `CLAUDE_CONFIG_DIR` pointed at the slot dir — the same
preparation `cswap run` uses (`SessionManager.setup_session`).
**Rejected:** calling the messages API directly with the account's OAuth token
(cheaper to implement, no child process). Anthropic has cut off accounts for
third-party use of Claude Code OAuth tokens; cswap already lives on the edge by
reading the usage endpoint, and a message call is a different category of risk
for a fork whose whole value is four working accounts.
**Why it matters:** the runner's command line is the contract (measured at ~430
input / <100 output tokens per hello). Anyone "optimizing" it into an HTTP call
reopens the account-risk question.

## 2026-09-06 - Warmup policy is staggered phases, not keep-alive
**Chose:** a cold account (5h stamp absent or past) is pinged when its new reset
would land at the midpoint of the largest gap between the other warm accounts'
reset phases (tolerance half a spacing, spacing = 5h / eligible accounts);
otherwise it waits for that moment. `autoswitch.warmupStagger=false` degrades
to plain keep-alive. Jeremy's pick, 2026-09-06.
**Rejected:** keep-alive on expiry (simplest; leaves the four resets wherever
history bunched them). Fixed clock times (today's Task Scheduler pattern;
accounts sit cold between times and phases drift off the clock).
**Why it matters:** the worst-case cold wait is 3 h 45 for four accounts. A cold
account is still fully usable, so the wait costs reset-nearness, never quota.
The manual `p` / `cswap warm` deliberately ignores the stagger.

## 2026-09-06 - Model-window pings ride the existing `autoswitch.model` setting
**Chose:** when a label in `autoswitch.model` (Fable today) has no reset stamp on
an account, the next hello for that account uses that model (alias = lowercased
label; `--model fable` verified), at most once per account per label per day.
**Rejected:** a separate `warmupModels` knob (a second list to keep in sync with
the windows the engine already ranks on). Haiku-only (leaves the Fable stamp
blank until real use — the exact gap Jeremy asked to close).
**Why it matters:** a Fable hello measured 494 input / 15 output tokens — noise
against a weekly window — but it is still a Fable call; the per-day guard is
what keeps an API quirk from looping it.

## 2026-09-06 - The active login is pinged through the default config dir
**Chose:** for the account that is the current global login, the hello runs with
`CLAUDE_CONFIG_DIR` = the live Claude config home (today's Task Scheduler job's
path); every other account goes through its slot dir.
**Rejected:** a slot dir for the active account — `setup_session` refuses it,
correctly: a slot copy of the live login would rotate a second token family
and drift from the store.
**Why it matters:** the two paths look alike in the code and must stay distinct;
`deep` is briefed to attack exactly this boundary.
