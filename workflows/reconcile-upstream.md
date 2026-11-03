# Workflow: reconcile `jeremy` onto a new upstream release

**Objective:** move the fork (and the live `cswap` on the dev box) onto a new
upstream release tag without losing a fork feature, without reverting an upstream
fix, and with about a minute of auto-switch downtime.
**Written:** 2026-11-02 from the v0.26.0 → v0.29.0 reconcile
(`docs/plans/2026-11-02-reconcile-v0.29.0.md`, decisions.md 2026-11-02).
**Trigger:** Jeremy sees cswap's "newer version available" notice, or asks.
**Never:** `cswap upgrade`, `pip install claude-swap`, `pip install --upgrade
claude-swap`. Each replaces the editable install with the PyPI wheel.

## Inputs
- `<new>`, the release tag (`git fetch upstream --tags`; `git tag -l 'v*' --sort=v:refname`).
  Use a release tag, never `upstream/main`, which carries betas.
- `<old>`, the tag `jeremy` sits on (`git describe --tags $(git merge-base jeremy upstream/main)`).

## Tools
- `tools/run_tests_low.py`: pytest at BELOW_NORMAL, args passed through. It tests the
  `src/` of the checkout it runs from.
- `tools/trial/`: scratch-store trial kit.
  - `guard.py` points home/config at a scratch dir and blocks the real store,
    `~/.claude`, keyring, network and subprocesses.
  - `fake_account.py <a..i>` and `cswap_trial.py <cswap args>` drive it.
  - Set `CSWAP_TRIAL_SRC` to the worktree's `src`, and `CSWAP_TRIAL_HOME` to a fresh dir.
- Agents (routing.md): `tool-runner` resolves conflicts, `review` checks the merge,
  `deep` is the adversarial verifier.

## Steps

### 1. Size it (orchestrator, read-only)
- Fork delta: `git rev-list --count <old>..jeremy` and `git diff --stat <old>..jeremy -- src tests pyproject.toml`.
- Upstream delta: the same two commands over `<old>..<new>`.
- Overlap: files changed on both sides (`comm -12` of the two `--name-only` lists).
- Scan `git log --oneline <old>..<new>` for credential, session, path or migration commits.
  Those put `deep` in scope.
- Write `docs/plans/<date>-reconcile-<new>.md`. Tag the current tip
  `pre-<new>-reconcile` and push the tag. That tag is the rollback point.

### 2. Pick the method
- **Small overlap** (upstream didn't restructure code the fork builds on): try
  `git rebase <new>` in a worktree first (`git worktree add .tmp/reconcile-<new> -b reconcile/<new> jeremy`).
  If the stops are few and local, finish it.
- **Restructured overlap** (the v0.29.0 case: upstream rewrote the autoswitch ranking
  under 20 fork commits): `git rebase --abort` and do a **final-state port**.
  1. `git checkout -B reconcile/<new> <new>` in the worktree.
  2. **Frame replay.** For every fork commit in order, apply only its non-code paths:
     `git diff --binary c^ c -- . ':!src' ':!tests' ':!pyproject.toml' | git apply --index`.
     Commit pure-frame commits with `git commit -C c`. For mixed commits, copy the
     original author and date, and append "[frame part only]" to the message.
     Upstream never touches the frame, so this never conflicts. Whole-commit
     cherry-pick does conflict, because mixed commits carry docs.
  3. **Fork-only files:** `git show jeremy:<f> > <f>`.
  4. **Files both sides changed:** 3-way merge with **all three sides taken from git
     objects**. Then save the marked-up state as a ref
     (`refs/reconcile/automerge`) so review can diff against it.
     ```bash
     git show <new>:$f > cur; git show <old>:$f > base; git show jeremy:$f > ours
     git merge-file -L <new> -L <old> -L jeremy cur base ours; cp cur $f
     ```
     Never merge against the working-tree file. Its CRLF endings against LF blobs
     make every file one giant conflict.

### 3. Resolve (parallel `tool-runner`s, split by file ownership)
- Engine (`autoswitch.py` + its tests), settings/cli/switcher (+ warmup and
  engine_control verification), and TUI. Agent B does `settings.py` FIRST, because
  everyone imports it.
- The rule in every brief: **upstream's structure and semantics win where upstream
  changed them; re-apply the fork's intent on top.** Where upstream now fixes what a
  fork commit fixed, keep upstream's version. Every judgment call gets a file:line.
- Relay cross-agent notes while they're still running (SendMessage is cheap
  mid-run). Example from v0.29: a new upstream strategy had to be handled by the TUI
  `s` cycle.
