"""Fork: ``cswap fleet`` (dev box side + the CLI). Contract ``cswap-fleet/1``.

The dev box keeps a list of worker hosts (``<backup_root>/fleet/hosts.json``)
and talks to each over ``ssh`` with a FIXED command line: the payload (a lease
or a settings document) only ever travels on stdin as JSON, never through the
remote shell. The worker-side verbs (``receive``, ``snapshot``, ``apply``)
read stdin and print exactly one JSON object.

``forget`` (worker, human or ``--json``) drops the stored lease so a box that
left the fleet runs its own persisted strategy again.

The lease format and the worker snapshot live in ``fleet_lease``; the leader
thread lives in ``fleet_leader``. This module never moves credentials between
boxes and never prints an email (accounts are slot numbers). See docs/fleet.md.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import logging
import re
import secrets
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from claude_swap.fleet_lease import (  # noqa: F401 (LEASE_FILENAME re-exported)
    FLEET_DIRNAME,
    HOSTS_FILENAME,
    LEASE_FILENAME,
)

CONTRACT = "cswap-fleet/1"
DEFAULT_TIMEOUT = 20.0
LOCAL_HOST_NAME = "local"
MAX_WORKERS = 4
_MAX_STDIN_BYTES = 4 * 1024 * 1024

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

#: settings.json sections a push mirrors onto workers: exactly the key set
#: ``fleet_lease.fingerprint_document`` hashes, so push-then-status shows no
#: drift. ``ui``, ``run``, ``updates`` and ``fleet`` are box-local (theme,
#: history sharing, update hint, each box's own fallback) and never pushed.
PUSHED_SECTIONS = ("autoswitch",)

OVERRIDE_MODES = ("follow", "pin", "strategy")
DEFAULT_OVERRIDE = {"mode": "follow", "account": None, "strategy": None}

# An ssh alias / host name: no whitespace, no shell metacharacters, and never
# a leading "-" (ssh would read it as an option).
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:\-]{0,127}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@\-]{0,63}$")
# One token of the remote cswap command (after shlex.split): a path or a word.
_COMMAND_TOKEN_RE = re.compile(r"^[A-Za-z0-9_./~:=+@%,\\\-]+$")
_EMAIL_RE = re.compile(r"[^\s@<>()\"']+@[^\s@<>()\"']+\.[^\s@<>()\"']+")
#: Account-row fields that reach the ``fleet status`` contract (allowlist: no
#: email, org name/uuid or alias ever leaves the box through it).
_ROW_KEEP = (
    "number", "active", "isOrganization", "usageStatus", "usage",
    "usageFetchedAt", "usageAgeSeconds", "lastGoodUsage", "lastGoodFetchedAt",
    "lastGoodAgeSeconds", "usageError", "usageRetryAt", "disabled", "threshold",
    "loginExpiresAt",
)

_logger = logging.getLogger("claude-swap")


class FleetError(Exception):
    """A handled fleet failure; ``code`` is the contract error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _lease():
    """``fleet_lease``, imported late (and swappable in tests)."""
    return importlib.import_module("claude_swap.fleet_lease")


def _scrub(text: object) -> str:
    return _EMAIL_RE.sub("<email>", str(text or ""))


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# --------------------------------------------------------------------------
# Hosts config
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Host:
    name: str
    alias: str
    command: str = "cswap"
    override: dict = field(default_factory=lambda: dict(DEFAULT_OVERRIDE))

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "alias": self.alias,
            "command": self.command,
            "override": dict(self.override),
        }


def validate_alias(alias: str) -> str:
    if not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias):
        raise FleetError(
            "invalid-host",
            f"invalid ssh alias {alias!r}: letters, digits and ._@:- only, "
            "no whitespace or shell characters, not starting with '-'",
        )
    return alias


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name) or name == LOCAL_HOST_NAME:
        raise FleetError(
            "invalid-host",
            f"invalid host name {name!r}: letters, digits and ._@- only "
            f"('{LOCAL_HOST_NAME}' is reserved for the dev box)",
        )
    return name


def validate_command(command: str) -> str:
    """The remote cswap command: shlex-split, each token a plain word/path."""
    try:
        tokens = shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        tokens = []
    if not tokens or not all(_COMMAND_TOKEN_RE.fullmatch(t) for t in tokens):
        raise FleetError(
            "invalid-host",
            f"invalid remote command {command!r}: plain words and paths only "
            "(no quotes, spaces inside paths, or shell characters)",
        )
    return " ".join(tokens)


