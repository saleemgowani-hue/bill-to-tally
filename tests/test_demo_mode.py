"""DEMO_MODE: a public showcase deployment prepares its own demo company."""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(code: str, tmp_path, **env):
    full_env = {k: v for k, v in os.environ.items()
                if k not in ("LICENSE_SIGNING_SECRET", "DEMO_MODE", "LICENSE_REQUIRED")}
    full_env.update(DATA_DIR=str(tmp_path), DATABASE_URL=f"sqlite:///{tmp_path}/demo.db", PYTHONPATH=str(ROOT), **env)
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=ROOT,
                          env=full_env, capture_output=True, text=True, timeout=120)


def test_demo_mode_off_by_default(tmp_path):
    out = _run("from utils.config import settings; print(settings.DEMO_MODE, settings.LICENSE_REQUIRED)",
               tmp_path, LICENSE_SIGNING_SECRET="x" * 32)
    assert out.stdout.split() == ["False", "True"], out.stderr


def test_demo_mode_seeds_once_and_needs_no_licence(tmp_path):
    code = """
        from services import demo_bootstrap
        from database.database import get_session
        from database.models import User, Company
        from utils.config import settings
        demo_bootstrap.ensure_demo_company()
        demo_bootstrap.ensure_demo_company()          # second call must be harmless
        db = get_session()
        print(settings.DEMO_MODE, settings.LICENSE_REQUIRED,
              db.query(User).count(), db.query(Company).count())
    """
    out = _run(code, tmp_path, DEMO_MODE="true")
    assert out.stdout.split() == ["True", "False", "1", "1"], out.stderr


def test_flag_file_is_not_shipped_and_bootstrap_is_not_in_customer_build():
    text = (ROOT / "make_customer_build.py").read_text(encoding="utf-8")
    assert '"DEMO_MODE.flag"' in text and '"demo_bootstrap.py"' in text
    assert not (ROOT / "DEMO_MODE.flag").exists()
