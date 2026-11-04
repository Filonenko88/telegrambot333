from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
project_root_str = str(PROJECT_ROOT)

if project_root_str not in sys.path:
    sys.path.insert(0, project_root_str)

os.environ.setdefault("BOT_TOKEN", "123456789:TEST_TOKEN_FOR_LOCAL_TESTS")
os.environ.setdefault("ADMIN_IDS", "123456789")
