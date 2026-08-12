#!/usr/bin/env python3
"""
ps-slayer -- instant process killer bound to sim-racing wheel buttons or keyboard combos.

Run modes:
    python ps_slayer.py                 run the daemon (normally started by Task Scheduler)
    python ps_slayer.py --list-devices  print detected controllers
    python ps_slayer.py --learn         live view of button presses (to find button indices)
    python ps_slayer.py --check         validate config.toml and exit
    python ps_slayer.py --test "Name"   fire one mapping by name, once
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import fnmatch
import io
import logging
import math
import os
import random
import sys
import threading
import time
import wave
from array import array
from logging.handlers import RotatingFileHandler
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        sys.exit("ps-slayer needs Python 3.11+ (or `pip install tomli`).")

APP_NAME = "ps-slayer"
__version__ = "1.2.0"
APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = APP_DIR / "config.toml"
LOG_DIR = APP_DIR / "logs"

log = logging.getLogger(APP_NAME)


# --------------------------------------------------------------------------------------
# Win32 layer
# --------------------------------------------------------------------------------------

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x0002
STILL_ACTIVE = 259
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PATH = 260

TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008
SE_PRIVILEGE_ENABLED = 0x0002

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wt.DWORD), ("HighPart", ctypes.c_long)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wt.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wt.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND),
        ("message", wt.UINT),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", wt.DWORD),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]


kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wt.BOOL
kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wt.BOOL
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
kernel32.TerminateProcess.restype = wt.BOOL
kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
kernel32.GetExitCodeProcess.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)
]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
kernel32.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
kernel32.CreateMutexW.restype = wt.HANDLE   # default c_int truncates a 64-bit handle

ERROR_ALREADY_EXISTS = 183

user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.RegisterHotKey.restype = wt.BOOL
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
# NB: PostThreadMessage is user32, not kernel32 -- getting this wrong only shows up
# when something actually tries to stop the hotkey thread (tray Reload / Quit).
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
user32.PostThreadMessageW.restype = wt.BOOL
user32.GetMessageW.restype = ctypes.c_int

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]


def enable_debug_privilege() -> bool:
    """SeDebugPrivilege lets us open handles to processes owned by other users."""
    h_token = wt.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
        ctypes.byref(h_token),
    ):
        return False
    try:
        luid = LUID()
        if not advapi32.LookupPrivilegeValueW(None, "SeDebugPrivilege", ctypes.byref(luid)):
            return False
        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        advapi32.AdjustTokenPrivileges(h_token, False, ctypes.byref(tp), 0, None, None)
        return ctypes.get_last_error() == 0
    finally:
        kernel32.CloseHandle(h_token)


def is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def snapshot_processes() -> list[tuple[int, int, str]]:
    """[(pid, parent_pid, lowercase_exe_name)] for every process we can see."""
    out: list[tuple[int, int, str]] = []
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return out
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            out.append((pe.th32ProcessID, pe.th32ParentProcessID, pe.szExeFile.lower()))
            ok = kernel32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        kernel32.CloseHandle(snap)
    return out


def process_name(pid: int) -> str:
    """Fast exe-name lookup for a single pid (no full snapshot)."""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wt.DWORD(MAX_PATH)
        buf = ctypes.create_unicode_buffer(MAX_PATH)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def descendants(pid: int, children_of: dict[int, list[int]], limit: int = 512) -> list[int]:
    found: list[int] = []
    stack = list(children_of.get(pid, ()))
    seen = {pid}
    while stack and len(found) < limit:
        child = stack.pop()
        if child in seen:
            continue
        seen.add(child)
        found.append(child)
        stack.extend(children_of.get(child, ()))
    return found


def visible_windows() -> list[tuple[int, int, str]]:
    """[(hwnd, pid, title)] for visible top-level windows that have a title."""
    results: list[tuple[int, int, str]] = []

    @WNDENUMPROC
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        results.append((hwnd, pid.value, buf.value))
        return True

    user32.EnumWindows(_cb, 0)
    return results


# --------------------------------------------------------------------------------------
# Kill engine
# --------------------------------------------------------------------------------------

PROTECTED = {
    "system", "system idle process", "registry", "memory compression", "ntoskrnl.exe",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "lsaiso.exe", "svchost.exe", "fontdrvhost.exe", "dwm.exe",
    "audiodg.exe", "sihost.exe", "ctfmon.exe", "conhost.exe",
}

SELF_PID = os.getpid()


class KillEngine:
    """
    Terminates processes with the lowest latency Windows allows.

    Fast path: a background thread keeps PROCESS_TERMINATE handles open for every process
    matching a configured target, so firing a mapping is a single TerminateProcess() call
    -- tens of microseconds, no enumeration. Falls back to a live snapshot if the cache
    has nothing (process started since the last refresh).
    """

    def __init__(self, patterns: set[str], refresh_ms: int = 1000, allow_system: bool = False,
                 use_cache: bool = True):
        self.patterns = {p.lower() for p in patterns}
        self.refresh_ms = max(200, refresh_ms)
        self.allow_system = allow_system
        self.use_cache = use_cache
        self._lock = threading.Lock()
        self._cache: dict[str, list[tuple[int, int]]] = {}       # pattern -> [(pid, handle)]
        self._tree_cache: dict[int, list[tuple[int, int]]] = {}  # pid -> [(child_pid, handle)]
        self._open: dict[int, int] = {}                          # pid -> handle
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- cache maintenance ---------------------------------------------------------

    def start(self):
        if not self.use_cache or not self.patterns:
            return
        self._thread = threading.Thread(target=self._refresh_loop, name="handle-cache", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            for h in self._open.values():
                kernel32.CloseHandle(h)
            self._open.clear()
            self._cache.clear()
            self._tree_cache.clear()

    def _refresh_loop(self):
        while not self._stop.is_set():
            try:
                self._refresh()
            except Exception:
                log.exception("handle cache refresh failed")
            self._stop.wait(self.refresh_ms / 1000.0)

    def _refresh(self):
        procs = snapshot_processes()
        children_of: dict[int, list[int]] = {}
        for pid, ppid, _ in procs:
            children_of.setdefault(ppid, []).append(pid)

        matches: dict[str, list[int]] = {p: [] for p in self.patterns}
        for pid, _ppid, name in procs:
            if pid == SELF_PID:
                continue
            for pat in self.patterns:
                if fnmatch.fnmatch(name, pat):
                    matches[pat].append(pid)

        needed: set[int] = set()
        for pids in matches.values():
            needed.update(pids)
            for pid in pids:
                needed.update(descendants(pid, children_of))
        needed.discard(SELF_PID)

        with self._lock:
            for pid in list(self._open):
                if pid not in needed:
                    kernel32.CloseHandle(self._open.pop(pid))
            for pid in needed:
                if pid not in self._open:
                    # QUERY_LIMITED_INFORMATION as well as TERMINATE: without it
                    # GetExitCodeProcess() fails and every kill falls back to a scan.
                    h = kernel32.OpenProcess(
                        PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                    if not h:
                        h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
                    if h:
                        self._open[pid] = h
            self._cache = {
                pat: [(pid, self._open[pid]) for pid in pids if pid in self._open]
                for pat, pids in matches.items()
            }
            self._tree_cache = {
                pid: [(c, self._open[c]) for c in descendants(pid, children_of) if c in self._open]
                for pids in matches.values() for pid in pids
            }

    # -- killing -------------------------------------------------------------------

    def _terminate(self, handle: int) -> bool:
        code = wt.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != STILL_ACTIVE:
            return False  # already gone; don't inflate the count
        return bool(kernel32.TerminateProcess(handle, 1))

    def _terminate_pid(self, pid: int) -> bool:
        if pid == SELF_PID:
            return False
        h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not h:
            return False
        try:
            return bool(kernel32.TerminateProcess(h, 1))
        finally:
            kernel32.CloseHandle(h)

    def _protected(self, name: str) -> bool:
        return (not self.allow_system) and name.lower() in PROTECTED

    def _kill_pids(self, pids, tree: bool, children_of: dict[int, list[int]]) -> int:
        n = 0
        if tree:  # children first, so nothing gets respawned by a dying supervisor
            for pid in pids:
                for child in descendants(pid, children_of):
                    n += self._terminate_pid(child)
        for pid in pids:
            n += self._terminate_pid(pid)
        return n

    def kill_targets(self, targets: list["Target"],
                     foreground_pid: int | None = None) -> tuple[int, list[str]]:
        """
        Kill every target of a mapping. Cached handles for all process targets are
        collected under one lock, then fired back to back with nothing in between,
        so a multi-program mapping goes down as close to simultaneously as the
        scheduler allows. Anything the cache missed is resolved from a single
        shared process snapshot.
        """
        killed = 0
        notes: list[str] = []

        # -- phase 1: gather every pre-opened handle, one lock acquisition ---------
        gathered: list[tuple[Target, list[int]]] = []
        with self._lock:
            for t in targets:
                if t.type != "process":
                    continue
                pat = (t.value or "").lower()
                if self._protected(pat):
                    notes.append(f"{pat}: refused (protected)")
                    continue
                cached = self._cache.get(pat, ())
                handles: list[int] = []
                if t.tree:
                    for pid, _h in cached:
                        handles.extend(h for _c, h in self._tree_cache.get(pid, ()))
                handles.extend(h for _pid, h in cached)
                gathered.append((t, handles))

        # -- phase 2: fire. no locks, no allocation, no enumeration ---------------
        missed: list[Target] = []
        for t, handles in gathered:
            n = 0
            for h in handles:
                n += self._terminate(h)
            if n:
                killed += n
                notes.append(f"{t.value}:{n}")
            else:
                missed.append(t)

        windows = [t for t in targets if t.type == "window"]
        foreground = [t for t in targets if t.type == "foreground"]
        if not (missed or windows or foreground):
            return killed, notes

        # -- phase 3: one snapshot serves every remaining target ------------------
        procs = snapshot_processes()
        children_of: dict[int, list[int]] = {}
        names: dict[int, str] = {}
        for pid, ppid, name in procs:
            children_of.setdefault(ppid, []).append(pid)
            names[pid] = name

        for t in missed:
            pat = (t.value or "").lower()
            pids = [pid for pid, _pp, name in procs
                    if fnmatch.fnmatch(name, pat) and not self._protected(name)]
            n = self._kill_pids(pids, t.tree, children_of)
            killed += n
            notes.append(f"{t.value}:{n} (scan)" if n else f"{t.value}: none running")

        if windows:
            visible = visible_windows()
            for t in windows:
                pat = (t.value or "").lower()
                glob = any(c in pat for c in "*?")
                pids = {pid for _hwnd, pid, title in visible
                        if (fnmatch.fnmatch(title.lower(), pat) if glob else pat in title.lower())}
                pids.discard(SELF_PID)
                live = [p for p in pids if not self._protected(names.get(p, ""))]
                n = self._kill_pids(live, t.tree, children_of)
                killed += n
                notes.append(f"window {t.value}:{n}" if n
                             else f"window {t.value}: no match")

        for t in foreground:
            name = names.get(foreground_pid or 0, "")
            if not foreground_pid or foreground_pid == SELF_PID:
                notes.append("foreground: nothing focused")
                continue
            if self._protected(name):
                notes.append(f"foreground {name}: refused (protected)")
                continue
            n = self._kill_pids([foreground_pid], t.tree, children_of)
            killed += n
            notes.append(f"foreground {name or foreground_pid}:{n}")

        return killed, notes


# --------------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------------

VK_NAMES: dict[str, int] = {
    "backspace": 0x08, "tab": 0x09, "clear": 0x0C, "enter": 0x0D, "return": 0x0D,
    "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "pgup": 0x21, "pageup": 0x21, "pgdn": 0x22, "pagedown": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "prtsc": 0x2C, "insert": 0x2D, "ins": 0x2D,
    "delete": 0x2E, "del": 0x2E, "numlock": 0x90, "scrolllock": 0x91,
    "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
    "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}
VK_NAMES.update({f"f{i}": 0x6F + i for i in range(1, 25)})       # f1..f24 -> 0x70..0x87
VK_NAMES.update({f"num{i}": 0x60 + i for i in range(10)})        # numpad 0..9
MOD_NAMES = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT, "shift": MOD_SHIFT,
    "win": MOD_WIN, "super": MOD_WIN, "meta": MOD_WIN,
}


class ConfigError(Exception):
    pass


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+alt+k' -> (modifiers, virtual_key_code)"""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ConfigError(f"empty hotkey {spec!r}")
    mods, vk = 0, None
    for part in parts:
        if part in MOD_NAMES:
            mods |= MOD_NAMES[part]
        elif part in VK_NAMES:
            vk = VK_NAMES[part]
        elif len(part) == 1 and (part.isalnum()):
            vk = ord(part.upper())
        else:
            raise ConfigError(f"unknown key {part!r} in hotkey {spec!r}")
    if vk is None:
        raise ConfigError(f"hotkey {spec!r} has modifiers but no key")
    return mods | MOD_NOREPEAT, vk


