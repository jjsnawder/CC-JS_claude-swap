# Auto-switch: watch the Fable bar, prefer the soonest weekly reset

**Status:** DONE 2026-09-04 — A applied to settings, B–E committed on `jeremy`
(0a000f3 #313 cherry-pick, ceeabe2 item E, 163ab4f items C+D). Upstream PRs not yet opened.
**Origin:** Jeremy's live-monitor screenshot (TUI auto screen, LIVE, threshold
95% session-only). Active account showed 5h 0% / 7d 25% / Fable 37%, but the
log read `25% used (switch at 95%)`, and "Next best" ranked by % used while the
account Jeremy wanted next was the one whose weekly window resets soonest.

## Findings (verified in `src/claude_swap/` at v0.26.0)

Both asks already exist upstream as settings. Jeremy's `settings.json` has no
`autoswitch` section, so the engine runs every default.

| Ask | Upstream mechanism | Where |
|---|---|---|
| Threshold = highest of 5h / 7d / Fable | `autoswitch.model` (`Fable`, `Fable,Opus`, or `all`). `oauth.relevant_windows` appends scoped per-model windows only when set; `account_headroom` = `100 - max(pcts)`. The poll log's `N% used` and `others: … · Fable N%` columns, and the TUI "Next best" metric, all read the same filtered set. | `oauth.py:505-561`, `autoswitch.py:516-528, 975-1000`, `PollEvent.human` `autoswitch.py:330-359`, `tui/autoview.py:294-331` |
| Prefer the soonest weekly reset | `autoswitch.strategy = consume-first`: candidates ranked by soonest 7d `resets_at` (unknown last), most headroom on ties; below the threshold it also *moves* to an account that resets strictly sooner than the active one (cooldown + healthy-landing gate still apply). | `_rank_candidates` `autoswitch.py:1755-1951`, trigger `autoswitch.py:981-998` |
| Persist the 95% threshold | `autoswitch.threshold` (TUI `t` is session-only by design). | `settings.py:100-143`, `autoview.py:182-190` |

The engine reads settings once at construction; the TUI loads them on mount.
**Any settings change needs the running TUI exited and relaunched** (never kill
it: Jeremy exits with `esc` then `q`; the process is identified by command line
and only ever stopped by PID if that fails).

### Gaps (the actual code work)

1. **TUI "Next best" ignores the strategy.** `AutoScreen._candidates_text`
   always sorts by `binding_pct` and shows no reset time, so under
   consume-first the panel disagrees with the engine's pick.
2. **No in-TUI strategy toggle.** Only `l` (live/dry-run) and `t` (threshold).
3. **consume-first has known ranking bugs upstream** — issue #305 (escape
   triggers rank by reset, not headroom) fixed by open, mergeable **PR #313**
   (also adds a 5h-reset tiebreak and most-used-first on full ties). Related
   open: #288 (at-limit landing), #303 (per-window thresholds), PRs #178, #250,
   #262, #312, #319 (other strategy variants). We touch none of those.
4. **Jeremy's refinement:** rank by soonest weekly reset, but demote an account
   whose 5h window is already "hot" (within the hysteresis margin of the
   threshold). Upstream has no such guard.

