@.claude/routing.md

# CC-JS_claude-swap — Agent Instructions

> **STATUS (2026-11-02): LIVE FORK on upstream v0.29.0.** This repo is Jeremy's
> **fork of [realiti4/claude-swap](https://github.com/realiti4/claude-swap)**
> (`cswap`, the Claude Code multi-account switcher, MIT) plus the house WAT +
> routing frame (Opus 5.5 main), and it IS the `cswap` running on the dev box.
> Fork features on top of upstream: consume-first ranking refinements, the
> `consume-first-reserve-jj` strategy, warmup stagger (`cswap warm`), the auto
> screen's `n`/`m`/`p`/`s` keys, and engine control (`cswap engine`, used by the
> CC-JS_Claude_Remote hub). Upstream's `priority-consume-first` is suppressed
> (`settings.SUPPRESSED_STRATEGIES`). Specs: `docs/plans/`; rulings:
> `docs/decisions.md`; the one workflow so far: `workflows/reconcile-upstream.md`.

> **This repo is PUBLIC** (GitHub requires forks of public repos to be public).
> Nothing operator-specific goes in it: no account emails, slot rosters, usage
> numbers, internal hostnames, and nothing from `~/.claude-swap-backup/`.
> Operator notes live in the private env repo
> (`CC-JS_Claude_Environment/machine-setup/claude-swap-cheatsheet.md`).

## Planning session — how a new change starts

Jeremy opens a fresh session here and says, in effect, **"I want cswap to…"**.
That sentence is the whole kickoff. When it arrives, before proposing anything:

1. **Read in full** — this file, `.claude/routing.md`, `docs/decisions.md`, and:
   - `~/.claude/guides/local-windows-hosting.md` §9 (this project's inventory
     row) and `~/.claude/guides/devbox-compute-budget.md` (the test suite runs on
     the dev box: BELOW_NORMAL priority, bounded workers, kill by PID only).
   - `~/.claude/guides/claude-code-permission-mode.md` §The acknowledgment flag
     and cswap slots, and the cswap section of the global `~/.claude/CLAUDE.md` —
     how `cswap run` slot dirs are copied from `~/.claude` at every launch. Two
     house systems depend on the resulting layout: the nightly restic backup
     (`CC-JS_Claude_Environment/scripts/backup-devbox.ps1` discovers slot dirs)
     and the `cross-sessions` skill (`~/.claude/skills/cross-sessions/`).
   - Upstream's own `README.md` for the CLI surface, and the module(s) that own
     the behaviour in question (`grep -rn` under `src/claude_swap/`) with their
     `tests/test_<module>.py` — the tests ARE the contract.
   Everything those settle is settled — don't re-ask it.
2. **Take what's already decided as given** (below, and `docs/decisions.md`):
   true GitHub fork, public, personal account; `main` mirrors upstream, `jeremy`
   is the working branch based on the latest release tag; editable install into
   system Python 3.13 (no venv — deliberate); Opus 5.5 main and workers, with
   `deep` on Fable 5.2 (`.claude/routing.md`).
3. **Plan with Jeremy, then write `docs/plans/<date>-<slug>.md` before any
   code.** The conversation must produce:
   - **The change itself**, and whether it is (a) generic → structured for an
     upstream PR, or (b) Jeremy-specific → fork-only, behind a config flag or in
     a separable module so it rebases cleanly. This decides shape, not whether.
   - **Blast radius.** Does it touch credential paths, the store layout, or the
     slot-copy step? If yes, restic + cross-sessions are in scope and `deep`
     verifies it.
   - **Tests.** Which existing tests cover it; what new test proves it.
   - **Rebase cost.** Every fork change is replayed at each upstream release
     (`workflows/reconcile-upstream.md`): keep it separable and its diff small.
4. Every non-obvious call → `docs/decisions.md`.

## What this project is

A fork of `claude-swap` that **is the `cswap` install on Jeremy's dev box**: the
package is `pip install -e` from this checkout into system Python 3.13, so an
edit to any `.py` under `src/claude_swap/` is live on the next `cswap`
invocation. No build step. Only a `pyproject.toml` change (entry points,
dependencies) needs `pip install -e .` re-run — and never while a `cswap`
process is live (pip cannot replace an in-use `cswap.exe`; check by command
line, kill by PID only).

## Branch and remote model
| Name | Role | Rule |
|---|---|---|
| `upstream` remote | `realiti4/claude-swap` | Read-only source of truth. Never push to it. |
| `origin` remote | `jjsnawder/CC-JS_claude-swap` | Our fork. Default branch `jeremy`. |
| `main` | Pristine mirror of `upstream/main` | **Never commit here.** Fast-forward only. |
| `jeremy` | The working branch — every local change lives here | Based on the latest upstream **release tag** (v0.26.0 at fork time, 2026-09-04; v0.29.0 since 2026-11-02), not `upstream/main`, which carries pre-release betas. |

