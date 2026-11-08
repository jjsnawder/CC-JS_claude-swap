"""Fleet lease: the leader → worker message of ``cswap fleet`` (contract ``cswap-fleet/1``).

A dev box whose engine is live (the leader) sends each worker a *lease* on
every tick: the account the worker should be on, the leader's live
autoswitch settings, a salted identity hash per slot and the leader's
``cswap list --json`` usage. The worker (``cswap fleet receive``) validates
it, stores it at ``<backup_root>/fleet/lease.json`` stamped with its OWN wall
clock, adopts the usage with a hold of ``ttl_s`` and answers with a snapshot.
The worker engine reads the stored lease through :func:`active_lease` at the
top of each tick; once ``received_at + ttl_s`` passes on the worker's clock
the lease is gone and the worker runs autonomously. The two clocks never have
to agree.

Nothing here moves credentials, and no email address is ever put in a lease
field this module creates, a log line or the stored lease: identities travel
as salted hashes. (The ``usage`` field is the leader's ``list --json``
payload verbatim, which the import-usage path matches by identity.)

This module imports nothing heavy at module level so ``autoswitch`` may
import it from its tick hook without a cycle.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher

CONTRACT = "cswap-fleet/1"
DEFAULT_TTL_S = 180
MIN_TTL_S = 30
MAX_TTL_S = 900

# The one home of the fleet file names (fleet_leader / fleet_remote import them).
FLEET_DIRNAME = "fleet"
LEASE_FILENAME = "lease.json"
HOSTS_FILENAME = "hosts.json"
TARGET_MODES = ("follow", "pin", "strategy")
RELOGIN_WINDOW_S = 7 * 86400
# A lease stamped more than this far in the worker's future means the
# worker's clock stepped backwards since receipt: distrust it rather than let
# the step stretch the lease's life.
_FUTURE_SLACK_S = 5.0
_ENGINE_STATUS_WAIT_S = 5.0
_MAX_ID_LEN = 128

_logger = logging.getLogger("claude-swap")


class LeaseError(ValueError):
    """A lease that is malformed, of another contract, or stale. Nothing was written."""


@dataclass(frozen=True)
class Lease:
    """A stored lease that is still valid on this box's clock."""

    leader_id: str
    seq: int
    ttl_s: float
    received_at: float
    target_mode: str
    target_account: str | None
    target_strategy: str | None
    settings: dict
    follow_blocked: str | None
    # Slots whose leader-side identity differs from this box's: the leader's
    # per-slot policy must not land on a different local account.
    identity_mismatch: tuple[str, ...] = ()


# -- paths --------------------------------------------------------------------


def fleet_dir(backup_root: Path) -> Path:
    return Path(backup_root) / FLEET_DIRNAME


def lease_path(backup_root: Path) -> Path:
    return fleet_dir(backup_root) / LEASE_FILENAME


# -- identity -----------------------------------------------------------------


def identity_hash(email: str, org_uuid: str | None, salt: str) -> str:
    """sha256 hex of salt + lowercased email + org uuid (NUL-separated).

    Never log the inputs. The salt is random per lease, so a hash says
    nothing about the account outside the lease that carries it.
    """
    material = "\x00".join((salt, (email or "").strip().lower(), org_uuid or ""))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _slot_identities(switcher: ClaudeAccountSwitcher) -> dict[str, tuple[str, str]]:
    """Managed slot → (email, org uuid) from sequence.json."""
    data = switcher._get_sequence_data() or {}
    accounts = data.get("accounts") or {}
    out: dict[str, tuple[str, str]] = {}
    for num in data.get("sequence", []):
        record = accounts.get(str(num))
        if not isinstance(record, dict):
            continue
        email = record.get("email")
        if not isinstance(email, str) or not email:
            continue
        org = record.get("organizationUuid") or ""
        out[str(num)] = (email, org if isinstance(org, str) else "")
    return out


# -- dev-box side -------------------------------------------------------------


