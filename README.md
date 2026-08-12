# ps-slayer

Kill a named program instantly from a button (or button combo) on your sim wheel,
or from a keyboard combo. Runs invisibly in the background, starts itself at logon.

Measured on this machine: **~0.6 ms** from trigger to `TerminateProcess`, using the
pre-opened handle cache. The whole loop — button press detected, process gone — is
under 3 ms worst case.

---

## Install

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Accept the UAC prompt. It installs like a normal Windows app:

- a private `.venv` with `pygame` / `pystray` / `pillow`
- **Start Menu → ps-slayer** with *ps-slayer*, *Stop ps-slayer*, *Edit config*,
  *Find wheel buttons*, *View log*
- a **desktop shortcut** (skip with `-NoDesktop`)
- an entry in **Settings → Installed apps**, uninstallable from there
- a Task Scheduler job that starts it at logon, elevated, with no console window

A sword appears in the system tray. **Self-test:** open Notepad, press
`Ctrl+Alt+N` — it should vanish with a blade ring, and the tray sword flashes
red for a moment.

The Start Menu and desktop shortcuts don't launch Python directly — they run
`schtasks /run /tn ps-slayer`. Because the task is registered *Run with highest
privileges*, that starts ps-slayer **elevated with no UAC prompt**, every time.
You'll see a brief minimised console blip from `schtasks`; that's it exiting.

Flags:

| | |
|---|---|
| `-NoTask` | No autostart and no scheduled task. Needs no admin, but then it runs as a standard user |
| `-NoDesktop` | Start Menu only |

To remove: `powershell -ExecutionPolicy Bypass -File .\uninstall.ps1`, or use
Settings → Installed apps. Your `config.toml`, logs and `.venv` are left in place.

---

## Elevation — what it costs you

Elevation is not cosmetic. Launching by hand from an ordinary terminal:

```powershell
.\.venv\Scripts\python.exe ps_slayer.py     # standard user
```

runs as a standard user, because a process inherits its parent's token. Your
account being an admin doesn't change that — an unelevated process gets the
*filtered* token, which doesn't contain `SeDebugPrivilege` at all.

**Unelevated it still kills** anything running as you at normal integrity: the
sim, iRacing UI, SimHub, Discord, OBS. That covers most of what you'd bind.

**It cannot kill** anything started as admin, or running as SYSTEM or another
user. You get the whiff sound and `none running` in the log.

**And the keyboard hotkeys go dead while an elevated window has focus** — UIPI
blocks a lower-integrity process from receiving them. Wheel buttons are
unaffected; those come from polling the device, not the hotkey queue. If your
panic key ever seems ignored, check what's focused.

Where to see which you've got:

- the tray tooltip: `ps-slayer -- armed, elevated` / `..., standard user`
- the second line of the tray menu spells out the consequence
- the log at startup: `elevated=True` plus a `SeDebugPrivilege enabled` line

To start it elevated by hand, from a normal PowerShell:

```powershell
cd C:\Users\mitch\Desktop\Scripts\ps-slayer
Start-Process .\.venv\Scripts\pythonw.exe -ArgumentList 'ps_slayer.py' -WorkingDirectory $PWD -Verb RunAs
```

Accept the UAC prompt. Swap in `python.exe` if you want a console window. The
Start Menu shortcut does the same thing without the prompt, which is the reason
to install rather than launch by hand.

---

## Binding wheel buttons

Your wheel's buttons are numbered, and the numbers aren't printed on it. Find them:

```powershell
.\.venv\Scripts\python.exe ps_slayer.py --learn
```

Press a button, read the index. Combos show up as `held: [12+13]`.
(If nothing appears, run `--list-devices` first to confirm the wheel is seen.)

Detected on this PC:

| Device | GUID | Buttons |
|---|---|---|
| Ascher Racing Artura ULTIMATE | `0300bd9aee3500005410000000000000` | 128 |
| SC-Link Hub | `0300c144d0160000660d000000000000` | 128 |
| vJoy Device ×3 | `0300f80034120000adbe000000000000` | 32–64 |

Then edit `config.toml`, set the real numbers, set `enabled = true`, and use the
tray icon → **Reload config**. No restart needed.

---

## Config reference

Each `[[mapping]]` is one trigger → one target.

### Trigger

