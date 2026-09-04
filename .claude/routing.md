# Model routing layer (FABLE 5.1 orchestrator / Opus 5 execution)

> Imported by CLAUDE.md via @.claude/routing.md. Read alongside the WAT
> instructions in the rest of CLAUDE.md. Adds only the model-cost dimension.
>
> **This project's main is FABLE 5.1, not Opus 5** — Jeremy's explicit choice at
> scaffold time (2026-09-04; rationale in `docs/decisions.md`). This file is the
> Fable-main variant of the house routing layer: three rules are inverted from
> the Opus-5-main default. Do not "restore" them from another project's copy.

WAT keeps the reasoning layer thin by offloading execution to deterministic
tools. This file tiers that execution by model: mechanical work on a cheap model,
judgment on an expensive one.

## Who runs on what
- You, the WAT Agent (this main session): **FABLE 5.1** (`claude-fable-5-1`).
  Orchestrate — read the workflow, sequence tools, handle failures, ask
  clarifying questions, own git and workflow edits, run the self-improvement
  loop, report results.
- Default execution: **OPUS 5** subagents (`tool-runner`, `review`). Editing
  `src/claude_swap/`, running `tools/*.py`, fixing them when they break,
  reviewing a change before commit.
- Simple mechanical work: the **SONNET 5** `chore` subagent. Running a known-good
  tool or the test suite, capturing and summarizing output, bulk repetitive
  passes — work with no code to write and no judgment call in it.
- Discovery: the **HAIKU** `scout` subagent. Read-only lookups — which module
  owns a behaviour, which test covers it, whether a tool already exists.
- Consequential verification: the **FABLE 5.1** `deep` subagent, isolated, at
  `xhigh` effort. See §Verification — with a Fable main this is a
  *fresh-context verifier*, not an escalation hop, and it stays on Fable at
  Jeremy's standing ruling.

## Routing (per step)
Treat this as a **menu, not a script.** Prescriptive step-by-step scaffolding
lowers Fable's output quality — state the goal and the constraints, then pick
your own path.

- "Which module/test owns X? Is there already a tool for Y?" → `scout` (Haiku)
- Run a known-good tool or the test suite, capture output → `chore` (Sonnet 5)
- Change package code, write or fix a tool, make the tests pass → `tool-runner` (Opus 5)
- Review a change before you commit → `review` (Opus 5)
- Independent fresh-context check of consequential work → `deep` (Fable 5.1)
- Decompose, sequence, synthesize, decide → inline, you (Fable 5.1)

### chore vs tool-runner — the one test
Does the step require writing or fixing code, or judging whether the output is
right? No → `chore`. Yes → `tool-runner`. `chore` has no Write/Edit and is told to
stop and report if its tool fails, so a broken tool comes back to you for routing
instead of being quietly repaired at the cheap tier. When in doubt on a
first-time run, use `tool-runner`; move the step to `chore` once the tool is
known good.

## Delegation discipline — delegate freely, and prefer async

**Inverted from the Opus-5-main default.** That default warns against
over-delegation because Opus 5 reaches for subagents readily. Fable is the
opposite: its parallel subagents are dependable and it under-reaches.

- Delegate freely. Dispatch independent tracks in a single message so they run
  concurrently, and keep working while they run — intervene only if one goes off
  track or is missing context.
- Prefer **long-lived** subagents that keep their context over spawn-and-block:
  no per-subtask context rebuild, and you are not bottlenecked on the slowest one.
- Brief each one precisely the first time.
- Still true: after a subagent returns, trust its summary. Do not re-open its
  files or re-run its command on the main thread.

## Verification — make it explicit and run it on a cadence

**Inverted from the Opus-5-main default.** Opus 5 self-verifies, which is why the
default file tells it *not* to add verification hops. Fable does not do this
reliably on its own.

- Establish a concrete method for checking your own work against the spec, and
  run it on a cadence — not once at the end. Here the method is concrete: the
  upstream test suite (`python -m pytest -q -n 4`, BELOW_NORMAL priority) plus a
  scratch-store manual trial, then `cswap status` as the live smoke test.
