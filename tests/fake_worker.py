from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

mode = sys.argv[1]
state_dir = Path(sys.argv[2])
prompt = sys.stdin.read()
(state_dir).mkdir(parents=True, exist_ok=True)
(state_dir / "stdout.log").write_text("worker stdout\n", encoding="utf-8")
(state_dir / "stderr.log").write_text("worker stderr\n", encoding="utf-8")
result = state_dir / "worker-result.json"
if mode == "success":
    result.write_text(
        json.dumps(
            {
                "session_id": "fake-session",
                "final_response": "done",
                "finish_reason": "completed",
                "return_code": 0,
                "usage": {"input_tokens": 1, "output_tokens": 2, "requests": 1},
            }
        ),
        encoding="utf-8",
    )
    sys.exit(0)
if mode == "nonzero":
    sys.exit(7)
if mode == "corrupt":
    result.write_text("{not-json", encoding="utf-8")
    sys.exit(0)
if mode == "hang":
    time.sleep(60)
    sys.exit(0)
if mode == "child":
    # Stay in the worker process group; the manager must terminate both.
    child = os.fork()
    if child == 0:
        time.sleep(60)
        os._exit(0)
    (state_dir / "child.pid").write_text(str(child), encoding="utf-8")
    time.sleep(60)
    sys.exit(0)
raise SystemExit(2)