TARGET_TYPES = ("process", "window", "foreground")


class Target:
    """One thing to kill. A mapping holds a list of these."""
    __slots__ = ("type", "value", "tree")

    def __init__(self, type_: str, value: str | None, tree: bool):
        self.type = type_
        self.value = value
        self.tree = tree

    def __str__(self) -> str:
        base = "foreground app" if self.type == "foreground" else f"{self.type} {self.value!r}"
        return base + (" +children" if self.tree else "")


class Mapping:
    __slots__ = ("name", "enabled", "buttons", "device", "guid", "hotkey", "hotkey_parsed",
                 "targets", "tree", "hold_ms", "cooldown_ms",
                 "_down_since", "_fired", "_last_fire")

    def __init__(self, raw: dict, index: int):
        def bad(msg):
            raise ConfigError(f"[[mapping]] #{index + 1}: {msg}")

        self.name = str(raw.get("name") or f"mapping {index + 1}")
        self.enabled = bool(raw.get("enabled", True))

        default_type = str(raw.get("type", "process")).lower()
        if default_type not in TARGET_TYPES:
            bad(f"type must be one of {', '.join(TARGET_TYPES)} (got {default_type!r})")
        self.tree = bool(raw.get("tree", False))
        self.targets = self._parse_targets(raw, bad, default_type, self.tree)
        if not self.targets:
            bad('nothing to kill -- set `target`, `targets`, or type = "foreground"')

        buttons = raw.get("buttons")
        if buttons is None and "button" in raw:
            buttons = [raw["button"]]
        if buttons is not None:
            if not isinstance(buttons, list) or not all(isinstance(b, int) for b in buttons):
                bad("`buttons` must be a list of integers, e.g. buttons = [12, 13]")
            if not buttons:
                bad("`buttons` is empty")
        self.buttons = list(buttons) if buttons else []

        self.device = str(raw["device"]) if raw.get("device") else None
        self.guid = str(raw["guid"]).lower() if raw.get("guid") else None
        self.hotkey = str(raw["hotkey"]) if raw.get("hotkey") else None
        self.hotkey_parsed = parse_hotkey(self.hotkey) if self.hotkey else None

        if not self.buttons and not self.hotkey:
            bad("needs either `buttons` (wheel) or `hotkey` (keyboard)")

        self.hold_ms = int(raw.get("hold_ms", 0))
        self.cooldown_ms = int(raw.get("cooldown_ms", 400))

        self._down_since: float | None = None
        self._fired = False
        self._last_fire = 0.0

    @staticmethod
    def _parse_targets(raw: dict, bad, default_type: str, default_tree: bool) -> list[Target]:
        """
        Accepts, in increasing order of verbosity:
            target  = "a.exe"
            target  = ["a.exe", "b.exe"]                       # same type for all
            targets = ["a.exe", "b.exe"]                       # same thing
            targets = [{type="process", target="a.exe", tree=true},
                       {type="window",  target="*telemetry*"},
                       {type="foreground"}]                    # mixed types
        `target` and `targets` may both be present; results are concatenated in order.
        """
        out: list[Target] = []

        def add(type_: str, value, tree: bool):
            if type_ not in TARGET_TYPES:
                bad(f"unknown target type {type_!r} (expected {', '.join(TARGET_TYPES)})")
            if type_ == "foreground":
                out.append(Target("foreground", None, tree))
                return
            if not isinstance(value, str) or not value.strip():
                bad(f"target for type {type_!r} must be a non-empty string, got {value!r}")
            out.append(Target(type_, value.strip(), tree))

        spec = raw.get("target")
        if spec is None:
            if default_type == "foreground":
                add("foreground", None, default_tree)
        else:
            items = spec if isinstance(spec, list) else [spec]
            if not items:
                bad("`target` is an empty list")
            for item in items:
                add(default_type, item, default_tree)

        extra = raw.get("targets", [])
        if not isinstance(extra, list):
            bad("`targets` must be a list")
        for entry in extra:
            if isinstance(entry, str):
                # a bare string can't be a foreground target, so assume a process
                add("process" if default_type == "foreground" else default_type,
                    entry, default_tree)
            elif isinstance(entry, dict):
                add(str(entry.get("type", default_type)).lower(),
                    entry.get("target"),
                    bool(entry.get("tree", default_tree)))
            else:
                bad(f"`targets` entries must be strings or tables, got {entry!r}")
        return out

    @property
    def trigger_text(self) -> str:
        bits = []
        if self.buttons:
            dev = self.device or self.guid or "any wheel"
            bits.append(f"{dev} button " + "+".join(str(b) for b in self.buttons))
        if self.hotkey:
            bits.append(self.hotkey)
        hold = f" (hold {self.hold_ms}ms)" if self.hold_ms else ""
        return " / ".join(bits) + hold

    @property
    def action_text(self) -> str:
        return "kill " + ", ".join(str(t) for t in self.targets)


