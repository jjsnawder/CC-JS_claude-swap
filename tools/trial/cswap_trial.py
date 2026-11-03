"""Run a cswap command against the scratch store: python tools/trial/cswap_trial.py list"""
import sys

import guard  # noqa: F401  (must be imported before claude_swap)
from claude_swap.cli import main

sys.argv = ["cswap", *sys.argv[1:]]
main()