- **Fresh-context verifier subagents beat self-critique.** `review` (Opus 5) is
  the standing hop for any change before commit. `deep` is the hop for anything
  consequential and hard to detect by tests: anything that touches credential
  read/write paths, the account store layout, the switch/`run` launch logic, the
  `CLAUDE_CONFIG_DIR` copy step (restic and the cross-sessions skill depend on
  that layout), or an upstream rebase with conflicts in those areas.
- **`deep` stays on FABLE (5.1, `xhigh`) — settled, do not re-litigate.** It
  runs the same model as the main by Jeremy's **standing ruling (2026-08-17)**,
  the house default for every Fable-main project
  (`~/.claude/guides/project-scaffolding-scope.md` §Questions that ARE fair to
  ask). The value is the *fresh, isolated context*, not a capability step-up,
  and it costs 2× Opus 5 per token — so route to it for consequential checks,
  not routine ones. Known failure mode to watch: `deep` echoing the main's
  framing instead of challenging it. Brief it adversarially ("try to refute X")
  rather than repointing it.

## The live account store is gated

`~/.claude-swap-backup/` is the LIVE store: plaintext OAuth tokens for every
account on this box, plus the per-slot `sessions/` transcript trees that restic
and the cross-sessions skill read. Nothing in this project tests against it.
Subagents build and test against `tests/` fixtures and a scratch
`CLAUDE_CONFIG_DIR` + scratch backup dir. The one sanctioned touch of the live
store is the orchestrator's foreground `cswap status` / `cswap list` smoke test
after a change. Reinstalling the package (`pip install -e .`, only after a
`pyproject.toml` change) is orchestrator-owned, foreground, and preceded by the
no-live-`cswap`-process check. Upstream reconciles (fetch, rebase,
force-with-lease push of `jeremy`) are orchestrator-owned and confirmed with Jeremy.

## Ownership — never delegate these
- **Git and `workflows/` are yours**, and the guard hook ENFORCES it: a subagent
  that tries `git commit/push/reset/rebase` or a write under `workflows/` is
  blocked at the tool call (`.claude/hooks/guard_subagent.py`, wired into every
  Bash-capable agent). Subagents return diffs; you commit. Update a workflow only
  with Jeremy's confirmation.
- **The live account store, reinstalls, and upstream reconciles** (section above).
- **Secrets**: never print, log, copy, or return credential material — the
  contents of `~/.claude-swap-backup/`, `~/.claude/.credentials.json`, or any
  `.env`/`credentials.json`/`token.json`, redacted or not. In a PUBLIC repo a
  leaked token is a rotation event.
- **Deliverables**: you commit and report; subagents leave intermediates in `.tmp/`.

## Fable 5.1 operating facts (know these)
- **Thinking is always on.** `thinking: {"type": "disabled"}` is a 400; depth is
  controlled by effort alone.
- **30-day data retention is required.** Fable is not available under zero data
  retention — a ZDR workspace 400s on every request.
- **Single turns can run many minutes** at higher effort. Good for long
  autonomous runs, rough for quick interactive edits.
- **5.1 deltas vs Fable 5.** Narrates less between tool calls and writes
  shorter summaries — a one-line preface and a standalone recap are the house
  ask. Rewrites whole files where a targeted edit would do; **prefer targeted
  edits here** — every rewritten upstream file is a rebase conflict later.
  `medium` effort roughly matches Fable 5 quality at lower cost.

## Model wiring (desktop app)
Main = Fable 5.1 via `.claude/settings.json` (exact ID `claude-fable-5-1` —
house convention is exact IDs so each release is a deliberate step). Workers
pinned per agent file (`tool-runner`/`review` = `claude-opus-5`, `chore` =
`claude-sonnet-5`, `scout` = `haiku`, `deep` = `claude-fable-5-1` @ `xhigh`).
**DO NOT set `CLAUDE_CODE_SUBAGENT_MODEL`** — it overrides frontmatter and
collapses every worker onto one model. Confirm Fable 5.1 in the startup header;
if the model picker says otherwise it overrides for that session only.

## Usage tracking & cost recaps
Run `tools/report_model_usage.py` at session end and on request, and include the
cost recap when you report. Fable main-session tokens plus a Fable `deep` are the
dominant line items here — watch both. `docs/model-usage.md` is **gitignored in
this repo** (public fork; Jeremy's spend stays local). The procedure lives in
`~/.claude/guides/claude-code-model-usage-tracking.md` — READ it when you run the
tracker, do not `@`-import it.