DEFAULT_SETTINGS = {
    "poll_hz": 500,
    "handle_cache": True,
    "handle_refresh_ms": 1000,
    "sounds": True,
    "volume": 0.7,
    "tray": True,
    "allow_system": False,
    "log_level": "INFO",
}


def load_config(path: Path) -> tuple[dict, list[Mapping]]:
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]  # Notepad saves UTF-8 with a BOM; tomllib refuses to parse it
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path.name} must be saved as UTF-8 ({exc})") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: {exc}") from exc

    settings = dict(DEFAULT_SETTINGS)
    raw_settings = data.get("settings", {})
    settings.update(raw_settings)
    settings["poll_hz"] = max(50, min(1000, int(settings["poll_hz"])))
    settings["volume"] = max(0.0, min(1.0, float(settings["volume"])))
    if "beep" in raw_settings and "sounds" not in raw_settings:
        settings["sounds"] = bool(raw_settings["beep"])  # older key name

    raw_maps = data.get("mapping", [])
    if not isinstance(raw_maps, list):
        raise ConfigError("`mapping` must be a list of [[mapping]] tables")
    mappings = [Mapping(raw, i) for i, raw in enumerate(raw_maps)]

    # Names are only labels (used by --test and the tray menu). A collision must not
    # ground the whole tool -- including the panic key -- so rename instead of failing.
    seen: dict[str, int] = {}
    for m in mappings:
        if m.name in seen:
            seen[m.name] += 1
            renamed = f"{m.name} ({seen[m.name]})"
            log.warning("duplicate mapping name %r -- using %r for the second one",
                        m.name, renamed)
            m.name = renamed
        seen.setdefault(m.name, 1)
    return settings, mappings


