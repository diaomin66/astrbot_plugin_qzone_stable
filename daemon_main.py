"""Process entry point for the QQ??? daemon."""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from qzone_bridge.daemon import main


if __name__ == "__main__":
    main()