## Reconciling with upstream (when a new release lands)
Follow **`workflows/reconcile-upstream.md`**: work in a worktree, try a rebase
onto the release tag, and switch to a final-state port when upstream restructured
code the fork builds on (the v0.29.0 case). Verify with the suite, a scratch-store
trial, `review` and `deep`, and check the Remote hub contract. Then cut over with
Jeremy's OK: he quits the TUI, you reset `jeremy`, `pip install -e .`,
`cswap --version`/`status`, he relaunches. `git push --force-with-lease origin
jeremy` afterwards is the ONLY sanctioned history rewrite. Never `cswap upgrade`.

A generic change goes upstream as a PR from a topic branch cut from the release
tag with only the code commits cherry-picked (never the WAT/`.claude/` frame).
Once merged, that part of our diff disappears — the smaller the fork's delta,
the cheaper each reconcile. Reconciles happen ONLY in a session in this
directory, never from another project.

## Never do
- **Never `pip install claude-swap` or `pip install --upgrade claude-swap`.** That
  replaces the editable install with the PyPI wheel and silently drops every local
  change. Updates arrive through git only.
- **Never test against the live account store.** `~/.claude-swap-backup/` holds
  plaintext OAuth tokens. Unit tests use the fixtures under `tests/`; manual
  trials go through `tools/trial/` (scratch home; real store, keyring, network
  and subprocesses blocked). The only live touch is the orchestrator's read-only
  smoke test (`cswap status`, `cswap list`, `cswap engine status --json`).
- Never print, log, copy, or commit credential material, even redacted.
- Never edit upstream's `README.md` or `.github/` — pure rebase friction. Fork
  documentation lives here and in `docs/`.

## The WAT framework

You're working inside **WAT** (Workflows, Agents, Tools): probabilistic AI
handles the reasoning, deterministic code handles the execution.

- **Layer 1 — Workflows**: markdown SOPs in `workflows/` (objective, inputs,
  tools, outputs, edge cases). So far: `reconcile-upstream.md` (2026-11-02).
- **Layer 2 — Agent**: you. Read the relevant workflow, run tools in the right
  sequence, handle failures gracefully, ask clarifying questions.
- **Layer 3 — Tools**: `tools/` holds house helpers: the usage tracker,
  `run_tests_low.py` (pytest at BELOW_NORMAL), and the `trial/` scratch-store
  kit. The **product code is
  upstream's `src/claude_swap/`** — edit it directly; it is not a WAT tool.

How to operate: look for existing behaviour and tests first; when something
fails, fix the system and not just the run, then record what you learned in the
workflow; keep workflows current — don't create or overwrite one without asking
unless told to.

## Development
- Layout is upstream's: `src/claude_swap/` (package, `cli.py` is the entry),
  `src/claude_swap/tui/` (Textual dashboard), `tests/` (pytest + pytest-asyncio +
  pytest-xdist). `uv.lock` is upstream's lockfile; on this box we use system
  Python 3.13 + pip, not uv (no venv — the editable install IS the point).
- Test deps: `pip install pytest pytest-asyncio pytest-xdist`. Run the suite with
  `python tools/run_tests_low.py -q -n 4 -p no:cacheprovider`. Verified green
  2026-11-02 on v0.29.0 + fork (3298 passed, 121 skipped, ~2–2.5 min at `-n 4`).
- Commit discipline: one concern per commit, upstream's style (`fix(scope): …`,
  `feat(scope): …`). Prefer targeted edits over file rewrites — every rewritten
  upstream file is a rebase conflict later.
- Commit-message trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
  (the main session's model; it was Fable 5.1 until 2026-09-27).
- **Push is pre-authorized** (Jeremy's standing instruction, all projects).
  `git push --force-with-lease origin jeremy` after a rebase is the one sanctioned
  history rewrite here; anything else that rewrites history still needs his OK.

## Plans and decisions

**Plan on disk before code**: `docs/plans/<date>-<slug>.md` for anything
spanning a session or more than a handful of files; work off the file and keep
it current. **Log non-obvious choices** in `docs/decisions.md` (append-only,
fixed shape: Chose / Rejected / Why it matters, dated). Corrections are new
entries, not edits. Session handoffs (the `handoff` skill) land in
`session-handoffs/`.

## File structure
- **Product code** → `src/claude_swap/` (upstream's), tests → `tests/`.
- **House frame** → `.claude/` (agents, routing, guard hook), `workflows/`,
  `tools/`, `docs/`, `session-handoffs/` — committed; travels with `jeremy`, never
  in an upstream PR.
- **Scratch** → `.tmp/`, gitignored. **Cost recaps** → `docs/model-usage.md`,
  gitignored (public repo).
- **Secrets**: none belong to this project. The live store it manages is out of
  scope for every file here.