# --------------------------------------------------------------------------------------
# Input watchers
# --------------------------------------------------------------------------------------

def _init_sdl():
    """SDL must be told to run headless and to report input while unfocused."""
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    import pygame  # noqa: PLC0415
    pygame.init()
    pygame.joystick.init()
    return pygame


def _device_matches(mapping: Mapping, joy) -> bool:
    if mapping.guid:
        return joy.get_guid().lower() == mapping.guid
    if mapping.device:
        return fnmatch.fnmatch(joy.get_name().lower(), mapping.device.lower())
    return True


class JoystickWatcher(threading.Thread):
    def __init__(self, mappings: list[Mapping], fire, poll_hz: int):
        super().__init__(name="joystick", daemon=True)
        self.mappings = [m for m in mappings if m.enabled and m.buttons]
        self.fire = fire
        self.period = 1.0 / poll_hz
        self.stop_event = threading.Event()
        self.devices: dict[int, object] = {}

    def stop(self, timeout: float = 2.0):
        self.stop_event.set()
        self.join(timeout)
        if self.is_alive():
            log.warning("joystick thread would not exit")

    def run(self):
        if not self.mappings:
            return
        try:
            pygame = _init_sdl()
        except Exception:
            log.exception("could not initialise SDL/pygame -- wheel triggers disabled")
            return

        # 1 ms scheduler granularity so time.sleep() is actually short
        try:
            ctypes.WinDLL("winmm").timeBeginPeriod(1)
        except Exception:
            pass

        for i in range(pygame.joystick.get_count()):
            self._add(pygame, i)

        log.info("watching %d wheel mapping(s) at %d Hz", len(self.mappings), round(1 / self.period))
        while not self.stop_event.is_set():
            for event in pygame.event.get():
                if event.type == pygame.JOYDEVICEADDED:
                    self._add(pygame, event.device_index)
                elif event.type == pygame.JOYDEVICEREMOVED:
                    joy = self.devices.pop(event.instance_id, None)
                    if joy is not None:
                        log.info("controller disconnected: %s", joy.get_name())
            now = time.perf_counter()
            for m in self.mappings:
                self._evaluate(m, now)
            time.sleep(self.period)

        for joy in self.devices.values():
            try:
                joy.quit()
            except Exception:
                pass
        pygame.quit()

    def _add(self, pygame, index: int):
        try:
            joy = pygame.joystick.Joystick(index)
            joy.init()
        except Exception:
            log.exception("could not open controller at index %d", index)
            return
        iid = joy.get_instance_id()
        if iid in self.devices:
            return
        self.devices[iid] = joy
        log.info("controller: %r  guid=%s  buttons=%d",
                 joy.get_name(), joy.get_guid(), joy.get_numbuttons())

    def _combo_down(self, m: Mapping) -> bool:
        for joy in self.devices.values():
            if not _device_matches(m, joy):
                continue
            n = joy.get_numbuttons()
            if all(b < n and joy.get_button(b) for b in m.buttons):
                return True
        return False

    def _evaluate(self, m: Mapping, now: float):
        down = self._combo_down(m)
        if not down:
            m._down_since = None
            m._fired = False
            return
        if m._down_since is None:
            m._down_since = now
        if m._fired:
            return
        if (now - m._down_since) * 1000.0 < m.hold_ms:
            return
        if (now - m._last_fire) * 1000.0 < m.cooldown_ms:
            return
        m._fired = True
        m._last_fire = now
        self.fire(m, "wheel")


class HotkeyWatcher(threading.Thread):
    """Uses RegisterHotKey -- the OS dispatches straight to us, no polling, no key hook."""

    def __init__(self, mappings: list[Mapping], fire):
        super().__init__(name="hotkeys", daemon=True)
        self.mappings = [m for m in mappings if m.enabled and m.hotkey]
        self.fire = fire
        self.thread_id = 0
        self.ready = threading.Event()   # set once every combo has been registered
        self._registered: list[int] = []

    def run(self):
        if not self.mappings:
            self.ready.set()
            return
        self.thread_id = kernel32.GetCurrentThreadId()
        by_id: dict[int, Mapping] = {}
        for i, m in enumerate(self.mappings, start=1):
            mods, vk = m.hotkey_parsed  # type: ignore[misc]
            if user32.RegisterHotKey(None, i, mods, vk):
                by_id[i] = m
                self._registered.append(i)
                log.info("hotkey registered: %s -> %s", m.hotkey, m.name)
            else:
                log.error("hotkey %s (%s) is already taken by another app", m.hotkey, m.name)
        self.ready.set()

        msg = MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            if msg.message == WM_HOTKEY:
                m = by_id.get(int(msg.wParam))
                if m is None:
                    continue
                now = time.perf_counter()
                if (now - m._last_fire) * 1000.0 < m.cooldown_ms:
                    continue
                m._last_fire = now
                self.fire(m, "keyboard")

        for hk_id in self._registered:
            user32.UnregisterHotKey(None, hk_id)

    def stop(self, timeout: float = 2.0):
        # run() may not have reached GetMessageW yet; without a thread id there is
        # nothing to post to, and the old registrations would outlive the reload
        deadline = time.perf_counter() + 1.0
        while self.is_alive() and not self.thread_id and time.perf_counter() < deadline:
            time.sleep(0.005)
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        self.join(timeout)
        if self.is_alive():
            log.warning("hotkey thread would not exit -- its combos stay registered")


# --------------------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------------------

# ======================================================================================
# Theme -- the sword and the sounds it makes
# ======================================================================================

# -- icon ------------------------------------------------------------------------------

