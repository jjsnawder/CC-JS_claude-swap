#!/usr/bin/env python3
"""PreToolUse guard for WAT subagents. Exit 2 blocks the tool call and feeds the
message back to the subagent. Wired by default into every Bash-capable agent
(tool-runner, deep, chore, review) - see DEPLOYMENT.md, sec. 7."""
import json
import re
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)  # not JSON -> do not block

tool_input = data.get("tool_input", {}) or {}
cmd = str(tool_input.get("command", ""))
path = str(tool_input.get("file_path", ""))

# 1) Block git history mutations from a subagent.
if re.search(r"\bgit\s+(commit|push|reset|rebase|revert|merge|tag|cherry-pick)\b",
             cmd, re.IGNORECASE):
    sys.stderr.write("Blocked: git history is orchestrator-owned. Return your diff instead.\n")
    sys.exit(2)

# 2) Block writes into workflows/.
norm = path.replace("\\", "/")
if re.search(r"(^|/)workflows/", norm):
    sys.stderr.write("Blocked: workflows/ is orchestrator-owned. Propose the change in your summary.\n")
    sys.exit(2)

sys.exit(0)
