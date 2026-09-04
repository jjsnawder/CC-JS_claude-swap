# CC-JS_claude-swap — project instructions

This is a **fork of [realiti4/claude-swap](https://github.com/realiti4/claude-swap)** (`cswap`,
the Claude Code multi-account switcher, MIT). On this box `cswap` runs **this checkout**:
the package is installed *editable* into system Python 3.13 (`pip install -e .`), so an edit
to any `.py` under `src/claude_swap/` is live on the next `cswap` invocation. No build step.
Only a change to `pyproject.toml` (entry points, dependencies) needs `pip install -e .` re-run.

**This repo is PUBLIC** (GitHub requires forks of public repos to be public). Nothing personal
goes in it: no account emails, slot rosters, usage numbers, internal hostnames, or anything
from `~/.claude-swap-backup/`. Operator-specific notes live in the private env repo
(`CC-JS_Claude_Environment/machine-setup/claude-swap-cheatsheet.md`).

## Branch and remote model
| Name | Role | Rule |
|---|---|---|
| `upstream` remote | `realiti4/claude-swap` | Read-only source of truth. Never push to it. |
| `origin` remote | `jjsnawder/CC-JS_claude-swap` | Our fork. |
| `main` | Pristine mirror of `upstream/main` | **Never commit here.** Fast-forward only. |
| `jeremy` | The working branch — every local change lives here | Based on the latest upstream **release tag** (v0.26.0 at fork time, 2026-09-04), not `upstream/main`, which carries pre-release betas. |

## Reconciling with upstream (when a new release lands)
```bash
git fetch upstream --tags
git checkout main && git merge --ff-only upstream/main && git push origin main
git checkout jeremy && git rebase v<new-tag>        # replay our commits on the new release
# resolve any conflicts (only where upstream touched the same lines we did), then:
git push --force-with-lease origin jeremy           # the ONLY sanctioned history rewrite; our branch, our fork
pip install -e .                                    # only if pyproject.toml changed
python -m pytest -q                                 # confirm the merge before trusting it
```
A change that is generic (not Jeremy-specific) goes upstream as a PR from `jeremy` or a topic
branch. Once merged, that part of our diff disappears — the smaller the fork's delta, the
cheaper each reconcile.

## Never do
- **Never `pip install claude-swap` or `pip install --upgrade claude-swap`.** That replaces the
  editable install with the PyPI wheel and silently drops every local change. Updates arrive
  through git only.
- **Never reinstall while a `cswap` process is running** (pip cannot replace an in-use
  `cswap.exe`). Check with `Get-CimInstance Win32_Process` by command line; never kill by name.
- **Never test against the live account store.** `~/.claude-swap-backup/` holds plaintext OAuth
  tokens. Unit tests use the fixtures under `tests/`; manual trials use a scratch
  `CLAUDE_CONFIG_DIR` and a scratch backup dir.
- Never print, log, copy, or commit credential material, even redacted.

## Development
- Layout is upstream's: `src/claude_swap/` (package, `cli.py` is the entry), `src/claude_swap/tui/`
  (Textual dashboard), `tests/` (pytest + pytest-asyncio + pytest-xdist). `uv.lock` is upstream's
  lockfile; on this box we use system Python 3.13 + pip, not uv.
- Test deps: `pip install pytest pytest-asyncio pytest-xdist`. Run the suite at BELOW_NORMAL
  priority with a bounded worker count (`-n 4`), per the dev-box compute rules in the global
  CLAUDE.md.
- Commit discipline: one concern per commit, upstream's style (`fix(scope): …`, `feat(scope): …`).
  Keep Jeremy-specific behaviour behind a config flag or clearly separable module where
  possible, so it rebases cleanly and could be offered upstream.
- Commit trailer: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Planning session — how this project starts
Jeremy opens a session in this directory and says "I want to change…". Before touching code:
1. Locate the module(s) that own the behaviour (`grep -rn` under `src/claude_swap/`), and read
   the matching `tests/test_<module>.py` to learn the contract.
2. State whether the change is (a) generic → PR upstream, or (b) Jeremy-specific → fork-only.
   This decides how it is structured, not whether it is done.
3. Write the plan to `docs/plans/<date>-<topic>.md` (committed — it documents the
   fork's delta) before editing.
4. Implement on `jeremy`, run the suite, verify manually against a scratch config dir, then a
   live `cswap status` as the smoke test.