ICON_PALETTES = {
    #            blade       blade edge   fuller     guard      grip       pommel     gem
    "idle": ("#DCE3EC", "#5F6B7A", "#A9B4C2", "#C9A227", "#4A3524", "#B8912A", "#B3261E"),
    "slay": ("#EF4136", "#6E1512", "#FF9E96", "#F5CB47", "#5A4230", "#E8BA36", "#FFE08A"),
    "miss": ("#AEB8C4", "#5C6672", "#8D97A4", "#9A8742", "#3B2C20", "#8B7830", "#7A4B47"),
}


def sword_icon(mood: str = "idle", size: int = 64):
    """
    A slayer's sword, drawn at 4x and downsampled so the edges survive being
    squeezed into a 16px tray slot. The crossguard sweeps up into dragon wings
    and there's a gem where the blade meets the hilt.
    """
    from PIL import Image, ImageDraw

    blade, edge, fuller, guard, grip, pommel, gem = ICON_PALETTES.get(
        mood, ICON_PALETTES["idle"])
    S = 256
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # pommel and grip -- deliberately short, so the blade dominates
    d.ellipse((117, 6, 139, 28), fill=pommel, outline=edge, width=3)
    d.rounded_rectangle((120, 24, 136, 66), radius=5, fill=grip, outline=edge, width=3)

    # crossguard: narrow bar with the tips swept up into a pair of wings
    d.polygon([(74, 58), (113, 72), (143, 72), (182, 58),
               (184, 74), (145, 88), (111, 88), (72, 74)],
              fill=guard, outline=edge, width=3)

    # blade: long, narrow, parallel sides into a taper -- a sword, not a dagger
    d.polygon([(114, 78), (142, 78), (142, 198), (128, 252), (114, 198)],
              fill=blade, outline=edge, width=3)
    d.line((128, 92, 128, 194), fill=fuller, width=3)           # fuller

    # gem where the blade meets the hilt
    d.ellipse((120, 64, 136, 80), fill=gem, outline=edge, width=3)

    # Tilt it so it reads as a sword and not a letter T at 16px, then crop to the
    # blade and re-square it -- rotation leaves dead margin, and a tray slot is far
    # too small to waste any of.
    img = img.rotate(30, resample=Image.BICUBIC, expand=True)
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    w, h = img.size
    side = max(w, h)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.alpha_composite(img, ((side - w) // 2, (side - h) // 2))
    return square.resize((size, size), Image.LANCZOS)


# -- sound synthesis -------------------------------------------------------------------
# Everything below is stdlib-only DSP: build float samples, wrap them in a WAV in
# memory, hand the bytes to winsound. No audio dependency, no files on disk.

SAMPLE_RATE = 22050


def _svf_bandpass(src, sr, f_start, f_end, q=1.4):
    """State-variable bandpass whose centre frequency sweeps across the buffer."""
    out = []
    low = band = 0.0
    n = max(1, len(src))
    qq = 1.0 / q
    for i, x in enumerate(src):
        fc = f_start + (f_end - f_start) * (i / n)
        f = 2.0 * math.sin(math.pi * min(fc, sr * 0.45) / sr)
        high = x - low - qq * band
        band += f * high
        low += f * band
        out.append(band)
    return out


def _lowpass(src, sr, fc):
    a = 1.0 - math.exp(-2.0 * math.pi * fc / sr)
    y = 0.0
    out = []
    for x in src:
        y += a * (x - y)
        out.append(y)
    return out


def _partials(n, freqs, tau, sr=SAMPLE_RATE):
    """Inharmonic partials with exponential decay -- struck metal, not a synth tone."""
    out = [0.0] * n
    for k, f in enumerate(freqs):
        amp = 1.0 / (k + 1.3)
        t_decay = tau * (0.88 ** k)
        w = 2.0 * math.pi * f / sr
        for i in range(n):
            out[i] += amp * math.exp(-(i / sr) / t_decay) * math.sin(w * i)
    return out


def _sound_slay(sr=SAMPLE_RATE):
    """A clean draw-cut: air rushing off the edge, then a bright ring. Target died."""
    n = int(0.34 * sr)
    rng = random.Random(7)
    noise = [rng.uniform(-1.0, 1.0) for _ in range(n)]
    swish = _svf_bandpass(noise, sr, 1800, 7200, q=1.1)
    ring = _partials(n, (1867, 2789, 3971, 5303), 0.20, sr)
    delay = int(0.018 * sr)
    out = []
    for i in range(n):
        env = min(1.0, i / (0.004 * sr)) * math.exp(-(i / sr) / 0.055)
        out.append(0.85 * swish[i] * env + (0.55 * ring[i - delay] if i >= delay else 0.0))
    return out


def _sound_graze(sr=SAMPLE_RATE):
    """Steel skidding off scales, with a small bounce. Some targets weren't there."""
    n = int(0.30 * sr)
    rng = random.Random(11)
    noise = [rng.uniform(-1.0, 1.0) for _ in range(n)]
    scrape = _svf_bandpass(noise, sr, 1400, 700, q=1.6)
    hit = _partials(n, (742, 1103, 1631, 2417), 0.085, sr)
    bounce = _partials(n, (611, 909, 1344), 0.055, sr)
    at = int(0.085 * sr)
    out = []
    for i in range(n):
        v = 0.70 * hit[i] + 0.35 * scrape[i] * math.exp(-(i / sr) / 0.030)
        if i >= at:
            v += 0.32 * bounce[i - at]
        out.append(v)
    return out


def _sound_whiff(sr=SAMPLE_RATE):
    """Swung at empty air. Nothing was running."""
    n = int(0.26 * sr)
    rng = random.Random(3)
    noise = [rng.uniform(-1.0, 1.0) for _ in range(n)]
    air = _svf_bandpass(noise, sr, 2100, 480, q=0.9)
    return [air[i] * math.sin(math.pi * min(1.0, (i / n) * 1.15)) ** 1.6 for i in range(n)]


def _sound_sharpen(sr=SAMPLE_RATE):
    """Two passes of a whetstone. The config reloaded cleanly."""
    n = int(0.30 * sr)
    rng = random.Random(23)
    noise = [rng.uniform(-1.0, 1.0) for _ in range(n)]
    rasp = _svf_bandpass(noise, sr, 2600, 4200, q=1.3)
    ring = _partials(n, (3140, 4710), 0.09, sr)
    out = [0.18 * ring[i] for i in range(n)]
    span = 0.075 * sr
    for start, gain in ((0.0, 0.90), (0.115, 0.75)):
        s = int(start * sr)
        for i in range(s, min(n, s + int(span))):
            out[i] += gain * rasp[i] * math.sin(math.pi * (i - s) / span) ** 1.3
    return out


def _sound_growl(sr=SAMPLE_RATE):
    """The dragon is unimpressed. Something is wrong with the config."""
    n = int(0.50 * sr)
    rng = random.Random(31)
    dur = n / sr
    out = []
    phase = 0.0
    for i in range(n):
        t = i / sr
        f = 58.0 - 12.0 * (t / dur) + 3.5 * math.sin(2 * math.pi * 5.5 * t)
        phase += 2.0 * math.pi * f / sr
        saw = 2.0 * ((phase / (2.0 * math.pi)) % 1.0) - 1.0
        env = min(1.0, t / 0.05) * math.exp(-t / 0.30)
        out.append((0.80 * saw + 0.25 * rng.uniform(-1.0, 1.0)) * env)
    return _lowpass(out, sr, 700)


SOUND_BUILDERS = {
    "slay": _sound_slay,
    "graze": _sound_graze,
    "whiff": _sound_whiff,
    "sharpen": _sound_sharpen,
    "growl": _sound_growl,
}
# if WAV playback is unavailable for any reason, fall back to the PC speaker
FALLBACK_BEEP = {
    "slay": (1150, 70), "graze": (620, 110), "whiff": (380, 180),
    "sharpen": (900, 60), "growl": (300, 250),
}


def _wav_bytes(samples, sr=SAMPLE_RATE) -> bytes:
    pcm = array("h", (int(max(-1.0, min(1.0, s)) * 32767) for s in samples))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _normalise(samples, volume: float):
    peak = max((abs(s) for s in samples), default=0.0) or 1e-9
    g = 0.92 / peak
    return [math.tanh(s * g * 1.2) * volume for s in samples]


class SoundBank:
    """Renders each sound once, then plays it from memory off the kill path."""

    def __init__(self, enabled: bool = True, volume: float = 0.7):
        self.enabled = bool(enabled)
        self.volume = max(0.0, min(1.0, float(volume)))
        self._wav: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def prerender(self):
        if self.enabled:
            threading.Thread(target=self._render_all, name="sound-render",
                             daemon=True).start()

    def _render_all(self):
        t0 = time.perf_counter()
        for name in SOUND_BUILDERS:
            try:
                self._get(name)
            except Exception:
                log.debug("could not render sound %r", name, exc_info=True)
        log.debug("rendered %d sounds in %.0f ms",
                  len(self._wav), (time.perf_counter() - t0) * 1000)

    def _get(self, name: str) -> bytes:
        with self._lock:
            cached = self._wav.get(name)
        if cached is not None:
            return cached
        data = _wav_bytes(_normalise(SOUND_BUILDERS[name](), self.volume))
        with self._lock:
            self._wav[name] = data
        return data

    def play(self, name: str):
        if not self.enabled:
            return

        def _go():
            try:
                import winsound
                # synchronous inside this throwaway thread: SND_MEMORY needs the
                # buffer to stay alive for the duration, and nothing waits on us
                winsound.PlaySound(self._get(name), winsound.SND_MEMORY)
            except Exception:
                try:
                    import winsound
                    winsound.Beep(*FALLBACK_BEEP.get(name, (900, 80)))
                except Exception:
                    pass

        threading.Thread(target=_go, daemon=True).start()


class Slayer:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.settings: dict = dict(DEFAULT_SETTINGS)
        self.mappings: list[Mapping] = []
        self.engine: KillEngine | None = None
        self.joy: JoystickWatcher | None = None
        self.keys: HotkeyWatcher | None = None
        self.tray = None
        self.sounds = SoundBank()
        self.elevated = is_elevated()
        self._lock = threading.Lock()
        self._flash: threading.Timer | None = None

    @property
    def privilege_text(self) -> str:
        return "elevated" if self.elevated else "standard user"

    def _title(self) -> str:
        return f"{APP_NAME} -- armed, {self.privilege_text}"

    # -- lifecycle -----------------------------------------------------------------

    def load(self):
        self.settings, self.mappings = load_config(self.config_path)
        logging.getLogger(APP_NAME).setLevel(
            getattr(logging, str(self.settings["log_level"]).upper(), logging.INFO))
        self.sounds = SoundBank(enabled=bool(self.settings["sounds"]),
                                volume=float(self.settings["volume"]))
        self.sounds.prerender()

    def start(self):
        patterns = {t.value.lower() for m in self.mappings if m.enabled
                    for t in m.targets if t.type == "process" and t.value}
        self.engine = KillEngine(
            patterns,
            refresh_ms=int(self.settings["handle_refresh_ms"]),
            allow_system=bool(self.settings["allow_system"]),
            use_cache=bool(self.settings["handle_cache"]),
        )
        self.engine.start()
        self.joy = JoystickWatcher(self.mappings, self.fire, int(self.settings["poll_hz"]))
        self.joy.start()
        self.keys = HotkeyWatcher(self.mappings, self.fire)
        self.keys.start()
        # don't report "armed" until the combos are actually claimed, otherwise a
        # reload can look finished while the hotkeys are still dead
        if not self.keys.ready.wait(3.0):
            log.warning("hotkey registration is taking unusually long")
        enabled = [m for m in self.mappings if m.enabled]
        log.info("armed with %d mapping(s):", len(enabled))
        for m in enabled:
            log.info("   %-28s %s  ->  %s", m.name, m.trigger_text, m.action_text)

    def stop(self):
        # join, don't just signal: start() re-registers the same hotkeys, and if the
        # old thread still holds them RegisterHotKey fails and the combos go dead
        if self.joy:
            self.joy.stop()
        if self.keys:
            self.keys.stop()
        if self.engine:
            self.engine.stop()
        self.joy = self.keys = self.engine = None

    def reload(self, _icon=None, _item=None):
        with self._lock:
            try:
                settings, mappings = load_config(self.config_path)
            except Exception as exc:
                log.error("reload failed, keeping previous config: %s", exc)
                self.sounds.play("growl")
                return

            previous = (self.settings, self.mappings)
            try:
                self.stop()
                self.settings, self.mappings = settings, mappings
                self.sounds = SoundBank(enabled=bool(settings["sounds"]),
                                        volume=float(settings["volume"]))
                self.sounds.prerender()
                self.start()
            except Exception:
                # never leave the user disarmed because a reload went wrong
                log.exception("reload failed while restarting -- rolling back")
                self.settings, self.mappings = previous
                try:
                    self.stop()
                    self.start()
                except Exception:
                    log.exception("rollback failed -- ps-slayer is now disarmed")
                self.sounds.play("growl")
                return

            log.info("config reloaded")
            self.sounds.play("sharpen")
            if self.tray is not None:
                self.tray.menu = self._menu()
                self.tray.update_menu()

    # -- the actual kill -----------------------------------------------------------

    def fire(self, m: Mapping, source: str):
        engine = self.engine
        if engine is None:
            return
        # resolve the focused window up front, so a foreground target still means
        # "what was in front when I hit the button" even if it is listed last
        fg_pid = None
        if any(t.type == "foreground" for t in m.targets):
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                pid = wt.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                fg_pid = pid.value

        t0 = time.perf_counter()
        try:
            killed, notes = engine.kill_targets(m.targets, fg_pid)
        except Exception:
            log.exception("%s: kill failed", m.name)
            self.sounds.play("growl")
            return
        dt_us = (time.perf_counter() - t0) * 1e6

        detail = ", ".join(notes) if notes else "no targets"
        hit_all = killed > 0 and not any(
            (": none running" in n) or (": no match" in n) or ("nothing focused" in n)
            or ("refused" in n) for n in notes)
        if killed:
            log.info("%s [%s] killed %d process(es) across %d target(s) in %.0f us  [%s]",
                     m.name, source, killed, len(m.targets), dt_us, detail)
        else:
            log.warning("%s [%s] nothing killed  [%s]", m.name, source, detail)

        self.sounds.play("slay" if hit_all else "graze" if killed else "whiff")
        self._flash_icon("slay" if killed else "miss",
                         f"{m.name}: {killed} killed" if killed else f"{m.name}: nothing to kill")

    def _flash_icon(self, mood: str, tooltip: str):
        """Blade goes red on a kill, grey on a whiff, then back to steel."""
        tray = self.tray
        if tray is None:
            return
        try:
            tray.icon = sword_icon(mood)
            tray.title = f"{APP_NAME} -- {tooltip}"
        except Exception:
            return
        if self._flash is not None:
            self._flash.cancel()

        def _restore():
            try:
                tray.icon = sword_icon("idle")
                tray.title = self._title()
            except Exception:
                pass

        self._flash = threading.Timer(0.9, _restore)
        self._flash.daemon = True
        self._flash.start()

    # -- tray ----------------------------------------------------------------------

    def _menu(self):
        import pystray
        enabled = [m for m in self.mappings if m.enabled]

        def open_config(_i=None, _it=None):
            os.startfile(str(self.config_path))  # noqa: S606

        def open_log(_i=None, _it=None):
            os.startfile(str(LOG_DIR / "ps-slayer.log"))  # noqa: S606

        def quit_app(icon, _item=None):
            log.info("quit requested from tray")
            self.stop()
            icon.stop()

        def make_test(m: Mapping):
            return pystray.MenuItem(m.name, lambda _i=None, _it=None: self.fire(m, "tray test"))

        mapping_items = [
            pystray.MenuItem(m.name, pystray.Menu(
                pystray.MenuItem(f"trigger: {m.trigger_text}", None, enabled=False),
                pystray.Menu.SEPARATOR,
                *[pystray.MenuItem(f"kill {t}", None, enabled=False) for t in m.targets],
            ))
            for m in enabled
        ] or [pystray.MenuItem("(no mappings)", None, enabled=False)]

        privilege = ("Elevated -- admin-owned processes are killable"
                     if self.elevated else
                     "Standard user -- cannot kill admin-owned processes, and "
                     "hotkeys are blocked while an elevated window is focused")

        return pystray.Menu(
            pystray.MenuItem(f"{APP_NAME} {__version__} -- armed ({len(enabled)})",
                             None, enabled=False),
            pystray.MenuItem(privilege, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Mappings", pystray.Menu(*mapping_items)),
            pystray.MenuItem("Test fire", pystray.Menu(*[make_test(m) for m in enabled]))
            if enabled else pystray.MenuItem("Test fire", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Reload config", self.reload),
            pystray.MenuItem("Open config file", open_config),
            pystray.MenuItem("Open log", open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", quit_app),
        )

    def run_tray(self):
        import pystray

        self.tray = pystray.Icon(APP_NAME, sword_icon("idle"), self._title(), self._menu())
        self.tray.run()


# --------------------------------------------------------------------------------------
# CLI helpers
# --------------------------------------------------------------------------------------

def setup_logging(level: str = "INFO", console: bool = True):
    LOG_DIR.mkdir(exist_ok=True)
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_DIR / "ps-slayer.log", maxBytes=1_000_000,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if console and sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(sh)


def cmd_list_devices() -> int:
    pygame = _init_sdl()
    n = pygame.joystick.get_count()
    if not n:
        print("No controllers detected. Is the wheel plugged in and powered?")
        return 1
    print(f"{n} controller(s) detected:\n")
    for i in range(n):
        j = pygame.joystick.Joystick(i)
        j.init()
        print(f"  [{i}] {j.get_name()}")
        print(f"      guid    = {j.get_guid()}")
        print(f"      buttons = {j.get_numbuttons()}   axes = {j.get_numaxes()}"
              f"   hats = {j.get_numhats()}")
        print()
    print("In config.toml set  device = \"*part of the name*\"  (or guid = \"...\").")
    print("Run  --learn  to find button numbers.")
    return 0


def cmd_learn() -> int:
    pygame = _init_sdl()
    if not pygame.joystick.get_count():
        print("No controllers detected.")
        return 1
    joys = []
    for i in range(pygame.joystick.get_count()):
        j = pygame.joystick.Joystick(i)
        j.init()
        joys.append(j)
    print("Press wheel buttons -- indices appear below. Ctrl+C to stop.\n")
    prev: dict[int, set[int]] = {i: set() for i in range(len(joys))}
    try:
        while True:
            pygame.event.pump()
            for idx, j in enumerate(joys):
                now = {b for b in range(j.get_numbuttons()) if j.get_button(b)}
                for b in sorted(now - prev[idx]):
                    held = "+".join(str(x) for x in sorted(now))
                    print(f"  {j.get_name()[:38]:38}  button {b:>3}   held: [{held}]")
                prev[idx] = now
            time.sleep(0.004)
    except KeyboardInterrupt:
        print("\nDone.")
    return 0


def cmd_sounds(volume: float) -> int:
    import winsound
    bank = SoundBank(volume=volume)
    meaning = {
        "slay": "every target died",
        "graze": "some died, some weren't running",
        "whiff": "nothing was there",
        "sharpen": "config reloaded",
        "growl": "something is wrong",
    }
    print(f"Playing all sounds at volume {volume}:\n")
    for name in SOUND_BUILDERS:
        t0 = time.perf_counter()
        data = bank._get(name)
        print(f"  {name:9} {meaning[name]:34} ({len(data)/2/SAMPLE_RATE:.2f}s, "
              f"rendered in {(time.perf_counter()-t0)*1000:.0f} ms)")
        winsound.PlaySound(data, winsound.SND_MEMORY)
        time.sleep(0.3)
    return 0


def cmd_icons() -> int:
    out = APP_DIR / "preview"
    out.mkdir(exist_ok=True)
    for mood in ICON_PALETTES:
        for size in (16, 32, 64, 256):
            sword_icon(mood, size).save(out / f"sword-{mood}-{size}.png")
    print(f"Wrote {len(ICON_PALETTES) * 4} icon previews to {out}")
    print("  idle = armed,  slay = just killed something,  miss = nothing to kill")
    return 0


def cmd_ico() -> int:
    """Multi-resolution .ico for the Start Menu / desktop shortcuts."""
    path = APP_DIR / "ps-slayer.ico"
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    sword_icon("idle", 256).save(path, format="ICO", sizes=sizes)
    print(f"wrote {path} ({len(sizes)} resolutions)")
    return 0


def cmd_is_running() -> int:
    """
    Exit 0 if an instance is live, 1 if not.

    Asks the mutex rather than scanning processes, because Win32_Process hides the
    command line and image path of a higher-integrity process -- so an elevated
    ps-slayer is invisible to any unelevated scan looking for 'ps_slayer.py'.
    """
    running = not single_instance()
    print("running" if running else "not running")
    return 0 if running else 1


def cmd_check(path: Path) -> int:
    try:
        settings, mappings = load_config(path)
    except Exception as exc:
        print(f"CONFIG ERROR: {exc}")
        return 1
    print(f"OK -- {len(mappings)} mapping(s)\n")
    for m in mappings:
        flag = "ON " if m.enabled else "off"
        print(f" [{flag}] {m.name}")
        print(f"     trigger: {m.trigger_text}")
        if len(m.targets) == 1:
            print(f"     kills  : {m.targets[0]}")
        else:
            print(f"     kills  : {len(m.targets)} targets")
            for t in m.targets:
                print(f"              - {t}")
    print(f"\nsettings: {settings}")
    return 0


def cmd_test(path: Path, name: str) -> int:
    settings, mappings = load_config(path)
    match = next((m for m in mappings if m.name.lower() == name.lower()), None)
    if match is None:
        print(f"No mapping named {name!r}. Available: "
              + ", ".join(repr(m.name) for m in mappings))
        return 1
    app = Slayer(path)
    app.settings, app.mappings = settings, mappings
    app.engine = KillEngine(set(), allow_system=bool(settings["allow_system"]), use_cache=False)
    print(f"Firing {match.name!r}: {match.action_text}")
    app.fire(match, "cli test")
    return 0


_SINGLETON = None  # kept for the life of the process; the mutex dies with it


def single_instance() -> bool:
    """
    True if no other instance holds the mutex.

    Fails open on purpose: if we cannot tell, we start anyway. Refusing to arm
    because of an ambiguous handle check is far worse than two instances.

    CreateMutexW does NOT reset the thread's last-error on success, so reading it
    without clearing first returns whatever some earlier Win32 call left behind --
    which is reliably 183 (ERROR_ALREADY_EXISTS) during interpreter startup, and
    made every launch think it was a duplicate.
    """
    global _SINGLETON
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, True, f"Local\\{APP_NAME}-singleton")
    err = ctypes.get_last_error()
    if not handle:
        log.warning("could not create the single-instance mutex (error %d); starting anyway", err)
        return True
    _SINGLETON = handle
    return err != ERROR_ALREADY_EXISTS


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog=APP_NAME, description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--list-devices", action="store_true", help="print detected controllers")
    ap.add_argument("--learn", action="store_true", help="live view of button presses")
    ap.add_argument("--check", action="store_true", help="validate the config and exit")
    ap.add_argument("--sounds", action="store_true", help="play every sound once")
    ap.add_argument("--icons", action="store_true", help="write tray icon previews to .\\preview")
    ap.add_argument("--ico", action="store_true", help="write ps-slayer.ico for shortcuts")
    ap.add_argument("--is-running", action="store_true",
                    help="exit 0 if an instance is already running, 1 if not")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    ap.add_argument("--test", metavar="NAME", help="fire one mapping by name, once")
    ap.add_argument("--no-tray", action="store_true", help="run without the tray icon")
    args = ap.parse_args(argv)

    if args.list_devices:
        return cmd_list_devices()
    if args.learn:
        return cmd_learn()
    if args.icons:
        return cmd_icons()
    if args.ico:
        return cmd_ico()
    if args.is_running:
        return cmd_is_running()
    if args.check:
        return cmd_check(args.config)
    if args.sounds:
        vol = 0.7
        try:
            vol = float(load_config(args.config)[0]["volume"])
        except Exception:
            pass
        return cmd_sounds(vol)

    setup_logging()
    if args.test:
        enable_debug_privilege()
        return cmd_test(args.config, args.test)

    if not single_instance():
        log.warning("%s is already running -- exiting", APP_NAME)
        return 0

    log.info("starting %s %s (pid %d, elevated=%s)",
             APP_NAME, __version__, SELF_PID, is_elevated())
    if enable_debug_privilege():
        log.info("SeDebugPrivilege enabled")
    if not is_elevated():
        log.warning("running as a standard user: admin-owned processes cannot be killed, "
                    "and keyboard hotkeys will not fire while an elevated window has focus. "
                    "Start from the Start Menu shortcut, or run install.ps1.")

    app = Slayer(args.config)
    try:
        app.load()
    except Exception as exc:
        log.error("config error: %s", exc)
        SoundBank().play("growl")
        time.sleep(1.0)  # let the growl finish before the process exits
        return 1

    app.start()
    try:
        if app.settings.get("tray", True) and not args.no_tray:
            app.run_tray()          # blocks until Quit
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
