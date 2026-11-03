"""Run pytest at BELOW_NORMAL priority (dev-box compute rule; children inherit it).

Args pass straight through to pytest. Run from the checkout whose src/ you want
tested (pyproject's `pythonpath = ["src"]` picks that copy, not the live install):

    python tools/run_tests_low.py -q -n 4 -p no:cacheprovider
    python ../../tools/run_tests_low.py -q -n 4 tests/test_x.py   # from a worktree
"""
import ctypes
import sys

if sys.platform == "win32":
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL_PRIORITY_CLASS

import pytest  # noqa: E402

sys.exit(pytest.main(sys.argv[1:]))