def normalize_override(override: object) -> dict:
    """A validated override dict; raises FleetError on a bad one."""
    from claude_swap.settings import SELECTABLE_STRATEGIES

    if override is None:
        return dict(DEFAULT_OVERRIDE)
    if not isinstance(override, dict):
        raise FleetError("invalid-override", "override must be an object")
    mode = override.get("mode", "follow")
    if mode == "follow":
        return dict(DEFAULT_OVERRIDE)
    if mode == "pin":
        account = str(override.get("account") or "")
        if not (account.isascii() and account.isdigit()) or int(account) < 1:
            raise FleetError("invalid-override", f"pin needs an account number, got {account!r}")
        return {"mode": "pin", "account": str(int(account)), "strategy": None}
    if mode == "strategy":
        strategy = override.get("strategy")
        if strategy not in SELECTABLE_STRATEGIES:
            raise FleetError(
                "invalid-override",
                f"strategy must be one of: {', '.join(SELECTABLE_STRATEGIES)}",
            )
        return {"mode": "strategy", "account": None, "strategy": strategy}
    raise FleetError("invalid-override", f"unknown override mode {mode!r}")


def hosts_path(backup_root: Path) -> Path:
    return Path(backup_root) / FLEET_DIRNAME / HOSTS_FILENAME


def _host_from_json(entry: object) -> Host:
    if not isinstance(entry, dict):
        raise FleetError("invalid-hosts", "host entry is not an object")
    alias = validate_alias(entry.get("alias"))
    return Host(
        name=validate_name(entry.get("name") or alias),
        alias=alias,
        command=validate_command(entry.get("command") or "cswap"),
        override=normalize_override(entry.get("override")),
    )


