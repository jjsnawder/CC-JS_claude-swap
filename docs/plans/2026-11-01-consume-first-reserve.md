# Plan — `consume-first-reserve-jj` strategy

Date: 2026-11-01 · Branch: `jeremy` · Status: **planned**
Shape: **fork-only** (Jeremy, 2026-11-01), separable so it rebases cleanly.

## Ask
A third auto-switch strategy: consume-first, except that one or more
configured "reserve" accounts are always consumed LAST. (Jeremy's case: his
own account #1. The repo is public, so the account is a setting, never code.)

## Configuration
- `autoswitch.strategy` gains the choice `consume-first-reserve-jj`
  (settings spec, `cswap auto --strategy`, `s` cycle picks it up for free).
- New `autoswitch.reserveAccounts` (camelCase JSON key; field
  `reserve_accounts`): comma-separated account numbers, e.g. `"1"` or `"1,4"`.
  Default `"1"` (Jeremy, 2026-11-01); `none` = no reserve. Session-only
  override `--reserve NUMS|none` on `cswap auto` and the TUI launch
  (`cswap`, `cswap tui`, `cswap watch`, which also take `--strategy`).
- Strategy set but reserve list empty → behaves exactly like `consume-first`.
- Everywhere the code asks "is this consume-first?" (`consume_first` bool in
  the engine, warmup ordering, Next best panel, API-key gates), the new
  strategy answers yes. One helper, not scattered string compares.

## Behaviour contract (S = reserve set)
1. **Reserve last.** A reserve account is a switch target only when no
   non-reserve account is a usable landing (below the threshold, headroom
   > 0, not quarantined/unhealthy). This holds for every trigger:
   proactive, consume-first nudge, at-limit, failover, `n`, and the
   all-accounts-above-threshold recovery mode. Escapes therefore prefer a
   reserve below the threshold over a non-reserve at/over it; if every
   non-reserve AND every reserve is at/over, today's escape/recovery ranking
   applies with reserves still sorted after non-reserves.
2. **Never nudged onto.** The below-threshold consume-first nudge from a
   non-reserve active never targets a reserve (nothing forces the move).
3. **Leave the reserve right away** (Jeremy, 2026-11-01). Active ∈ S and
   below the threshold → the consume-first nudge runs with the
   strictly-sooner-weekly-reset gate WAIVED for non-reserve candidates. Kept:
   landing-health gate, cooldown, the `m` manual-pick pin, the no-return bar,
   two-phase fresh fetch. Targets only non-reserve accounts.
4. **`m` unchanged.** An explicit pick of a reserve is allowed (same rules
   as today) and the pin holds it until the threshold.
5. **Display/warmup agree.** Next best panel ranks reserves after every
   non-reserve and tags them `reserve`; the warmup stagger (which ranks via
   `_rank_candidates`) inherits the order. TUI summary shows the strategy and
   the reserve list. Events moving off a reserve say so in their `note`.

## Shape (rebase-friendly)
- Reserve logic in small helpers (module-level, pure) in `autoswitch.py`,
  called from `_rank_candidates` / `_tick_inner` with targeted edits; no
  rewrite of existing blocks. Panel imports the same helper.
- New tests in `tests/test_autoswitch_reserve.py` (own file = no conflicts);
  settings/CLI additions in their existing test files.

## Blast radius
Switch DECISION path only (ranking, trigger gates), plus settings/CLI/TUI
display. No credential read/write, store layout, slot-copy or launch change,
so restic and cross-sessions are out of scope. Verification: full suite,
`review` (Opus) before commit, `cswap status` smoke test.

## Tests that prove it
- Reserve with most headroom / soonest reset is still ranked last under
  proactive, at-limit, failover, manual, all-above.
- Escape picks reserve below threshold over non-reserve over threshold.
- Nudge from non-reserve never lands on reserve.
- On reserve below threshold: moves to a non-reserve with a LATER weekly
  reset; does not move during cooldown, under the `m` pin, or when no
  non-reserve is healthy.
- Empty reserve list == consume-first (parametrized parity).
- Settings validation, CLI choice/flag, `s` cycles three strategies,
  panel order + `reserve` tag.