Semantics check against the screenshot: under consume-first the active account
(#1, resets 21h) stays put — it already resets soonest. "Next best" would read
#3 (1d 0h), #2 (1d 16h), #4 (4d 6h); with the 5h guard (E), #3 at 5h 93% ≥
95−10 drops below #2 and #4.

## Decisions taken with Jeremy (2026-09-04)

- `autoswitch.model = Fable` (not `all`; a warning fires at startup if the
  display name ever stops matching).
- Strategy: upstream consume-first **plus** the 5h-hot demotion (E).
- Toggle: persisted config key **and** a TUI hotkey `s` that flips strategy for
  the session, mirroring `t`.
- Cherry-pick PR #313 onto `jeremy` now; it drops out of our delta when upstream
  merges it.

Logged in `docs/decisions.md` (2026-09-04 entries).

## Work items

### A. Configuration — no code (orchestrator, foreground, live store touch)
Requires the TUI exited first (Jeremy). Then:
```
cswap config set autoswitch.model Fable
cswap config set autoswitch.strategy consume-first
cswap config set autoswitch.threshold 95
cswap config list
```
Jeremy relaunches the TUI → auto screen. **Verify:** header/log show a `Fable`
column in `others:`, the active `N% used` equals the highest of its three bars,
the `(session)` tag on the threshold is gone, `cswap status` still clean.
A can be done before any code lands and is independently useful.

### B. Cherry-pick upstream PR #313 (orchestrator: git; `chore`: test run)
```
git fetch upstream pull/313/head:pr-313
git log --oneline v0.26.0..pr-313         # expect the PR's commits only
git cherry-pick <sha…>                    # onto jeremy, keep upstream authorship
```
Touches `src/claude_swap/autoswitch.py` (`_rank_candidates` key, new
`_five_hour_reset_ts`) and `tests/test_autoswitch.py` (3 tests in
`TestConsumeFirstStrategy`). Resolve conflicts only if the frame touched the
same lines (it did not). **Verify:** full suite green.
Reconcile note: when upstream merges #313, the rebase should drop these commits
as already-applied; if upstream merged a different version, resolve toward
upstream and re-apply E on top.

### C. Extract one pure ranking-key helper; TUI "Next best" follows it (`tool-runner`)
Shape: **generic → upstream PR candidate** (`feat/tui-next-best-follows-strategy`).
- In `autoswitch.py`, factor the consume-first sort key (post-#313 chain plus
  E's tier) into a module-level pure function, e.g.
  `consume_first_key(usage, headroom, *, threshold, hysteresis_pct, now)`,
  used by `_rank_candidates` and importable by the TUI — so the display can
  never disagree with the engine (same pattern as `binding_pct`, which both
  already share).
- `AutoScreen._candidates_text`: when `self._settings.strategy == "consume-first"`
  sort by that key and render `resets 1d 16h` via `tui/data.reset_text`; mute a
  row the proactive trigger would not take (reset not sooner than the active
  account's) so a "why didn't it move" question answers itself. `best` keeps
  today's rendering.
- `_update_summary`: append `· model Fable · consume-first` so the effective
  decision inputs are on screen.
- Tests (`tests/test_tui.py::TestAutoScreen`): add
  `test_candidates_ranked_by_weekly_reset_under_consume_first`,
  `test_candidates_show_reset_countdown_under_consume_first`,
  `test_summary_shows_strategy_and_model`; keep
  `test_candidates_ranked_by_headroom` (best) and
  `test_candidates_ranking_honors_configured_model` green.

### D. TUI hotkey `s` — session-only strategy toggle (`tool-runner`)
Shape: generic → same upstream PR as C.
- `AutoScreen.BINDINGS`: `Binding("s", "toggle_strategy", "Strategy")`.
- Mirrors `_set_threshold`: `replace(self._settings, strategy=…)`, restart the
  engine (`_restart_engine`, same as the live/dry-run toggle — the engine reads
  settings at construction), re-render Next best and the summary with a
  `(session)` tag when it differs from the configured value; revert on unmount.
  `cswap config set autoswitch.strategy` remains the persistent path.
- Tests: `test_strategy_toggle_is_session_only`,
  `test_strategy_toggle_restarts_engine_and_reranks`.

### E. 5h-hot demotion under consume-first (`tool-runner`)
Shape: fork-first, upstream-able as `feat/consume-first-5h-guard` (issue #303's
5h/7d asymmetry is the same observation from another angle).
- In the consume-first key (proactive/consume-first triggers only — escapes
  rank by headroom per #313): `hot = five_hour_pct >= threshold - hysteresis_pct`;
  key = `(1 if hot else 0, seven_day_reset_ts, five_hour_reset_ts, headroom)`
  with #313's ordering inside each tier. A hot account is still a candidate, it
  just ranks behind every cool one.
- Margin reuses `autoswitch.hysteresisPct` (default 10) — **assumption:** no new
  setting; Jeremy said "maybe 10 points". Add a knob only if he asks.
- Tests (`TestConsumeFirstStrategy`): `test_hot_five_hour_candidate_ranks_after_cool_ones`,
  `test_hot_guard_ignored_on_at_limit_and_failover`,
  `test_hot_guard_uses_hysteresis_margin`; `PollEvent`/JSON unchanged (additive
  contract, nothing to add).

Order: A (any time) → B → E → C → D. C depends on B+E for the key chain; D on C.

## Blast radius

None of A–E touches credential read/write paths, the store layout, the
switch/`run` launch logic, or the `CLAUDE_CONFIG_DIR` copy step. restic and the
cross-sessions skill are unaffected. **`review` (Opus 5) before each commit is
sufficient; `deep` is not required.** The only live-store touch is A, run by the
orchestrator in the foreground.

## Verification (every code item)
- `python -m pytest -q -n 4` at BELOW_NORMAL priority (baseline 2026-09-04:
  2092 passed, 77 skipped, ~60 s).
- Scratch-store manual trial: a scratch `CLAUDE_CONFIG_DIR` + scratch backup dir
  with fixture usage (three accounts, staggered `seven_day.resets_at`, one 5h-hot)
  driving `cswap auto --dry-run --json --once` and the TUI auto screen.
- Live smoke: `cswap status`, then Jeremy relaunches the TUI and reads the
  first poll line.

## Upstream PR plan
Topic branches cut from `v0.26.0` with only code commits cherry-picked (never
`.claude/`, `docs/`, `workflows/`, `tools/`):
1. `feat/tui-next-best-follows-strategy` — C + D. Depends on #313 being merged
   first (or rebases onto it); reference #305 and #313 in the description.
2. `feat/consume-first-5h-guard` — E. Reference #303.
Once merged, the fork's delta shrinks to the WAT frame plus whatever is still
in flight.

## Open items / assumptions
- E's margin = `hysteresisPct`; confirm with Jeremy before implementing E if he
  wants it independent of the `best`-strategy hysteresis.
- `cswap list` reads headroom without the `models` argument
  (`switcher.py:215`) — display-only, out of scope, note for upstream later.
- The consume-first proactive move ranks by the **7d** reset, not the Fable
  window's; on Jeremy's accounts the two coincide. Revisit only if they diverge.

## Status log
- 2026-09-04 — planning session: findings verified, decisions taken, plan
  written. Live monitor was running the whole time (untouched). Next: Jeremy
  exits the TUI, orchestrator runs A, Jeremy relaunches; then B.
- 2026-09-04 (later) — built out. A: `cswap config set` model Fable / strategy
  consume-first / threshold 95 (TUI exited first). B: PR #313 cherry-picked clean
  (0a000f3). E: `consume_first_key` + 5h-hot tier (ceeabe2), reviewed; review
  fixes made the trigger-scoping test discriminate and added proactive coverage.
  C+D: Next best follows the strategy, `s` hotkey (163ab4f), reviewed; fixes added
  the health tier, gate-faithful muting, reset-unknown idle line, stable tie order.
  Full suite 2113 passed / 77 skipped. Open: upstream PRs (C+D depends on #313
  merging); `cswap list` headroom without models filter noted upstream-later.
