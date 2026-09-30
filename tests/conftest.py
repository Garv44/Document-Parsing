import os
import sys
import tempfile
from pathlib import Path

# Isolated DB + upload dir; set before any app module reads config.
_tmp = Path(tempfile.mkdtemp(prefix="docparse-test-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp / 'test.db'}"
os.environ["UPLOAD_DIR"] = str(_tmp / "uploads")
os.environ["ERP_WEBHOOK_URL"] = ""
os.environ["AUTO_EXPORT_VALIDATED"] = "false"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