def list_payload(switcher: ClaudeAccountSwitcher) -> dict:
    """The ``cswap list --json`` payload from the CLI's own producer, STORE-ONLY.

    ``fetch=set()`` (as the TUI and menubar do while an engine runs): this
    runs on the leader's thread beside a live engine, and a fetching list
    would claim stale slots and could spend refresh grants. Usage is
    whatever the store already holds."""
    from claude_swap.json_output import SCHEMA_VERSION

    try:
        payload = switcher.list_accounts(json_output=True, fetch=set())
    except Exception as exc:  # a failing list must not take the lease/snapshot down
        _logger.warning("fleet: list payload failed (%s)", type(exc).__name__)
        payload = None
    if not isinstance(payload, dict):
        return {
            "schemaVersion": SCHEMA_VERSION,
            "activeAccountNumber": None,
            "accounts": [],
            "error": "list-failed",
        }
    return payload


def build_lease(
    switcher: ClaudeAccountSwitcher,
    *,
    leader_id: str,
    seq: int,
    target: dict,
    settings: dict,
    ttl_s: int = DEFAULT_TTL_S,
    usage: dict | None = None,
) -> dict:
    """The lease for one worker (dev-box side).

    ``target`` = ``{"mode": "follow"|"pin"|"strategy", "account": "N"|None,
    "strategy": str|None}``. ``usage``: a :func:`list_payload` the caller
    built once for a whole round (None = build one here). The result passes
    :func:`validate_lease`; a bad argument raises :class:`LeaseError` here
    rather than on the worker.
    """
    salt = secrets.token_hex(16)
    lease = {
        "contract": CONTRACT,
        "leader": {"id": leader_id, "seq": seq, "ttl_s": ttl_s},
        "target": {
            "mode": target.get("mode"),
            "account": target.get("account"),
            "strategy": target.get("strategy"),
        },
        "identity": {
            "salt": salt,
            "slots": {
                num: identity_hash(email, org, salt)
                for num, (email, org) in _slot_identities(switcher).items()
            },
        },
        "settings": dict(settings),
        "usage": list_payload(switcher) if usage is None else usage,
    }
    validate_lease(lease)
    return lease


# -- validation ---------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _account_str(value: Any) -> str:
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise LeaseError("target.account must be a digit string")
    if int(value) < 1:
        raise LeaseError("target.account must be a slot number of 1 or more")
    return str(int(value))


def validate_lease(payload: Any) -> dict:
    """Check a lease's shape; returns a normalized copy (no usage). Raises LeaseError."""
    from claude_swap.settings import SELECTABLE_STRATEGIES

    if not isinstance(payload, dict):
        raise LeaseError("lease must be a JSON object")
    if payload.get("contract") != CONTRACT:
        raise LeaseError(f"unsupported lease contract (expected {CONTRACT})")

    leader = payload.get("leader")
    if not isinstance(leader, dict):
        raise LeaseError("lease.leader must be an object")
    leader_id = leader.get("id")
    if not isinstance(leader_id, str) or not leader_id or len(leader_id) > _MAX_ID_LEN:
        raise LeaseError("leader.id must be a non-empty string")
    seq = leader.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise LeaseError("leader.seq must be a non-negative integer")
    ttl_s = leader.get("ttl_s")
    if not _is_number(ttl_s) or not MIN_TTL_S <= ttl_s <= MAX_TTL_S:
        raise LeaseError(f"leader.ttl_s must be a number in [{MIN_TTL_S}, {MAX_TTL_S}]")

    target = payload.get("target")
    if not isinstance(target, dict):
        raise LeaseError("lease.target must be an object")
    mode = target.get("mode")
    if mode not in TARGET_MODES:
        raise LeaseError(f"target.mode must be one of: {', '.join(TARGET_MODES)}")
    account = target.get("account")
    strategy = target.get("strategy")
    if mode == "strategy":
        if account is not None:
            raise LeaseError("target.account must be null in strategy mode")
        if strategy not in SELECTABLE_STRATEGIES:
            raise LeaseError(
                f"target.strategy must be one of: {', '.join(SELECTABLE_STRATEGIES)}"
            )
    else:
        if strategy is not None:
            raise LeaseError(f"target.strategy must be null in {mode} mode")
        if mode == "pin" and account is None:
            raise LeaseError("target.account is required in pin mode")
        # follow with no account = the leader has no active account; stored,
        # but blocked (see receive_lease).
        if account is not None:
            account = _account_str(account)

    identity = payload.get("identity")
    if not isinstance(identity, dict):
        raise LeaseError("lease.identity must be an object")
    salt = identity.get("salt")
    slots = identity.get("slots")
    if not isinstance(salt, str) or not salt:
        raise LeaseError("identity.salt must be a non-empty string")
    if not isinstance(slots, dict) or not all(
        isinstance(k, str) and k.isascii() and k.isdigit() and isinstance(v, str)
        for k, v in slots.items()
    ):
        raise LeaseError("identity.slots must map slot numbers to hash strings")

    settings = payload.get("settings")
    if not isinstance(settings, dict):
        raise LeaseError("lease.settings must be an object")
    usage = payload.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise LeaseError("lease.usage must be an object or null")

    return {
        "contract": CONTRACT,
        "leader": {"id": leader_id, "seq": seq, "ttl_s": float(ttl_s)},
        "target": {"mode": mode, "account": account, "strategy": strategy},
        "identity": {"salt": salt, "slots": {str(int(k)): v for k, v in slots.items()}},
        "settings": settings,
    }


