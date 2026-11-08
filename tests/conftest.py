"""Import the plugin as a package while making AstrBot available to tests."""

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = PLUGIN_ROOT.parent
ASTRBOT_ROOT = PLUGINS_DIR.parent.parent
for path in (PLUGINS_DIR, ASTRBOT_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
