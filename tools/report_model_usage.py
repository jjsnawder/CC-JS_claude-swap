"""Aggregate Claude Code token usage for this project by model and role.

Parses the project's session transcripts (JSONL under ~/.claude/projects/<slug>/)
and regenerates docs/model-usage.md with per-model token totals and a cost
extrapolation at API list prices.

Reads ONLY the `usage` metadata of each message — never message content, so no
customer PII or credentials ever enter the report.

Usage:
    python tools/report_model_usage.py            # writes docs/model-usage.md
    python tools/report_model_usage.py --stdout   # print only, write nothing
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_PATH = os.path.join(REPO_ROOT, "docs", "model-usage.md")

# API list prices, USD per million tokens (input, output).
# Cache multipliers per Anthropic pricing: read = 0.1x input,
# 5m cache write = 1.25x input, 1h cache write = 2x input.
PRICES = {
    # Order matters: price_for() matches by prefix, so a point release must
    # sit ABOVE its parent ("claude-fable-5-1" before "claude-fable-5").
    "claude-fable-5-1": (10.00, 50.00),
    "claude-mythos-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
CACHE_READ_MULT = 0.10
# Flat per-MTok cache-read prices that override the 0.1x-of-input rule.
# Fable 5.1 / Mythos 5.1 (released 2026-09-01) read cache at $0.25/MTok
# (0.025x input) - 75% below Fable 5's $1.00. Prefix-matched like PRICES.
CACHE_READ_PRICE = {
    "claude-fable-5-1": 0.25,
    "claude-mythos-5-1": 0.25,
}
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.00


def project_slug(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def transcript_dirs() -> list[str]:
    """All transcript stores for this project. claude-swap (adopted 2026-08-08)
    keeps a per-account store under ~/.claude-swap-backup/sessions/<acct>/projects/,
    so the classic ~/.claude/projects/<slug> may not exist or may be partial;
    scan both (dedup by message id makes overlap harmless)."""
    home = os.path.expanduser("~")
    slug = project_slug(REPO_ROOT)
    candidates = [os.path.join(home, ".claude", "projects", slug)]
    candidates += glob.glob(
        os.path.join(home, ".claude-swap-backup", "sessions", "*", "projects", slug)
    )
    return [d for d in candidates if os.path.isdir(d)]


def price_for(model: str):
    for prefix, p in PRICES.items():
        if model.startswith(prefix):
            return p
    return None


def cache_read_price(model: str, in_p: float) -> float:
    """Per-MTok cache-read price: flat override where one exists, else 0.1x input."""
    for prefix, p in CACHE_READ_PRICE.items():
        if model.startswith(prefix):
            return p
    return in_p * CACHE_READ_MULT


def classify(rel_path: str) -> str:
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) == 1:
        return "orchestrator (main session)"
    if "subagents" in parts:
        return "subagent"
    return "other"


class Bucket:
    __slots__ = ("messages", "inp", "cache_5m", "cache_1h", "cache_read", "out")

    def __init__(self):
        self.messages = 0
        self.inp = 0
        self.cache_5m = 0
        self.cache_1h = 0
        self.cache_read = 0
        self.out = 0

    def add(self, u: dict):
        self.messages += 1
        self.inp += u.get("input_tokens", 0) or 0
        cc = u.get("cache_creation") or {}
        if cc:
            self.cache_5m += cc.get("ephemeral_5m_input_tokens", 0) or 0
            self.cache_1h += cc.get("ephemeral_1h_input_tokens", 0) or 0
        else:
            # no TTL breakdown available; count as 5m (cheaper assumption noted in report)
            self.cache_5m += u.get("cache_creation_input_tokens", 0) or 0
        self.cache_read += u.get("cache_read_input_tokens", 0) or 0
        self.out += u.get("output_tokens", 0) or 0

    def cost(self, model: str):
        p = price_for(model)
        if p is None:
            return None
        in_p, out_p = p
        return (
            self.inp * in_p
            + self.cache_5m * in_p * CACHE_WRITE_5M_MULT
            + self.cache_1h * in_p * CACHE_WRITE_1H_MULT
            + self.cache_read * cache_read_price(model, in_p)
            + self.out * out_p
        ) / 1_000_000


def collect(tdirs: list[str]):
    """Return {(role, model): Bucket}, deduped by message id (streamed messages
    can appear on multiple lines; the last line carries final usage)."""
    files = []
    for tdir in tdirs:
        for path in sorted(
            glob.glob(os.path.join(tdir, "**", "*.jsonl"), recursive=True)
        ):
            files.append((tdir, path))
    seen: dict[str, tuple] = {}  # msg key -> (role, model, usage)
    ts_min, ts_max = None, None
    for tdir, path in files:
        rel = os.path.relpath(path, tdir)
        if rel.startswith("memory"):
            continue
        role = classify(rel)
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for lineno, line in enumerate(fh):
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                msg = obj.get("message") or {}
                usage = msg.get("usage")
                model = msg.get("model")
                if not usage or not model or model == "<synthetic>":
                    continue
                key = msg.get("id") or obj.get("uuid") or f"{path}:{lineno}"
                seen[key] = (role, model, usage)
                ts = obj.get("timestamp")
                if ts:
                    ts_min = min(ts_min or ts, ts)
                    ts_max = max(ts_max or ts, ts)
    buckets: dict[tuple, Bucket] = {}
    for role, model, usage in seen.values():
        buckets.setdefault((role, model), Bucket()).add(usage)
    return buckets, len(seen), ts_min, ts_max


def fmt(n: int) -> str:
    return f"{n:,}"


def render(buckets, n_msgs, ts_min, ts_max) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Model utilization — {os.path.basename(REPO_ROOT)}",
        "",
        f"> Regenerated {now} by `tools/report_model_usage.py`. Do not hand-edit.",
        f"> Source: Claude Code transcripts for this project ({n_msgs} assistant messages",
        f"> spanning {ts_min or '?'} -> {ts_max or '?'}). Usage metadata only — no message",
        "> content, PII, or credentials are read.",
        "",
        "## Totals by role and model",
        "",
        "| Role | Model | Msgs | Input | Cache write | Cache read | Output | Est. cost (API list) |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    total_cost = 0.0
    grand = Bucket()
    for (role, model), b in sorted(buckets.items()):
        c = b.cost(model)
        cost_s = f"${c:,.2f}" if c is not None else "n/a (unknown model)"
        if c is not None:
            total_cost += c
        grand.messages += b.messages
        grand.inp += b.inp
        grand.cache_5m += b.cache_5m
        grand.cache_1h += b.cache_1h
        grand.cache_read += b.cache_read
        grand.out += b.out
        lines.append(
            f"| {role} | `{model}` | {fmt(b.messages)} | {fmt(b.inp)} "
            f"| {fmt(b.cache_5m + b.cache_1h)} | {fmt(b.cache_read)} "
            f"| {fmt(b.out)} | {cost_s} |"
        )
    lines += [
        f"| **total** | | **{fmt(grand.messages)}** | **{fmt(grand.inp)}** "
        f"| **{fmt(grand.cache_5m + grand.cache_1h)}** | **{fmt(grand.cache_read)}** "
        f"| **{fmt(grand.out)}** | **${total_cost:,.2f}** |",
        "",
        "## Assumptions",
        "",
        "- Prices are Anthropic API list prices per MTok: Fable 5 and 5.1 $10/$50,",
        "  Opus 5 and Opus 4.x $5/$25, Sonnet $3/$15, Haiku 4.5 $1/$5.",
        "- Cache read billed at 0.1x input (Fable 5.1: flat $0.25/MTok); cache writes",
        "  at 1.25x (5-min TTL) or",
        "  2x (1-hour TTL) input, using the per-TTL breakdown in the transcripts.",
        "- This session runs on a Claude subscription, not pay-per-token API —",
        "  the cost column is an *extrapolation* of what the same usage would",
        "  cost at API list prices, useful for comparing orchestrator vs",
        "  subagent spend, not an invoice.",
        "- Messages are deduplicated by message id (streamed messages appear on",
        "  multiple transcript lines; the final line's usage wins).",
        "- Long-context (>200K input) pricing premiums are NOT modeled, so the",
        "  orchestrator figure understates somewhat once its context grows large.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stdout", action="store_true", help="print report, write nothing")
    args = ap.parse_args()

    tdirs = transcript_dirs()
    if not tdirs:
        print(
            "ERROR: no transcript dir found (checked ~/.claude/projects and "
            "~/.claude-swap-backup/sessions/*/projects)",
            file=sys.stderr,
        )
        return 1
    buckets, n_msgs, ts_min, ts_max = collect(tdirs)
    if not buckets:
        print("ERROR: no usage records found", file=sys.stderr)
        return 1
    report = render(buckets, n_msgs, ts_min, ts_max)
    # Windows consoles default to cp1252; never let an em dash kill the run.
    sys.stdout.reconfigure(errors="replace")
    if args.stdout:
        print(report)
        return 0
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    tmp = REPORT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(report)
    os.replace(tmp, REPORT_PATH)
    print(f"Wrote {REPORT_PATH}")
    print(report.split("## Assumptions")[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