| Key | Meaning |
|---|---|
| `buttons = [12, 13]` | Wheel buttons, all held simultaneously |
| `device = "*ascher*"` | Which controller (glob on its name). Omit = any controller |
| `guid = "0300bd9a..."` | Exact controller, instead of `device` |
| `hotkey = "ctrl+alt+k"` | Keyboard combo. `ctrl` / `alt` / `shift` / `win` + a key |

Hotkey keys: `a`–`z`, `0`–`9`, `f1`–`f24`, `num0`–`num9`, `esc`, `space`, `home`,
`end`, `pgup`, `pgdn`, `left`/`right`/`up`/`down`, `del`, `ins`, `tab`, `enter`,
and the punctuation keys.

A mapping can have both — same kill from the wheel or the keyboard.

### Target — one program or many

| | |
|---|---|
| `type = "process"` | `target = "iRacingUI.exe"` — exe name. Globs work: `"chrome*.exe"` |
| `type = "window"` | `target = "*telemetry*"` — visible window title, substring or glob |
| `type = "foreground"` | No target. Kills whatever is focused right now |

A single mapping can take out any number of programs. Pass a list:

```toml
[[mapping]]
name    = "Nuke the overlays"
buttons = [16, 17]
type    = "process"
target  = ["SimHub.exe", "RaceLabApps.exe", "Discord.exe", "obs64.exe"]
tree    = true
hold_ms = 400
```

To mix types in one mapping, use `targets` with a table per entry. Each entry may
override `type` and `tree`; whatever it omits falls back to the mapping's values:

```toml
[[mapping]]
name    = "Full teardown"
buttons = [18, 19]
tree    = true                     # default for every entry below
targets = [
  { type = "process", target = "iRacingSim64DX11.exe", tree = false },
  { type = "process", target = "iRacingUI.exe" },
  { type = "window",  target = "*Telemetry*" },
  { type = "foreground" },
]
hold_ms = 750
```

`targets` also accepts bare strings (`targets = ["a.exe", "b.exe"]`), and `target`
and `targets` can both appear — everything is killed in the order written.

Targets that aren't running are not an error; they're reported in the log and you
hear the scrape instead of the clean ring. A `foreground` target resolves to
whatever was focused **when you pressed the button**, even if it's listed last.

### Options

| Key | Default | Meaning |
|---|---|---|
| `tree` | `false` | Also kill child processes (children die first, so nothing respawns) |
| `hold_ms` | `0` | Combo must be held this long. Use ~300–500 ms so an elbow mid-corner can't fire it |
| `cooldown_ms` | `400` | Ignore re-triggers inside this window |
| `enabled` | `true` | Turn a block off without deleting it |

### `[settings]`

| Key | Default | Meaning |
|---|---|---|
| `poll_hz` | `500` | Wheel polling rate. 500 Hz ≈ 2 ms worst-case detection, ~0.25% of one core |
| `handle_cache` | `true` | Keep kill handles pre-opened. This is what makes it sub-millisecond |
| `handle_refresh_ms` | `1000` | How often the cache rescans for new/dead processes |
| `sounds` | `true` | See below. `beep` is still accepted as the old name |
| `volume` | `0.7` | 0.0–1.0 |
| `tray` | `true` | Tray icon: reload, test-fire, open log, quit |
| `allow_system` | `false` | Lets you kill protected OS processes. Leave it off |

Validate any time: `.\.venv\Scripts\python.exe ps_slayer.py --check`

If two mappings share a name, the second is renamed (`... (2)`) with a warning
rather than refusing to start — a label typo should never disarm your panic key.

---

## Icon and sounds

The tray icon is a sword, and it tells you what just happened without you
having to look at a log:

| | |
|---|---|
| **Steel blade** | Armed and idle |
| **Blade flashes red** (~1 s) | Something just died. Tooltip shows the mapping and the count |
| **Blade flashes grey** | You swung at nothing |

See them at every size: `ps_slayer.py --icons` writes previews to `.\preview`.

The sounds are synthesized at startup from pure stdlib DSP — no audio library, no
`.wav` files on disk, nothing to go missing. Each one means something specific:

| Sound | | Meaning |
|---|---|---|
| **Ring** | a clean draw-cut into a bright metallic ring | Every target in the mapping died |
| **Scrape** | steel skidding off scales, with a bounce | Some died; others weren't running or were protected |
| **Whiff** | air, no contact | Nothing was there to kill |
| **Whetstone** | two rasps | Config reloaded cleanly |
| **Growl** | low, and displeased | Config is broken, or a kill threw |

