"""Skip the tests that need the ML stack (torch, open_clip, scikit-learn, joblib) on machines
without it, e.g. the Pi or a laptop that only runs the brain. They still run wherever the
stack is installed (gx10/vision/requirements.txt). Everything else here always runs."""

import importlib

ML_TESTS = ["test_embed.py", "test_heads.py", "test_vision_app.py"]


def _has(module: str) -> bool:
    try:
        importlib.import_module(module)
    except Exception:  # not just ImportError: open_clip fails differently when torch is absent
        return False
    return True


missing = [m for m in ("torch", "open_clip", "sklearn", "joblib") if not _has(m)]
collect_ignore = ML_TESTS if missing else []
if missing:
    print(f"\ngx10/vision: skipping {', '.join(ML_TESTS)} (missing {', '.join(missing)})")
