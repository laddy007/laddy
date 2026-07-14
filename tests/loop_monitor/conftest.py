"""Make the `loop_monitor` package importable in tests."""

import sys
from pathlib import Path

_MONITOR_DIR = Path(__file__).resolve().parents[2] / "monitoring"
if str(_MONITOR_DIR) not in sys.path:
    sys.path.insert(0, str(_MONITOR_DIR))