Hear them all: `ps_slayer.py --sounds`. Adjust with `volume`, or set
`sounds = false` for silence. Playback happens on a throwaway thread, so audio
never delays a kill. If WAV playback is unavailable, it falls back to the PC
speaker automatically.

---

## How the fast path works

A background thread keeps an open `PROCESS_TERMINATE` handle for every process
matching a configured target, refreshed once a second. When you hit the button
there's no enumeration and no `OpenProcess` — just `TerminateProcess(handle, 1)`,
which is a kernel call that returns immediately and doesn't wait for the process
to unwind. Keyboard triggers come through `RegisterHotKey`, so the OS dispatches
straight to us rather than us polling.

For a multi-program mapping, every handle is collected under a single lock
acquisition first, then all the `TerminateProcess` calls fire back to back with
nothing in between — so the programs go down as close to simultaneously as the
scheduler allows, not one-at-a-time with lookup work between each.

If a target launched since the last cache refresh, it falls back to a live
snapshot scan (~10 ms) — still instant to a human, and the log tells you which
path each target used. Window and foreground targets share one snapshot rather
than taking one each.

Measured here: 4 processes across 2 targets, all cached, in **2.2 ms**. Adding a
window-title target pushes it to ~15 ms because `EnumWindows` is unavoidable.

`SeDebugPrivilege` is enabled at startup, and the scheduled task runs elevated,
so admin-owned processes are killable.

---

## Safety

- Critical OS processes (`csrss`, `winlogon`, `lsass`, `svchost`, `dwm`, …) are
  refused unless you set `allow_system = true`.
- ps-slayer will not kill itself.
- Only one instance runs at a time (named mutex).
- Every kill is logged to `logs\ps-slayer.log` with the process count, code path
  and microsecond timing.

---

## Commands

```powershell
.\.venv\Scripts\python.exe ps_slayer.py --list-devices   # controllers + GUIDs
.\.venv\Scripts\python.exe ps_slayer.py --learn          # live button indices
.\.venv\Scripts\python.exe ps_slayer.py --check          # validate config.toml
.\.venv\Scripts\python.exe ps_slayer.py --sounds         # play every sound once
.\.venv\Scripts\python.exe ps_slayer.py --icons          # write icon previews
.\.venv\Scripts\python.exe ps_slayer.py --ico            # rebuild ps-slayer.ico
.\.venv\Scripts\python.exe ps_slayer.py --is-running     # exit 0 if an instance is live
.\.venv\Scripts\python.exe ps_slayer.py --version
.\.venv\Scripts\python.exe ps_slayer.py --test "Name"    # fire one mapping once
.\.venv\Scripts\python.exe ps_slayer.py --no-tray        # run in a console
.\.venv\Scripts\python.exe selftest.py                   # regression check
```

`selftest.py` drives the tray menu callbacks, config reload, BOM handling, the
protected-process guard and a real kill against Notepad. Run it after editing
`ps_slayer.py` — the tray callbacks in particular are easy to break and awkward
to reach by hand.

Note: the background daemon holds the single-instance lock, so quit it from the
tray before running the daemon manually. `--learn`, `--check` and `--list-devices`
work regardless.

Use `--is-running` rather than looking for the process in Task Manager: an
elevated instance hides its command line and image path from any unelevated
query, so a process scan will tell you it isn't there when it is. `--is-running`
asks the mutex, which doesn't care about integrity levels.

---

## Troubleshooting

**Wheel buttons do nothing.** Check `logs\ps-slayer.log` lists your controller at
startup. If the mapping shows `any wheel` when you meant the Ascher, your `device`
glob didn't match — use `guid` instead. Confirm indices with `--learn`.

**Hotkey does nothing.** The log will say `hotkey ... is already taken by another
app`. Pick a different combo.

**Whiff sound, nothing killed.** The target name is wrong. Check the exact exe
name in Task Manager → Details.

**Scrape sound.** Some targets in the mapping died and some weren't running (or
were refused as protected). The log line lists each target and its count.

**Config changes ignored.** Tray → Reload config. If the config has a syntax error
it keeps the old one, growls, and logs why.

**It didn't start after reboot.** `Get-ScheduledTask ps-slayer` should show `Ready`
or `Running`. The task waits 15 s after logon so the wheel can enumerate.
