"""Log the scratch Claude config in as fake account <letter> (a..i), ready for `cswap_trial.py add`.

    python tools/trial/fake_account.py a && python tools/trial/cswap_trial.py add
Tokens and emails are obviously fake (sk-ant-oat01-FAKE-..., a@example.test).
"""
import json
import sys
import time

import guard

L = sys.argv[1]
i = ord(L) - 96
cfg = guard.HOME / ".claude"
(cfg / ".claude.json").write_text(json.dumps({
    "oauthAccount": {"emailAddress": f"{L}@example.test",
                     "accountUuid": f"00000000-0000-4000-8000-00000000000{i}",
                     "organizationUuid": f"11111111-0000-4000-8000-00000000000{i}",
                     "organizationName": f"Fake Org {L}"},
    "numStartups": 1}, indent=2))
(cfg / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
    "accessToken": f"sk-ant-oat01-FAKE-{L}-" + "x" * 40,
    "refreshToken": f"sk-ant-ort01-FAKE-{L}-" + "y" * 40,
    "expiresAt": int(time.time() * 1000) + 8 * 3600 * 1000,
    "scopes": ["user:inference", "user:profile"], "subscriptionType": "max"}}))
print("wrote fake", L)
