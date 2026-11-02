# Reconcile: rebase `jeremy` onto upstream v0.29.0

**Status:** in progress (2026-11-02)
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
- 2026-11-02: plan written; rebase started.
