# Handoff — First fork change: Fable bar in the threshold, consume-first ranking, TUI Next best + `s` hotkey
Date: 2026-09-04 13:39  ·  Project: CC-JS_claude-swap  ·  Plan: `docs/plans/2026-09-04-autoswitch-fable-consume-first.md`
Previous handoff: none — first

## Firm decisions this session
- Both asks are upstream SETTINGS, not new defaults — `autoswitch.model=Fable`, `autoswitch.strategy=consume-first`, `autoswitch.threshold=95` written to the live settings.json via `cswap config set`. Rejected: flipping package defaults (permanent fork delta for a one-line config).
- `model = Fable`, not `all` (Jeremy's pick; startup warns if the display name stops matching).
- Strategy = upstream consume-first PLUS a fork-first 5h-hot demotion: a candidate whose 5h pct >= threshold − hysteresisPct ranks behind every cool candidate (proactive/consume-first triggers only; escapes rank by headroom). Margin REUSES `hysteresisPct` — no new knob unless Jeremy asks.
- Toggle = persisted config key AND a TUI `s` hotkey that cycles strategy for the session only (mirrors `t`; never writes settings.json). Keep `s` despite the dashboard's `s` = switch (different screen).
- Cherry-pick open upstream PR #313 (consume-first ranking fixes) onto `jeremy` with upstream authorship; it should drop out at the next reconcile as already-applied. If upstream merged a different version, resolve toward upstream and re-apply the 5h guard.
- One pure module-level `consume_first_key()` in `autoswitch.py` shared by engine and TUI so the panel cannot disagree with the engine.
- Next best rows render IDENTICALLY under both strategies (white label, severity-coloured pct). Jeremy rejected greying skipped rows twice (whole row, then label only). A row the proactive trigger would skip gets a trailing muted `skip` tag instead.
- No `deep` hop needed: nothing touched credential paths, store layout, or the slot-copy step. `review` (Opus 5) before every code commit.
- All five decisions are logged in `docs/decisions.md` (2026-09-04 entries).

## Where it started
Jeremy's TUI auto-screen screenshot: active account 5h 0% / 7d 25% / Fable 37% but the log said "25% used"; Next best ranked by % used while he wanted the soonest-weekly-reset account first, with a toggle between the two methods. Constraints that emerged: he was live-monitoring and near his threshold (relaunch ASAP), public fork (nothing operator-specific in git), rebase-friendly targeted edits.

## What shipped
All on branch `jeremy`, pushed to `origin`:
- 9ee2262 — plan + decisions (`docs/plans/2026-09-04-autoswitch-fable-consume-first.md`, `docs/decisions.md`)
- 0a000f3 — cherry-pick of upstream PR #313 (`src/claude_swap/autoswitch.py`, `tests/test_autoswitch.py`)
- ceeabe2 — item E: `consume_first_key`, `_five_hour_pct`, 5h-hot tier; `hysteresisPct` help/docstring updated (`src/claude_swap/autoswitch.py`, `src/claude_swap/settings.py`, `tests/test_autoswitch.py`)
- 163ab4f — items C+D: Next best follows strategy (health tier, reset countdown, `5h hot`, skip logic incl. reset-unknown idle line), `s` hotkey cycles `SETTING_SPECS` choices, summary shows `· model <x|account-wide> · <strategy>` with `(session)` tags (`src/claude_swap/tui/autoview.py`, `tests/test_tui.py`; `make_entry` gained `reset5_in`/`reset7_in` kwargs)
- 279ec7f — plan status log marked built out
- 4ab25c2 then b193fb4 — colour alignment: no greying at all, `skip` tag (`src/claude_swap/tui/autoview.py`, `tests/test_tui.py`; helpers `_skipped_rows`, `_label_styles`)
- Live settings (not in git): `~/.claude-swap-backup/settings.json` autoswitch section now model Fable / strategy consume-first / threshold 95.
- `docs/model-usage.md` regenerated (gitignored). Session cost recap at that point: Fable main $5.92, Opus workers $11.04.

## Dead-ends (do not retry)
- Greying skipped Next best rows (whole row, then label only) — Jeremy wants rows visually identical to `best`. Use the trailing `skip` tag only.
- `cswap config` / any PowerShell one-liner with `$_` inside a double-quoted Bash string — Bash eats `$_`; single-quote the PowerShell command.

## Key files for next session
- Plan: `docs/plans/2026-09-04-autoswitch-fable-consume-first.md` — read this FIRST (not duplicated here)
- `docs/decisions.md` — the 2026-09-04 entries are the rationale for every shape choice above
- `src/claude_swap/autoswitch.py` — `consume_first_key` (~:596) and the `_rank_candidates` consume-first branch (~:2031); the fork's only engine delta
- `src/claude_swap/tui/autoview.py` — `_candidates_text`, `action_toggle_strategy`, `_update_summary`
- `tests/test_tui.py::TestAutoScreen` — `_consume_first_app`, `_skipped_rows`, `_label_styles` helpers; Textual resolves appended hex styles into `Style` objects with `.foreground.hex` (see helpers)
- `~/.claude/guides/claude-code-model-usage-tracking.md` — read before running `tools/report_model_usage.py`
- Memory touched: none

## Running state
- Background processes: none (all subagents completed; no `run_in_background` shells)
- Dev servers / ports: none
- Worktrees / branches: `jeremy` (working, pushed, clean tree at b193fb4); local branch `pr-313` = fetched head of upstream PR #313 (can be deleted); `main` untouched at upstream. Jeremy relaunches the live TUI himself (`cswap` → auto screen); never kill it by name — identify via `Get-CimInstance Win32_Process` command line, PID only.

## Verification — how to confirm things still work
- `powershell -NoProfile -Command '$p = Start-Process python -ArgumentList "-m","pytest","-q","-n","4","-p","no:cacheprovider" -PassThru -NoNewWindow -RedirectStandardOutput .tmp/pytest.out; $p.PriorityClass="BelowNormal"; $p.WaitForExit(); Get-Content .tmp/pytest.out | Select-Object -Last 4'` — expect 2114 passed, 77 skipped (~75 s; last full run 2113 before the final +1 TUI test, TUI file alone 97 passed)
- `python -c "import claude_swap.autoswitch as a, claude_swap.tui.autoview; print(a.consume_first_key)"` — imports ok
- `cswap config list` — threshold 95, strategy consume-first, model Fable
- `cswap status` — live smoke (redact emails if pasting)
- In the TUI auto screen: summary reads `threshold 95% · poll every 60s · model Fable · consume-first`; poll log `N% used` = highest bar and `others:` rows carry a Fable column; Next best rows white with coloured pct, `· resets …`, `5h hot` / `skip` tags muted; `s` flips strategy with `(session)`.

## Deferred + open questions
- Deferred: upstream PRs not opened — `feat/tui-next-best-follows-strategy` (C+D, depends on #313 merging first; cite #305/#313) and `feat/consume-first-5h-guard` (E; cite #303). Cut from `v0.26.0` with only code commits cherry-picked, never `.claude/`, `docs/`, `workflows/`, `tools/`. Jeremy to say when.
- Deferred: `workflows/reconcile-upstream.md` and `workflows/verify-change.md` still unwritten (CLAUDE.md says they follow the first change; needs Jeremy's confirmation to create).
- Deferred: `cswap list` computes headroom without the `models` filter (`switcher.py:215`) — display-only, note for upstream later.
- Deferred: consume-first ranks on the 7d reset, not the Fable window's; coincide on Jeremy's accounts today.
- Open: whether the 5h-hot margin should ever become its own setting instead of `hysteresisPct` (plan assumption; Jeremy accepted "maybe 10 points").
- Open: pre-existing flake seen once under `-n 4`: `tests/test_tui.py::TestWatchScreen::test_late_normal_can_advance_usage_after_store_repaint` (thread race; passed on rerun). Not ours.

## Pick up here
Ask Jeremy whether the relaunched TUI looks right after b193fb4 (white rows, `skip` tags) and whether to cut the two upstream PRs now or wait for #313 to merge.
