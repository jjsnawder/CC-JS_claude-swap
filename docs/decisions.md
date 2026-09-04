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
