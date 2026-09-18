"""Xyrus v2 test suites (stdlib unittest). Run from D:\\arc:

    venv\\Scripts\\python.exe -m unittest discover -s tests -v

Safety net: any test imported through this package writes only to a temp data dir,
never to D:\\arc\\data or D:\\arc\\config.json.
"""
import os
import tempfile

if not os.environ.get("XYRUS_DATA_DIR"):
    os.environ["XYRUS_DATA_DIR"] = tempfile.mkdtemp(prefix="xyrus_test_")