def load_hosts(backup_root: Path, *, strict: bool = False) -> list[Host]:
    """Configured workers. Missing file → []. Lenient by default (a bad entry
    is skipped, a corrupt file reads as empty) so the leader never crashes on
    a hand edit; ``strict=True`` (the write path) raises instead."""
    path = hosts_path(backup_root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        if strict:
            raise FleetError(
                "invalid-hosts", f"{path} is unreadable ({type(e).__name__}); fix or delete it"
            ) from None
        _logger.warning("Could not read %s (%s); no fleet hosts", path, type(e).__name__)
        return []
    entries = raw.get("hosts") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        if strict:
            raise FleetError("invalid-hosts", f"{path} has no hosts list; fix or delete it")
        return []
    hosts: list[Host] = []
    seen: set[str] = set()
    for entry in entries:
        try:
            host = _host_from_json(entry)
        except FleetError:
            if strict:
                raise
            _logger.warning("Skipping an invalid entry in %s", path)
            continue
        if host.name in seen:
            continue
        seen.add(host.name)
        hosts.append(host)
    return hosts


def save_hosts(backup_root: Path, hosts: list[Host]) -> None:
    from claude_swap.settings import atomic_write_json

    atomic_write_json(
        hosts_path(backup_root),
        {"contract": CONTRACT, "hosts": [h.to_json() for h in hosts]},
    )


@contextlib.contextmanager
def _hosts_lock(backup_root: Path):
    """Serialize hosts.json read-modify-write (TUI and CLI may race); the
    same FileLock helper ``fleet_lease.receive_lease`` uses."""
    from claude_swap.exceptions import LockError
    from claude_swap.locking import FileLock

    try:
        with FileLock(hosts_path(backup_root).parent / ".hosts.lock"):
            yield
    except LockError:
        raise FleetError("busy", "hosts.json is locked by another cswap; retry") from None


def find_host(hosts: list[Host], name: str) -> Host:
    for host in hosts:
        if host.name == name:
            return host
    for host in hosts:
        if host.alias == name:
            return host
    raise FleetError("unknown-host", f"no fleet host named {name!r}")


def add_host(
    backup_root: Path, alias: str, *, name: str | None = None, command: str = "cswap"
) -> Host:
    host = Host(
        name=validate_name(name or alias),
        alias=validate_alias(alias),
        command=validate_command(command),
    )
    with _hosts_lock(backup_root):
        hosts = load_hosts(backup_root, strict=True)
        if any(h.name == host.name for h in hosts):
            raise FleetError("duplicate-host", f"a fleet host named {host.name!r} already exists")
        save_hosts(backup_root, [*hosts, host])
    return host


def remove_host(backup_root: Path, name: str) -> Host:
    with _hosts_lock(backup_root):
        hosts = load_hosts(backup_root, strict=True)
        host = find_host(hosts, name)
        save_hosts(backup_root, [h for h in hosts if h is not host])
    return host


def set_override(backup_root: Path, name: str, override: dict) -> Host:
    """Persist one worker's override (``follow`` | ``pin`` | ``strategy``)."""
    normalized = normalize_override(override)
    with _hosts_lock(backup_root):
        hosts = load_hosts(backup_root, strict=True)
        host = find_host(hosts, name)
        updated = Host(host.name, host.alias, host.command, normalized)
        save_hosts(backup_root, [updated if h is host else h for h in hosts])
    return updated


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteResult:
    rc: int | None
    payload: dict | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None


def ssh_argv(host: Host, args: list[str]) -> list[str]:
    return [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host.alias,
        *shlex.split(host.command),
        *(shlex.quote(str(a)) for a in args),
    ]


def _parse_json_object(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    candidates = [text]
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) > 1:
        candidates.append(lines[-1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _stderr_tail(stderr: object) -> str:
    lines = [ln.strip() for ln in str(stderr or "").splitlines() if ln.strip()]
    return _scrub(lines[-1])[:200] if lines else ""


def run_remote(
    host: Host,
    args: list[str],
    stdin_obj: dict | None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    runner: Callable[..., Any] = subprocess.run,
) -> RemoteResult:
    """One ssh round trip; never raises. The payload goes only on stdin."""
    argv = ssh_argv(host, args)
    kwargs: dict = {
        "input": json.dumps(stdin_obj) if stdin_obj is not None else "",
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = runner(argv, **kwargs)
    except subprocess.TimeoutExpired:
        return RemoteResult(None, None, f"timeout after {timeout:g}s")
    except FileNotFoundError:
        return RemoteResult(None, None, "ssh not found on PATH")
    except Exception as e:  # never raise into the leader thread or the CLI
        return RemoteResult(None, None, f"ssh failed to start ({type(e).__name__})")
    rc = getattr(proc, "returncode", None)
    payload = _parse_json_object(getattr(proc, "stdout", "") or "")
    if rc == 255:
        tail = _stderr_tail(getattr(proc, "stderr", ""))
        return RemoteResult(rc, None, f"ssh error (255){': ' + tail if tail else ''}")
    if rc != 0:
        detail = ""
        if payload is not None and payload.get("message"):
            detail = _scrub(payload.get("message"))[:200]
        else:
            detail = _stderr_tail(getattr(proc, "stderr", ""))
        return RemoteResult(rc, payload, f"remote exit {rc}{': ' + detail if detail else ''}")
    if payload is None:
        return RemoteResult(rc, None, "remote reply was not a JSON object")
    return RemoteResult(rc, payload, None)


def _pool_map(fn: Callable[[Host], Any], hosts: list[Host]) -> list:
    if not hosts:
        return []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(hosts))) as pool:
        return list(pool.map(fn, hosts))


# --------------------------------------------------------------------------
# Settings push (export on the dev box, apply on the worker)
# --------------------------------------------------------------------------


def _slot_identity(record: dict, salt: str) -> str:
    return _lease().identity_hash(
        str(record.get("email", "") or ""),
        (record.get("organizationUuid") or None),
        salt,
    )


def export_settings(switcher) -> dict:
    """The dev box's settings document for ``fleet push``.

    Built from ``fleet_lease.fingerprint_document`` (the same key set the
    drift fingerprint hashes): every ``autoswitch`` key at its effective value
    (``None`` = the default), and every managed slot's full policy (defaults
    included, so a worker's stray override is cleared) under a salted identity
    hash instead of any email."""
    lease = _lease()
    fp = lease.fingerprint_document(switcher)
    settings = {
        f"autoswitch.{json_key}": value
        for json_key, value in (fp.get("autoswitch") or {}).items()
    }
    policies = fp.get("accounts") or {}
    salt = secrets.token_hex(16)
    data = switcher._get_sequence_data() or {}
    accounts: dict = {}
    for num in data.get("sequence", []):
        num = str(num)
        record = data.get("accounts", {}).get(num)
        if not record:
            continue
        policy = policies.get(num) or {}
        accounts[num] = {
            "identity": _slot_identity(record, salt),
            "threshold": policy.get("threshold"),
            "priority": int(policy.get("priority") or 0),
            "disabled": bool(policy.get("disabled")),
        }
    return {
        "contract": CONTRACT,
        "kind": "settings",
        "salt": salt,
        "settings": settings,
        "accounts": accounts,
    }


def _validate_settings_doc(doc: object) -> dict:
    if not isinstance(doc, dict):
        raise FleetError("invalid-document", "settings document must be a JSON object")
    if doc.get("contract") != CONTRACT or doc.get("kind") != "settings":
        raise FleetError("invalid-document", f"not a {CONTRACT} settings document")
    if not isinstance(doc.get("salt"), str) or not doc["salt"]:
        raise FleetError("invalid-document", "settings document has no salt")
    if not isinstance(doc.get("settings", {}), dict):
        raise FleetError("invalid-document", "settings must be an object")
    accounts = doc.get("accounts", {})
    if not isinstance(accounts, dict) or not all(
        isinstance(k, str) and k.isascii() and k.isdigit() and isinstance(v, dict)
        for k, v in accounts.items()
    ):
        raise FleetError("invalid-document", "accounts must map slot numbers to objects")
    return doc


def apply_settings(switcher, doc: dict, *, dry_run: bool = False) -> dict:
    """Worker side of ``fleet push``: mirror the dev box's settings + policy.

    Everything is validated before the first write; a slot whose identity
    differs (or is missing here) is skipped, never written. ``dry_run``
    reports the same changes and writes nothing."""
    from claude_swap.models import normalize_account_priority, normalize_account_threshold
    from claude_swap.exceptions import ConfigError
    from claude_swap.settings import (
        SETTING_SPECS,
        effective_settings,
        format_setting_value,
        parse_setting_value,
        set_setting,
        unset_setting,
    )

    _validate_settings_doc(doc)
    root = Path(switcher.backup_dir)
    current = {spec.dotted: (value, is_set) for spec, value, is_set in effective_settings(root)}

    setting_changes: list[dict] = []
    ignored: list[str] = []
    planned_sets: list[tuple[str, str]] = []
    planned_unsets: list[str] = []
    for key, value in doc.get("settings", {}).items():
        spec = SETTING_SPECS.get(key)
        if spec is None or spec.section not in PUSHED_SECTIONS:
            ignored.append(str(key))
            continue
        old, is_set = current[key]
        if value is None or value == spec.default:
            # Target is the default: clear an explicit value, never write one.
            # An explicit value equal to the default is left alone: it hashes
            # like an unset one in the drift fingerprint.
            if is_set and old != spec.default:
                planned_unsets.append(key)
                setting_changes.append({"key": key, "from": old, "to": spec.default})
            continue
        raw = format_setting_value(value)
        try:
            parsed = parse_setting_value(spec, raw)
        except ConfigError as e:
            raise FleetError("invalid-document", str(e)) from None
        if old == parsed:
            continue
        planned_sets.append((key, raw))
        setting_changes.append({"key": key, "from": old, "to": parsed})

    data = switcher._get_sequence_data() or {}
    records = data.get("accounts", {})
    account_changes: list[dict] = []
    skipped: list[dict] = []
    planned_policy: list[tuple[str, str, object]] = []
    salt = doc["salt"]
    for num in sorted(doc.get("accounts", {}), key=int):
        wanted = doc["accounts"][num]
        num = str(int(num))
        record = records.get(num)
        if not record:
            skipped.append({"account": num, "reason": "missing"})
            continue
        if _slot_identity(record, salt) != wanted.get("identity"):
            skipped.append({"account": num, "reason": "identity-mismatch"})
            continue
        policy = switcher._policy_from_data(data, num)
        try:
            threshold = wanted.get("threshold")
            threshold = None if threshold is None else normalize_account_threshold(threshold)
            priority = normalize_account_priority(wanted.get("priority") or 0)
        except ValueError as e:
            raise FleetError("invalid-document", f"account {num}: {e}") from None
        disabled = bool(wanted.get("disabled"))
        for fld, old, new in (
            ("threshold", policy.threshold, threshold),
            ("priority", policy.priority, priority),
            ("disabled", switcher._disabled_from_data(data, num), disabled),
        ):
            if old != new:
                planned_policy.append((num, fld, new))
                account_changes.append({"account": num, "field": fld, "from": old, "to": new})

    if not dry_run:
        # The switcher setters print human confirmations (with emails); this
        # verb's stdout is one JSON object, so swallow them.
        with contextlib.redirect_stdout(io.StringIO()):
            for key, raw in planned_sets:
                set_setting(root, key, raw)
            for key in planned_unsets:
                unset_setting(root, key)
            for num, fld, new in planned_policy:
                if fld == "threshold":
                    switcher.set_account_threshold(num, new)
                elif fld == "priority":
                    switcher.set_account_priority(num, new)
                else:
                    switcher.set_account_disabled(num, new)

    return {
        "contract": CONTRACT,
        "op": "apply",
        "result": "dry-run" if dry_run else "applied",
        "dry_run": dry_run,
        "changed": bool(setting_changes or account_changes),
        "settings": setting_changes,
        "accounts": account_changes,
        "skipped": skipped,
        "ignored": ignored,
    }


def push(
    switcher,
    hosts: list[Host],
    *,
    dry_run: bool,
    runner: Callable[..., Any] = subprocess.run,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict]:
    """Send the dev box's settings document to each host's ``fleet apply``."""
    doc = export_settings(switcher)
    args = ["fleet", "apply", "--json"] + (["--dry-run"] if dry_run else [])

    def one(host: Host) -> dict:
        res = run_remote(host, args, doc, timeout=timeout, runner=runner)
        out = {"name": host.name, "ok": res.ok, "error": res.error}
        reply = res.payload or {}
        for key in ("changed", "settings", "accounts", "skipped", "ignored"):
            if key in reply:
                out[key] = reply[key]
        return out

    return _pool_map(one, hosts)


# --------------------------------------------------------------------------
# Status (the router contract)
# --------------------------------------------------------------------------


def _scrub_value(value):
    if isinstance(value, str):
        return _scrub(value)
    if isinstance(value, list):
        return [_scrub_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_value(v) for k, v in value.items()}
    return value


def _account_rows(accounts_payload: object) -> list[dict]:
    rows = accounts_payload.get("accounts") if isinstance(accounts_payload, dict) else None
    if not isinstance(rows, list):
        return []
    return [
        _scrub_value({
            k: (str(v) if k == "number" and v is not None else v)
            for k, v in row.items()
            if k in _ROW_KEEP
        })
        for row in rows
        if isinstance(row, dict)
    ]


def _engine_summary(reply: object) -> dict:
    if not isinstance(reply, dict):
        return {"running": False, "result": None}
    result = reply.get("result")
    out = {"running": result == "running", "result": result}
    engine = reply.get("engine")
    if isinstance(engine, dict):
        out.update(_scrub_value(engine))
    return out


def _active_from(snapshot: dict) -> str | None:
    accounts = snapshot.get("accounts")
    if isinstance(accounts, dict) and accounts.get("activeAccountNumber") is not None:
        return str(accounts["activeAccountNumber"])
    engine = (snapshot.get("engine") or {}).get("engine") if isinstance(snapshot.get("engine"), dict) else None
    if isinstance(engine, dict) and engine.get("active") is not None:
        return str(engine["active"])
    return None


def _host_entry(
    *,
    name: str,
    alias: str | None,
    local: bool,
    source: str,
    reachable: bool,
    error: str | None,
    snapshot: dict | None,
    override: dict | None,
    local_hash: str | None,
    last_ok_at: float | None = None,
) -> dict:
    entry = {
        "name": name,
        "alias": alias,
        "local": local,
        "source": source,
        "reachable": reachable,
        "error": _scrub(error) if error else None,
        "state": "unknown",
        "reason": None,
        "on_target": None,
        "lease_age_s": None,
        "active": None,
        "engine": None,
        "accounts": [],
        "relogin": [],
        "settings_hash": None,
        "drift": None,
        "override": override,
        "last_ok_at": _iso(last_ok_at) if isinstance(last_ok_at, (int, float)) else last_ok_at,
    }
    if isinstance(snapshot, dict):
        follow = snapshot.get("follow") if isinstance(snapshot.get("follow"), dict) else {}
        settings_hash = snapshot.get("settings_hash")
        on_target = follow.get("on_target")
        entry.update(
            on_target=on_target if isinstance(on_target, bool) else None,
            state=follow.get("state") or "unknown",
            reason=_scrub(follow.get("reason")) if follow.get("reason") else None,
            lease_age_s=follow.get("lease_age_s"),
            active=_active_from(snapshot),
            engine=_engine_summary(snapshot.get("engine")),
            accounts=_account_rows(snapshot.get("accounts")),
            relogin=[str(n) for n in snapshot.get("relogin") or []],
            settings_hash=settings_hash,
            drift=(
                None if not (settings_hash and local_hash) else settings_hash != local_hash
            ),
        )
    return entry


def fleet_status(
    switcher,
    backup_root: Path,
    *,
    leader_snapshot: dict | None,
    runner: Callable[..., Any] = subprocess.run,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Every host (the dev box first) in one ``cswap-fleet/1`` document.

    With a live leader, its last per-host snapshots are used; otherwise (or
    for a host the leader has not reported yet) one ``fleet snapshot`` pull
    per host. An unreachable host is data (``reachable: false``)."""
    lease = _lease()
    hosts = load_hosts(backup_root)
    try:
        local_hash = lease.settings_fingerprint(switcher)
    except Exception:
        local_hash = None
    leader_live = isinstance(leader_snapshot, dict)
    leader_hosts = (leader_snapshot or {}).get("hosts") if leader_live else None
    if not isinstance(leader_hosts, dict):
        leader_hosts = {}

    try:
        local_snap, local_err = lease.build_snapshot(switcher, backup_root), None
    except Exception as e:
        local_snap, local_err = None, f"local snapshot failed ({type(e).__name__})"
    local = _host_entry(
        name=LOCAL_HOST_NAME, alias=None, local=True, source="local",
        reachable=local_snap is not None, error=local_err, snapshot=local_snap,
        override=None, local_hash=local_hash,
    )
    if local_snap is not None:
        local["drift"] = False
        if leader_live:
            local["state"], local["reason"] = "leading", None

    def one(host: Host) -> dict:
        reported = leader_hosts.get(host.name)
        if isinstance(reported, dict):
            snap = reported.get("snapshot")
            return _host_entry(
                name=host.name, alias=host.alias, local=False, source="leader",
                reachable=bool(reported.get("reachable")) and isinstance(snap, dict),
                error=reported.get("error"), snapshot=snap if isinstance(snap, dict) else None,
                override=dict(host.override), local_hash=local_hash,
                last_ok_at=reported.get("last_ok_at"),
            )
        res = run_remote(host, ["fleet", "snapshot", "--json"], None, timeout=timeout, runner=runner)
        return _host_entry(
            name=host.name, alias=host.alias, local=False, source="pull",
            reachable=res.ok, error=res.error, snapshot=res.payload if res.ok else None,
            override=dict(host.override), local_hash=local_hash,
        )

    leader_block = {"live": leader_live}
    if leader_live:
        leader_block["leader_id"] = leader_snapshot.get("leader_id")
    return {
        "contract": CONTRACT,
        "generated_at": _iso(datetime.now(timezone.utc).timestamp()),
        "leader": leader_block,
        "hosts": [local, *_pool_map(one, hosts)],
    }


def _leader_snapshot(backup_root: Path) -> dict | None:
    """The live engine's leader snapshot (``engine status`` → ``fleet``)."""
    try:
        from claude_swap import engine_control

        reply = engine_control.request(backup_root, {"op": "status"}, reply_timeout=10.0)
    except Exception:
        return None
    if not isinstance(reply, dict) or reply.get("result") != "running":
        return None
    fleet = reply.get("fleet")
    if not isinstance(fleet, dict):
        engine = reply.get("engine")
        fleet = engine.get("fleet") if isinstance(engine, dict) else None
    if not isinstance(fleet, dict) or fleet.get("role") != "leader":
        return None
    leader = fleet.get("leader")
    return leader if isinstance(leader, dict) else None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _payload(op: str, result: str, **fields) -> dict:
    return {"contract": CONTRACT, "op": op, "result": result, **fields}


def _error(op: str, code: str, message: str) -> dict:
    return _payload(op, "error", error=code, message=_scrub(message))


def _account_number(value: str) -> str:
    if not (value.isascii() and value.isdigit()) or int(value) < 1:
        raise argparse.ArgumentTypeError(f"invalid account number: {value!r}")
    return str(int(value))


def build_parser() -> argparse.ArgumentParser:
    from claude_swap.settings import SELECTABLE_STRATEGIES

    parser = argparse.ArgumentParser(
        prog="cswap fleet",
        description=(
            "Worker boxes follow this dev box over ssh. Contract "
            f"{CONTRACT}; see docs/fleet.md."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exit codes:
  0  done (an unreachable host in status is data, not an error)
  1  error (bad hosts file, unknown host, a push that failed on any host, ...)
  2  usage error
        """,
    )
    parser.add_argument("--json", action="store_true", help="Print one JSON object")
    sub = parser.add_subparsers(
        dest="action",
        metavar="{hosts,status,push,override,forget,receive,snapshot,apply}",
    )
    sub.required = True

    p_hosts = sub.add_parser("hosts", help="Manage worker hosts (dev box)")
    hsub = p_hosts.add_subparsers(dest="hosts_action", metavar="{add,remove,list}")
    hsub.required = True
    p_add = hsub.add_parser("add", help="Add a worker by ssh alias")
    p_add.add_argument("alias", metavar="ALIAS", help="ssh alias (from ~/.ssh/config)")
    p_add.add_argument("--name", help="Display name (default: the alias)")
    p_add.add_argument(
        "--command", default="cswap", help="cswap command on the worker (default: cswap)"
    )
    p_remove = hsub.add_parser("remove", help="Remove a worker")
    p_remove.add_argument("name", metavar="NAME")
    p_list = hsub.add_parser("list", help="List workers")

    p_status = sub.add_parser("status", help="Every host in one view (the router contract)")
    p_push = sub.add_parser("push", help="Write this box's settings + policy to workers")
    p_push.add_argument("--host", metavar="NAME", help="Only this worker")
    p_push.add_argument("--dry-run", action="store_true", help="Report, write nothing")

    p_override = sub.add_parser(
        "override", help="Per-worker override: follow | pin N | strategy S"
    )
    p_override.add_argument("host", metavar="HOST")
    p_override.add_argument("mode", choices=OVERRIDE_MODES)
    p_override.add_argument("value", nargs="?", metavar="N|S")

    p_forget = sub.add_parser(
        "forget",
        help="(worker) drop the stored lease: back to this box's own strategy",
    )
    p_receive = sub.add_parser("receive", help="(worker) lease on stdin → snapshot")
    p_snapshot = sub.add_parser("snapshot", help="(worker) print this box's snapshot")
    p_apply = sub.add_parser("apply", help="(worker) settings document on stdin")
    p_apply.add_argument("source", nargs="?", default="-", choices=["-"], help="stdin")
    p_apply.add_argument("--dry-run", action="store_true", help="Report, write nothing")

    for p in (p_add, p_remove, p_list, p_status, p_push, p_override, p_forget,
              p_receive, p_snapshot, p_apply):
        p.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                       help="Print one JSON object")
    parser.set_defaults(_strategies=SELECTABLE_STRATEGIES)
    return parser


def _read_stdin_json(stream) -> dict:
    text = stream.read(_MAX_STDIN_BYTES + 1)
    if len(text) > _MAX_STDIN_BYTES:
        raise FleetError("invalid-input", "stdin document too large")
    try:
        value = json.loads(text)
    except ValueError:
        raise FleetError("invalid-input", "stdin is not JSON") from None
    if not isinstance(value, dict):
        raise FleetError("invalid-input", "stdin must hold a JSON object")
    return value


def _wake_engine(backup_root: Path) -> bool:
    """Best effort: nudge this box's running engine to tick now (a new or
    dropped lease then takes effect within seconds). Never raises."""
    try:
        from claude_swap import engine_control

        wake = getattr(engine_control, "wake_engine", None)
        return bool(wake(Path(backup_root))) if callable(wake) else False
    except Exception:
        return False


def _make_switcher():
    from claude_swap.switcher import ClaudeAccountSwitcher

    return ClaudeAccountSwitcher()


def _status_human(doc: dict) -> str:
    lines = []
    for h in doc.get("hosts", []):
        if not h.get("reachable"):
            lines.append(f"{h['name']}: unreachable ({h.get('error') or 'no reply'})")
            continue
        parts = [f"{h['name']}: {h.get('state')}"]
        if h.get("reason"):
            parts[0] += f" ({h['reason']})"
        parts.append(f"active #{h['active']}" if h.get("active") else "no active account")
        engine = h.get("engine") or {}
        parts.append("engine running" if engine.get("running") else "no engine")
        if (
            h.get("on_target") is False
            and h.get("state") in ("following", "overridden")
            and not str(h.get("reason") or "").startswith("strategy ")
        ):
            parts.append("not on target yet")
        if h.get("lease_age_s") is not None:
            parts.append(f"lease {h['lease_age_s']:.0f}s old")
        if h.get("relogin"):
            parts.append("re-login " + ", ".join(f"#{n}" for n in h["relogin"]))
        if h.get("drift"):
            parts.append("settings drift")
        override = h.get("override") or {}
        if override.get("mode") == "pin":
            parts.append(f"pinned #{override.get('account')}")
        elif override.get("mode") == "strategy":
            parts.append(f"strategy {override.get('strategy')}")
        lines.append(" · ".join(parts))
    leader = doc.get("leader") or {}
    lines.append("leader: live" if leader.get("live") else "leader: not running (one-shot pull)")
    return "\n".join(lines)


def run(
    argv: list[str],
    *,
    backup_root: Path | None = None,
    switcher=None,
    runner: Callable[..., Any] = subprocess.run,
    stdin=None,
) -> int:
    """Parse, act, print; returns the exit code (argparse exits 2)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    json_mode = bool(getattr(args, "json", False))
    action = args.action
    op = f"hosts-{args.hosts_action}" if action == "hosts" else action
    # The worker verbs are machine verbs: always one JSON object on stdout.
    if action in ("receive", "snapshot", "apply"):
        json_mode = True

    override = None
    if action == "override":
        if args.mode == "follow":
            if args.value is not None:
                parser.error("override follow takes no value")
            override = {"mode": "follow"}
        elif args.mode == "pin":
            try:
                override = {"mode": "pin", "account": _account_number(args.value or "")}
            except argparse.ArgumentTypeError as e:
                parser.error(str(e))
        else:
            if args.value not in args._strategies:
                parser.error(f"strategy must be one of: {', '.join(args._strategies)}")
            override = {"mode": "strategy", "strategy": args.value}

    def lazy_switcher():
        return switcher if switcher is not None else _make_switcher()

    def root() -> Path:
        if backup_root is not None:
            return Path(backup_root)
        from claude_swap.paths import get_backup_root

        return get_backup_root()

    human = None
    try:
        if action == "hosts":
            if args.hosts_action == "add":
                host = add_host(root(), args.alias, name=args.name, command=args.command)
                payload = _payload(op, "ok", host=host.to_json())
                human = f"added {host.name} (ssh {host.alias}, command {host.command})"
            elif args.hosts_action == "remove":
                host = remove_host(root(), args.name)
                payload = _payload(op, "ok", host=host.to_json())
                human = f"removed {host.name}"
            else:
                hosts = load_hosts(root(), strict=True)
                payload = _payload(op, "ok", hosts=[h.to_json() for h in hosts])
                human = "\n".join(
                    f"{h.name}  ssh {h.alias}  {h.command}  {h.override.get('mode')}"
                    + (f" #{h.override['account']}" if h.override.get("account") else "")
                    + (f" {h.override['strategy']}" if h.override.get("strategy") else "")
                    for h in hosts
                ) or "no fleet hosts configured"
        elif action == "override":
            host = set_override(root(), args.host, override)
            payload = _payload(op, "ok", host=host.to_json())
            human = f"{host.name}: override {host.override['mode']}" + (
                f" #{host.override['account']}" if host.override.get("account") else ""
            ) + (f" {host.override['strategy']}" if host.override.get("strategy") else "")
        elif action == "status":
            sw = lazy_switcher()
            r = Path(sw.backup_dir) if backup_root is None else root()
            payload = fleet_status(
                sw, r, leader_snapshot=_leader_snapshot(r), runner=runner
            )
            human = _status_human(payload)
        elif action == "push":
            sw = lazy_switcher()
            r = Path(sw.backup_dir) if backup_root is None else root()
            hosts = load_hosts(r, strict=True)
            if args.host:
                hosts = [find_host(hosts, args.host)]
            if not hosts:
                raise FleetError("no-hosts", "no fleet hosts configured")
            results = push(sw, hosts, dry_run=args.dry_run, runner=runner)
            failed = [r_ for r_ in results if not r_["ok"]]
            payload = _payload(
                op, "error" if failed else ("dry-run" if args.dry_run else "pushed"),
                dry_run=args.dry_run, hosts=results,
            )
            if failed:
                payload.update(error="push-failed", message=f"{len(failed)} host(s) failed")
            human = "\n".join(
                f"{r_['name']}: "
                + (f"failed ({r_['error']})" if not r_["ok"] else
                   ("no changes" if not r_.get("changed") else
                    f"{len(r_.get('settings', []))} setting(s), "
                    f"{len(r_.get('accounts', []))} policy change(s)"
                    + (" (dry run)" if args.dry_run else "")))
                + (f"; skipped {', '.join('#' + s['account'] + ' ' + s['reason'] for s in r_.get('skipped', []))}"
                   if r_.get("skipped") else "")
                for r_ in results
            )
        elif action == "receive":
            data = _read_stdin_json(stdin or sys.stdin)
            sw = lazy_switcher()
            try:
                payload = _lease().receive_lease(sw, data)
            except (ValueError, KeyError, TypeError) as e:
                raise FleetError("invalid-lease", str(e)) from None
            woken = _wake_engine(Path(sw.backup_dir))
            if isinstance(payload, dict):
                block = payload.get("lease")
                if not isinstance(block, dict):
                    block = payload["lease"] = {}
                block["engine_woken"] = woken
        elif action == "forget":
            forget = getattr(_lease(), "forget_lease", None)
            if not callable(forget):
                raise FleetError("unsupported", "this cswap cannot forget a lease")
            r = Path(switcher.backup_dir) if backup_root is None and switcher is not None else root()
            forgot = bool(forget(r))
            woken = _wake_engine(r) if forgot else False
            payload = _payload(op, "forgotten" if forgot else "no-lease",
                               forgot=forgot, engine_woken=woken)
            human = (
                "lease dropped: this box is back on its own strategy"
                if forgot else "no lease stored: this box already runs its own strategy"
            )
        elif action == "snapshot":
            sw = lazy_switcher()
            payload = _lease().build_snapshot(sw, Path(sw.backup_dir))
        else:  # apply
            data = _read_stdin_json(stdin or sys.stdin)
            payload = apply_settings(lazy_switcher(), data, dry_run=args.dry_run)
    except FleetError as e:
        payload = _error(op, e.code, str(e))
    except Exception as e:  # handled envelope; never a traceback on the wire
        _logger.debug("cswap fleet %s failed", op, exc_info=True)
        payload = _error(op, "internal", f"{type(e).__name__}: {e}")

    is_error = isinstance(payload, dict) and payload.get("result") == "error"
    if json_mode:
        print(json.dumps(payload), flush=True)
    elif is_error:
        print(f"error ({payload.get('error')}): {payload.get('message', '')}", flush=True)
    else:
        print(human if human is not None else json.dumps(payload), flush=True)
    return EXIT_ERROR if is_error else EXIT_OK


def main(argv: list[str]) -> None:
    """``cswap fleet ...`` entry (the cli.py dispatch)."""
    sys.exit(run(argv))