- Subagents can't run git writes (guard hook). The orchestrator commits.

### 4. Verify
1. No markers: `grep -rln '^<<<<<<<\|^>>>>>>>' src tests`.
2. Full suite from the worktree: `python ../../tools/run_tests_low.py -q -n 4 -p no:cacheprovider`
   (about 2.5 min; run it in the background).
3. Scratch trial through `tools/trial/`. Cover:
   - `add` ×2–3, `list`, `status`
   - `switch` round-trip: the live creds must be byte-identical to the stored ones,
     and no stray temp files may remain
   - `config get/set` strategy, `auto --once --dry-run`, `warm --dry-run`
   - `engine status` (expect exit 3)
   - any new upstream commands
   - `setup_session` called directly, for the slot tree
   - `--version` reads the OLD version until the reinstall. That's expected.
4. `review` (fresh context): lost fork behaviour, lost upstream behaviour, the
   judgment calls, dead code. Fix its findings with tests that fail without the fix.
5. `deep`, adversarial ("refute: safe"), whenever credential, session, path or
   migration code changed upstream. Cover credential writes on Windows; the slot
   layout that restic (`backup-devbox.ps1`) and cross-sessions read; **rollback**
   (can the old code read what the new code writes?); and the launch path.
6. **Remote hub contract.** CC-JS_Claude_Remote's `claude_remote/cswapctl.py`
   calls `engine status|pick|strategy --json`, `config get|set autoswitch.strategy`,
   `status --json` and `switch --json`. Check that the JSON fields it reads still
   exist, and that its `STRATEGIES` list equals the fork's `SELECTABLE_STRATEGIES`.
   A new upstream strategy needs a ruling: add it to the hub, or suppress it via
   `settings.SUPPRESSED_STRATEGIES`.

### 5. Commit (orchestrator)
One concern per commit (settings/cli, warmup/engine, autoswitch, tui, docs). Push
`reconcile/<new>` as a backup branch.

### 6. Cutover (Jeremy's OK first; about a minute of downtime)
1. Jeremy quits the `cswap` TUI (`q`). Don't kill it, and kill by PID only if you must.
2. Confirm no process has a command line matching `cswap|claude_swap`.
3. In the main checkout (on `jeremy`, clean):
   - `git reset --hard reconcile/<new>`
   - `python -m pip install -q -e .`
   - `cswap --version`, which should read `<new>`
   - `cswap status`, `cswap config get autoswitch.strategy`
4. Jeremy relaunches `cswap` and puts it in live mode.
   Then run `cswap engine status --json`: expect running, live, the expected strategy.
5. Housekeeping:
   - `git push --force-with-lease origin jeremy`
   - `git checkout main && git merge --ff-only upstream/main && git push origin main`
   - `git worktree remove`, delete the `reconcile/<new>` branch locally and on origin,
     `git update-ref -d refs/reconcile/automerge`
   - Mark the plan DONE.

## Outputs
Live `cswap` at `<new>` with the fork's features; `jeremy` pushed; plan and decisions updated.

## Rollback
`git checkout -B jeremy pre-<new>-reconcile && pip install -e .` (no live cswap
process), then relaunch. Before relying on it, `deep` step 4.5 must have confirmed
the old code reads the new code's store.

## Edge cases and lessons
- **Watch after cutover:** a switch refused with "live session-mode Claude (PID N)" in
  a slot with nothing running is a stale session record. On Windows a reused PID can
  read as live. Remove the record by name.
- **A new upstream test that contradicts a fork rule:** keep the fork rule, rename
  the test to say so, and add a sibling test that keeps the upstream test's
  discriminating intent (v0.29: failover ranking).
- **Display and engine keys:** after a port, any TUI code that mirrors an engine key
  (Next best, `consume_first_key`) must pass the same inputs as the engine.
  Review caught a mismatch here in v0.29.
- **diff3 on append-only files:** both sides may have appended only new classes at the
  end, and the merge still shows a few fake conflicts. Rebuild the tail as upstream's
  tail followed by the fork's tail, then check each against its source ref.
- **Line endings:** the working tree mixes CRLF and LF after merge-file. Git
  normalizes on add, so there's no content diff; don't "fix" it file by file.
