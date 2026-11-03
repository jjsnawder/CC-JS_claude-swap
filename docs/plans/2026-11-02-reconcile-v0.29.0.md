# Reconcile: rebase `jeremy` onto upstream v0.29.0

**Status:** DONE — cut over 2026-11-02; live `cswap` is 0.29.0 + fork.
**Kickoff:** Jeremy saw the 0.29.0 update notice; approved a rebase (never `cswap upgrade`,
which would replace the editable install with the PyPI wheel). Constraint: minimal
auto-switch downtime.

## Why
v0.26.0 → v0.29.0 is 120 upstream commits. Worth having: credential temp file 0600 from
creation and in-place replace in `switcher._write_json`; torn `history.jsonl` survival;
`auto --once` failover on an unreadable active account; most-room ranking when every
account is over its lines; `~/.claude/rules` shared into session profiles.

## Shape
- Fork delta: 57 commits (37 docs/frame-only, 20 touch src/tests).
- Both sides changed: `autoswitch.py` (ours +1442, theirs +758/-385), `switcher.py`
  (theirs +625/-141), `cli.py`, `settings.py`, `tui/app.py`, `tui/autoview.py`,
  `tui/modals.py`, and tests `conftest`, `test_autoswitch`, `test_cli`, `test_config_cli`,
  `test_settings`, `test_tui`.
- `pyproject.toml`: version line only → `pip install -e .` at cutover.

## Blast radius
- `switcher.py` credential write path changed upstream → `deep` verifies.
- Upstream `session.py` now shares `~/.claude/rules` into slot dirs → slot layout changes;
  restic (`backup-devbox.ps1` slot discovery) and the cross-sessions skill are in scope
  for `deep`.
- Our behaviour that must survive: consume-first + consume-first-reserve-jj ranking,
  warmup stagger + mid-hello switch refusal, manual `n`/`m` triggers, engine control
  (`cswap engine status|pick|strategy`), TUI auto screen panels.

## Steps
1. Worktree `.tmp/reconcile-v029` on branch `reconcile/v0.29.0` (from `jeremy`);
   `git rebase v0.29.0` there. Live install (main checkout `src/`) untouched; the running
   `cswap` TUI/auto keeps going.
2. Resolve conflicts per stop: keep upstream's restructuring, re-apply our intent on top.
3. Full suite in the worktree (`pythonpath = ["src"]` makes it test the worktree copy),
   BELOW_NORMAL, `-n 4`.
4. Scratch-store trial (scratch `CLAUDE_CONFIG_DIR` + backup dir): list, switch, auto --once,
   engine status.
5. `review` (Opus) on the conflict resolutions; `deep` (Fable, adversarial) on switcher
   credential writes + slot layout.
6. Jeremy OK → cutover (downtime ≈ 1 min): stop live cswap by PID, `jeremy` reset to the
   rebased branch (ff not possible after rebase; checkout -B), `pip install -e .`,
   relaunch, `cswap status`; `git push --force-with-lease origin jeremy`;
   `main` ff to `upstream/main`.
7. Write `workflows/reconcile-upstream.md` from what this run taught (with Jeremy's OK).

## Rollback
Before cutover: delete the worktree/branch; nothing live changed. After cutover: the
pre-rebase tip is tagged `pre-v029-reconcile` → `git checkout -B jeremy pre-v029-reconcile`
+ `pip install -e .`.

## Log
- 2026-11-02: plan written; rebase started, stopped on the first code commit (autoswitch ranking rewritten upstream). Switched to a final-state port (decisions.md 2026-11-02): 40 frame commits replayed (2 of them the frame half of mixed commits) onto v0.29.0; code 3-way merged per file (baseline ref `refs/reconcile/automerge`; conflicts: autoswitch 18, autoview 4, cli 3, test_config_cli 3, test_autoswitch 3, settings 2, others 1, switcher 0); three tool-runners resolving by file ownership.
- 2026-11-02: agents A/B/C resolved all conflicts; full suite 3286 passed / 121 skipped / 0 failed. Scratch trial (fake accounts, keyring/network/real-store blocked by a guard): list/status/switch round-trip byte-exact, no stray temps, config/auto/warm/engine/threshold/priority all behave; slots now get a `rules/` copy. `review`: no lost fork or upstream behaviour; W1 panel/engine consume-first key mismatch, W2 `n` under priority undone after cooldown (ruling in decisions.md), W3 warmup share_history (POSIX only), W4 flipped test lost its discriminating power, nit on test_config_cli key list — all sent for fixing. `deep` (adversarial): credential writes, slot layout (restic + cross-sessions), data migration/rollback and launch path all HOLD. Post-cutover watch: a switch refused for a "live session-mode Claude (PID N)" in a slot with nothing running = stale session record (Windows PID reuse). Optional env-repo follow-up: restic `--exclude '.credentials.json*'` (pre-existing gap for hard-kill temps).
- 2026-11-02: review fixes landed (suite 3291/0 failed); Jeremy asked to suppress priority-consume-first → fork-only `SUPPRESSED_STRATEGIES` (suite 3298/0). Cutover: Jeremy quit the TUI, no cswap process left; `jeremy` reset to the port tip, `pip install -e .`, `cswap --version` = 0.29.0, `cswap status` read all 4 accounts, strategy still consume-first-reserve-jj. `jeremy` force-with-lease pushed, `main` ff'd to upstream/main, worktree/branch/automerge ref removed. Open: step 7 (reconcile workflow) awaits Jeremy's OK.
- 2026-11-02: step 7 done (Jeremy OK): `workflows/reconcile-upstream.md`; trial kit promoted to `tools/trial/` + `tools/run_tests_low.py`. Env repo: restic exclude widened to `.credentials.json*` (verified on a throwaway repo).