# -- worker side --------------------------------------------------------------


def _identity_mismatches(
    switcher: ClaudeAccountSwitcher, identity: dict
) -> list[str]:
    """Slots the leader names whose local account hashes differently (or is absent)."""
    salt = identity["salt"]
    local = _slot_identities(switcher)
    out = []
    for num, remote_hash in identity["slots"].items():
        mine = local.get(num)
        if mine is None or identity_hash(mine[0], mine[1], salt) != remote_hash:
            out.append(num)
    return sorted(out, key=int)


def _read_stored(backup_root: Path) -> dict | None:
    try:
        raw = json.loads(lease_path(backup_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def receive_lease(
    switcher: ClaudeAccountSwitcher, payload: dict, *, now: float | None = None
) -> dict:
    """Validate and store a lease, adopt its usage, return this box's snapshot.

    Order: validate (LeaseError, nothing written) → staleness check against
    the stored lease (same leader, lower seq: a late delivery from a hung ssh
    must not undo a newer one) → identity check → adopt usage with
    hold = ttl_s → write ``lease.json`` (atomic, ``received_at`` = this box's
    wall clock). A usage document the import path refuses does not block the
    lease: following matters more than one round of usage, and the reason is
    reported in the reply's ``lease.usage_error``.

    The target slot hashing differently here (or not existing) stores the
    lease with ``follow_blocked: "identity-mismatch"``; a follow lease with
    no account stores ``follow_blocked: "no-target"``.

    The reply is :func:`build_snapshot` plus a ``lease`` block:
    ``{"accepted", "seq", "follow_blocked", "identity_mismatch",
    "usage_adopted", "usage_error", "changed"}``; ``changed`` is False when a
    live stored lease already had this target, settings and follow_blocked.
    """
    from claude_swap.exceptions import TransferError
    from claude_swap.locking import FileLock
    from claude_swap.settings import atomic_write_json
    from claude_swap.transfer import import_usage_document

    lease = validate_lease(payload)
    backup_root = Path(switcher.backup_dir)
    received_at = time.time() if now is None else float(now)

    mismatches = _identity_mismatches(switcher, lease["identity"])
    target = lease["target"]
    follow_blocked: str | None = None
    if target["mode"] != "strategy":
        if target["account"] is None:
            follow_blocked = "no-target"
        elif target["account"] in mismatches or target["account"] not in lease["identity"]["slots"]:
            follow_blocked = "identity-mismatch"

    with FileLock(fleet_dir(backup_root) / ".lease.lock"):
        stored = _read_stored(backup_root)
        changed = _lease_changed(stored, target, lease["settings"], follow_blocked,
                                 received_at)
        if stored is not None:
            prev_leader = stored.get("leader") or {}
            prev_seq = prev_leader.get("seq")
            if (
                prev_leader.get("id") == lease["leader"]["id"]
                and isinstance(prev_seq, int)
                and lease["leader"]["seq"] < prev_seq
            ):
                raise LeaseError("stale lease (an older sequence number than the stored one)")

        usage_adopted: list[str] = []
        usage_error: str | None = None
        usage = payload.get("usage")
        if usage is not None:
            try:
                adopted = import_usage_document(
                    switcher, usage, hold_s=lease["leader"]["ttl_s"], quiet=True
                )
                usage_adopted = sorted(adopted, key=int)
            except TransferError:
                # Its message can name an email: report the kind only.
                usage_error = "invalid-usage-document"

        record = {
            "contract": CONTRACT,
            "leader": lease["leader"],
            "target": target,
            "settings": lease["settings"],
            "received_at": received_at,
            "follow_blocked": follow_blocked,
            "identity_mismatch": mismatches,
        }
        atomic_write_json(lease_path(backup_root), record)

    snapshot = build_snapshot(switcher, backup_root, now=received_at)
    snapshot["lease"] = {
        "accepted": True,
        "seq": lease["leader"]["seq"],
        "follow_blocked": follow_blocked,
        "identity_mismatch": mismatches,
        "usage_adopted": usage_adopted,
        "usage_error": usage_error,
        "changed": changed,
    }
    return snapshot


def _lease_changed(
    stored: dict | None, target: dict, settings: dict, follow_blocked: str | None,
    now: float,
) -> bool:
    """False when ``stored`` is a live lease with the same target, settings
    and ``follow_blocked``: a heartbeat that changes nothing the engine
    decides with (the caller then skips waking it)."""
    prev = _lease_from_record(stored) if stored is not None else None
    if prev is None or not _is_live(prev, now):
        return True
    try:
        same_settings = json.loads(json.dumps(settings, sort_keys=True)) == prev.settings
    except (TypeError, ValueError):
        return True
    return not (
        same_settings
        and prev.follow_blocked == follow_blocked
        and (prev.target_mode, prev.target_account, prev.target_strategy)
        == (target.get("mode"), target.get("account"), target.get("strategy"))
    )


def _lease_from_record(record: dict) -> Lease | None:
    try:
        leader = record["leader"]
        target = record["target"]
        ttl_s = float(leader["ttl_s"])
        received_at = float(record["received_at"])
        seq = leader["seq"]
        settings = record.get("settings")
        account = target.get("account")
        strategy = target.get("strategy")
        if (
            (account is not None and not (isinstance(account, str) and account.isdigit()))
            or (strategy is not None and not isinstance(strategy, str))
        ):
            return None
        if (
            record.get("contract") != CONTRACT
            or not isinstance(leader["id"], str)
            or not isinstance(seq, int)
            or isinstance(seq, bool)
            or not math.isfinite(ttl_s)
            or not math.isfinite(received_at)
            or not MIN_TTL_S <= ttl_s <= MAX_TTL_S
            or target["mode"] not in TARGET_MODES
            or not isinstance(settings, dict)
        ):
            return None
        blocked = record.get("follow_blocked")
        mismatch = record.get("identity_mismatch")
        mismatch = tuple(
            str(n) for n in (mismatch if isinstance(mismatch, list) else ())
            if isinstance(n, str) and n.isascii() and n.isdigit()
        )
        return Lease(
            leader_id=leader["id"],
            seq=seq,
            ttl_s=ttl_s,
            received_at=received_at,
            target_mode=target["mode"],
            target_account=account,
            target_strategy=strategy,
            settings=settings,
            follow_blocked=blocked if isinstance(blocked, str) else None,
            identity_mismatch=mismatch,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _is_live(lease: Lease, now: float) -> bool:
    return lease.received_at - _FUTURE_SLACK_S <= now < lease.received_at + lease.ttl_s


def active_lease(backup_root: Path, *, now: float | None = None) -> Lease | None:
    """The stored lease while ``received_at + ttl_s > now`` (this box's clock), else None.

    A missing, unreadable or malformed file is None, never an exception: the
    engine tick calls this and must fall back, not crash. A ``received_at``
    in this box's future (its clock stepped back) is distrusted too.
    """
    now = time.time() if now is None else now
    record = _read_stored(backup_root)
    if record is None:
        return None
    lease = _lease_from_record(record)
    if lease is None or not _is_live(lease, now):
        return None
    return lease


def forget_lease(backup_root: Path) -> bool:
    """Remove the stored lease (under the lease lock): this box leaves the
    fleet and its engine goes back to its own strategy (no lease history =
    no fallback either). True if a lease was removed."""
    from claude_swap.locking import FileLock

    path = lease_path(backup_root)
    if not path.exists():
        return False
    with FileLock(fleet_dir(backup_root) / ".lease.lock"):
        try:
            path.unlink()
        except FileNotFoundError:
            return False
    return True


def follow_state(
    backup_root: Path,
    engine_status: dict | None,
    *,
    now: float | None = None,
    active: str | int | None = None,
) -> dict:
    """``{"state": "following"|"overridden"|"autonomous", "reason",
    "lease_age_s", "on_target"}``.

    ``on_target``: whether ``active`` (this box's active account; the caller
    passes it) is the live, unblocked lease's follow/pin target. ``null``
    when the caller passed no active account.

    ``engine_status`` is an ``engine status`` reply (or None = unknown, not
    judged). With no engine, or a dry-run one, nothing acts on a lease, so
    the box is ``autonomous`` with reason ``no-engine`` / ``engine-dry-run``.
    Otherwise: no lease file → ``no-lease``; expired → ``lease-expired``;
    blocked → the block reason; a follow lease → ``following``; a pin or
    strategy lease → ``overridden`` (reason ``pin N`` / ``strategy S``).
    """
    now = time.time() if now is None else now
    record = _read_stored(backup_root)
    stored = _lease_from_record(record) if record is not None else None
    lease_age_s = round(now - stored.received_at, 1) if stored is not None else None
    on_target: bool | None = None
    if active is not None:
        on_target = bool(
            stored is not None
            and _is_live(stored, now)
            and not stored.follow_blocked
            and stored.target_mode in ("follow", "pin")
            and stored.target_account is not None
            and str(stored.target_account) == str(active)
        )

    def out(state: str, reason: str | None) -> dict:
        return {"state": state, "reason": reason, "lease_age_s": lease_age_s,
                "on_target": on_target}

    if isinstance(engine_status, dict):
        if engine_status.get("result") == "no-engine":
            return out("autonomous", "no-engine")
        engine = engine_status.get("engine")
        if isinstance(engine, dict) and engine.get("mode") == "dry-run":
            return out("autonomous", "engine-dry-run")
    if stored is None:
        return out("autonomous", "no-lease")
    if not _is_live(stored, now):
        return out("autonomous", "lease-expired")
    if stored.follow_blocked:
        return out("autonomous", stored.follow_blocked)
    if stored.target_mode == "follow":
        return out("following", None)
    if stored.target_mode == "pin":
        return out("overridden", f"pin {stored.target_account}")
    return out("overridden", f"strategy {stored.target_strategy}")


# -- snapshot -----------------------------------------------------------------


def _quarantined_slots(backup_root: Path) -> set[str]:
    """Engine-quarantined slots, read-only from the autoswitch state file."""
    from claude_swap.autoswitch import STATE_FILENAME

    try:
        state = json.loads((Path(backup_root) / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    quarantine = state.get("quarantine") if isinstance(state, dict) else None
    if not isinstance(quarantine, dict):
        return set()
    return {str(k) for k in quarantine if str(k).isascii() and str(k).isdigit()}


def _parse_iso(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def relogin_slots(list_payload: dict, backup_root: Path, *, now: float) -> list[str]:
    """Slots that need a ``/login`` soon or now, as digit strings.

    Three sources: a row with ``usageStatus == "relogin_required"``, an
    engine quarantine entry, and ``loginExpiresAt`` within 7 days (or past).
    """
    found = set(_quarantined_slots(backup_root))
    rows = list_payload.get("accounts") if isinstance(list_payload, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or "number" not in row:
            continue
        num = str(row["number"])
        if row.get("usageStatus") == "relogin_required":
            found.add(num)
        expires = _parse_iso(row.get("loginExpiresAt"))
        if expires is not None and expires - now <= RELOGIN_WINDOW_S:
            found.add(num)
    return sorted((n for n in found if n.isdigit()), key=int)


def _engine_status(backup_root: Path) -> dict:
    from claude_swap import engine_control

    try:
        return engine_control.request(
            backup_root, {"op": "status"}, reply_timeout=_ENGINE_STATUS_WAIT_S
        )
    except Exception as exc:
        return {"op": "status", "result": "error", "error": type(exc).__name__}


def build_snapshot(
    switcher: ClaudeAccountSwitcher, backup_root: Path, *, now: float | None = None
) -> dict:
    """This box's state for the leader / ``fleet status`` (one JSON object).

    ``{"contract", "host_time", "accounts": <list --json payload>,
    "engine": <engine status reply, {"result": "no-engine"} when none>,
    "follow": follow_state(...), "relogin": [slot numbers],
    "settings_hash": settings_fingerprint(...)}``. ``host_time`` is this
    box's epoch seconds.
    """
    now = time.time() if now is None else now
    backup_root = Path(backup_root)
    accounts = list_payload(switcher)
    engine = _engine_status(backup_root)
    active = accounts.get("activeAccountNumber")
    return {
        "contract": CONTRACT,
        "host_time": now,
        "accounts": accounts,
        "engine": engine,
        "follow": follow_state(backup_root, engine, now=now, active=active),
        "relogin": relogin_slots(accounts, backup_root, now=now),
        "settings_hash": settings_fingerprint(switcher),
    }


# -- settings fingerprint -----------------------------------------------------


def fingerprint_document(switcher: ClaudeAccountSwitcher) -> dict:
    """The canonical settings document :func:`settings_fingerprint` hashes.

    Included: every key of the persisted ``autoswitch`` section, as
    ``load_settings`` resolves it (clamped, defaults filled; so an explicit
    value equal to the default hashes like an unset one), keyed by its
    settings.json name; and per slot the non-default account policy
    (``threshold``, ``priority``, ``disabled``). Slots on the default policy
    are omitted, so a slot one box lacks does not count as drift unless it
    carries a policy.

    Excluded, as box-local: the ``ui`` section (theme follows each
    terminal), ``run`` (``cswap run`` defaults), ``updates`` (the PyPI
    check), ``fleet`` (each box's own fleet config, e.g. its fallback
    strategy), and per-slot aliases (display names). Emails are never in it.
    """
    from claude_swap.settings import SETTING_SPECS, load_settings

    loaded = load_settings(Path(switcher.backup_dir))
    autoswitch = {
        spec.json_key: getattr(loaded, spec.field)
        for spec in SETTING_SPECS.values()
        if spec.section == "autoswitch"
    }
    data = switcher._get_sequence_data() or {}
    accounts: dict[str, dict] = {}
    for num in data.get("sequence", []):
        snum = str(num)
        policy = switcher._policy_from_data(data, snum)
        entry: dict[str, Any] = {}
        if policy.threshold is not None:
            entry["threshold"] = float(policy.threshold)
        if policy.priority:
            entry["priority"] = int(policy.priority)
        if switcher._disabled_from_data(data, snum):
            entry["disabled"] = True
        if entry:
            accounts[snum] = entry
    return {"autoswitch": autoswitch, "accounts": accounts}


def settings_fingerprint(switcher: ClaudeAccountSwitcher) -> str:
    """sha256 hex over :func:`fingerprint_document` as canonical JSON.

    Same function on both boxes, so equal hashes mean no drift.
    """
    canonical = json.dumps(
        fingerprint_document(switcher),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
