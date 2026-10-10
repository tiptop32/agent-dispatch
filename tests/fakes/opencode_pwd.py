#!/usr/bin/env python3
"""Fake opencode, эмулирующий выбор каталога v2: PWD прежде getcwd.

Upstream cli-run.ts:73 выбирает `process.env.PWD ?? process.cwd()`. Фейк
записывает то, что выбрал, в $FAKE_CAPTURE.pwd, argv — в $FAKE_CAPTURE.argv
и печатает штатный результат.
"""

import os
import sys
from pathlib import Path

capture = os.environ.get("FAKE_CAPTURE")
if capture:
    Path(capture + ".argv").write_text("\n".join(sys.argv[1:]) + "\n")
    chosen = os.environ.get("PWD") or os.getcwd()
    Path(capture + ".pwd").write_text(chosen + "\n")
    Path(capture + ".env").write_text("\n".join(f"{k}={v}" for k, v in os.environ.items()) + "\n")

print(Path(__file__).parent.joinpath("../fixtures/agent_output/opencode_run_json.txt").read_text())
