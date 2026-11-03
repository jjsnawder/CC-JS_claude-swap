"""Scratch-isolation guard for manual cswap trials. Import it FIRST.

Points every home/config lookup at a scratch home, then blocks what a trial must
never reach: the real account store and ~/.claude (audit hook), the OS
credential store (keyring), the network (urlopen) and subprocesses/exec (so no
real `claude` launches). Blocked attempts are logged to <scratch>/blocked.log.

Env overrides:
  CSWAP_TRIAL_SRC   src/ dir to import claude_swap from (default: this repo's src)
  CSWAP_TRIAL_HOME  scratch home (default: <repo>/.tmp/trial/home)
"""
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = pathlib.Path(os.environ.get("CSWAP_TRIAL_SRC") or REPO / "src").resolve()
HOME = pathlib.Path(os.environ.get("CSWAP_TRIAL_HOME") or REPO / ".tmp" / "trial" / "home").resolve()
LOG = HOME.parent / "blocked.log"

_REAL_HOME = os.path.normcase(os.path.expanduser("~"))
assert os.path.normcase(str(HOME)) != _REAL_HOME, "scratch home must not be the real home"
_FORBIDDEN = [os.path.join(_REAL_HOME, ".claude-swap-backup"), os.path.join(_REAL_HOME, ".claude")]

os.environ["USERPROFILE"] = str(HOME)
os.environ["HOME"] = str(HOME)
os.environ["CLAUDE_CONFIG_DIR"] = str(HOME / ".claude")
for _k in ("XDG_DATA_HOME", "HOMEDRIVE", "HOMEPATH"):
    os.environ.pop(_k, None)
(HOME / ".claude").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(SRC))


def _log(kind, what):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{' '.join(sys.argv[1:])[:60]!s:62} | {kind}: {what}\n")


def _hook(event, args):
    if event in ("open", "os.listdir", "os.scandir", "os.remove", "os.rename", "os.mkdir",
                 "os.rmdir", "shutil.copyfile", "os.replace") and args:
        p = args[0]
        if isinstance(p, (str, bytes, os.PathLike)):
            try:
                n = os.path.normcase(os.path.abspath(os.fsdecode(p)))
            except Exception:
                return
            for bad in _FORBIDDEN:
                if n == bad or n.startswith(bad + os.sep):
                    raise PermissionError(f"TRIAL GUARD: real-store access blocked ({event})")


sys.addaudithook(_hook)
sys.modules["keyring"] = None  # OS credential store unreachable


def _urlopen(req, *a, **k):
    _log("NET", getattr(req, "full_url", req))
    raise urllib.error.URLError("blocked by trial guard")


urllib.request.urlopen = _urlopen


class _Popen(subprocess.Popen):
    def __init__(self, args, *a, **k):
        _log("SUBPROCESS", args if isinstance(args, str) else " ".join(map(str, args))[:200])
        raise FileNotFoundError("blocked by trial guard")


subprocess.Popen = _Popen


def _noexec(*a, **k):
    _log("EXEC", a[:2])
    raise OSError("exec blocked by trial guard")


for _n in ("execv", "execve", "execvp", "execvpe", "execl", "execlp", "execle", "execlpe"):
    if hasattr(os, _n):
        setattr(os, _n, _noexec)
