"""Run every native/shared unittest module without importing legacy UI scripts.

The 1.3 storage smoke script is isolated because it mutates process-level data
paths at import time. The browser suite belongs to the separate legacy build.
"""
from pathlib import Path
import os
import subprocess
import sys
import unittest


def main():
    source = Path(__file__).resolve().parents[1] / "source" / "taskboard"
    os.chdir(source)
    sys.path.insert(0, str(source))
    scripts = {"test_browser", "test_storage_v13"}
    modules = [path.stem for path in sorted(source.glob("test_*.py")) if path.stem not in scripts]
    if not modules:
        raise SystemExit("No backend test modules were found")
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    if suite.countTestCases() == 0:
        raise SystemExit("No backend tests were collected")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return 1
    return subprocess.run([sys.executable, "test_storage_v13.py"], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
