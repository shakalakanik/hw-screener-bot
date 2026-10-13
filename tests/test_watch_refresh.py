"""Обёртка: python tests/test_watch_refresh.py → node tests/test_watch_refresh.js (Mini App «Обновить всё»)."""
import os, subprocess, sys
r = subprocess.run(["node", os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_watch_refresh.js")],
                   capture_output=True, text=True)
print("\n".join(l for l in r.stdout.splitlines() if l.startswith(("OK", "FAIL"))))
sys.exit(r.returncode)
