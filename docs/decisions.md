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
