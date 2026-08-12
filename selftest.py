#!/usr/bin/env python3
"""
ps-slayer self-test.

Exercises the paths that are awkward to reach by hand -- the tray menu callbacks,
config reload, and the kill engine -- against a throwaway config, using Notepad as
the victim. Run it after editing ps_slayer.py:

    .\\.venv\\Scripts\\python.exe selftest.py

Exits non-zero if anything fails.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ps_slayer as ps  # noqa: E402

CONFIG = """
[settings]
tray    = false
sounds  = false
[[mapping]]
name    = "selftest"
hotkey  = "ctrl+alt+f8"
type    = "process"
target  = ["notepad.exe", "not-a-real-process.exe"]
"""

failures: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  -- ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


def notepads() -> int:
    out = subprocess.run(["tasklist", "/fi", "imagename eq notepad.exe", "/nh"],
                         capture_output=True, text=True).stdout.lower()
    return out.count("notepad.exe")


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    ps.log.setLevel(logging.WARNING)

    cfg = Path(tempfile.gettempdir()) / "ps_slayer_selftest.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    app = ps.Slayer(cfg)

    print("\nps-slayer self-test\n")
    try:
        app.load()
        app.start()
        check("start: hotkey thread running", app.keys is not None and app.keys.is_alive())
        check("start: hotkey registered", bool(app.keys and app.keys._registered))

        # the tray "Reload config" button calls this with (icon, item)
        for i in range(1, 4):
            app.reload(None, None)
            alive = app.keys is not None and app.keys.is_alive()
            regd = bool(app.keys and app.keys._registered)
            check(f"reload {i}: thread alive", alive)
            check(f"reload {i}: hotkey re-registered", regd,
                  "" if regd else "old thread still holds the combo")

        # a UTF-8 BOM must not break parsing (Notepad adds one by default)
        cfg.write_bytes(b"\xef\xbb\xbf" + CONFIG.encode("utf-8"))
        try:
            ps.load_config(cfg)
            check("config: UTF-8 BOM tolerated", True)
        except Exception as exc:
            check("config: UTF-8 BOM tolerated", False, str(exc))

        # a protected process is refused
        killed, notes = app.engine.kill_targets([ps.Target("process", "csrss.exe", False)])
        check("safety: csrss.exe refused", killed == 0 and any("refused" in n for n in notes),
              str(notes))

        # real kill, plus a target that is not running -> partial, not an error
        subprocess.Popen(["notepad.exe"])
        time.sleep(2.5)
        before = notepads()
        app.fire(app.mappings[0], "selftest")
        time.sleep(0.8)
        check("kill: notepad died", before > 0 and notepads() == 0,
              f"{before} before, {notepads()} after")

        # the tray "Quit" button calls this
        app.stop()
        time.sleep(0.3)
        check("stop: watchers cleared", app.keys is None and app.joy is None)
    finally:
        try:
            app.stop()
        except Exception:
            pass
        cfg.unlink(missing_ok=True)

    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
