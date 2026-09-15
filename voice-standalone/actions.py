"""
Action executors — one function per skill, dispatched from main.py.

All functions follow the same signature contract:
    def skill_name(**kwargs) -> None

The DISPATCH dict maps skill name → lambda that unpacks kwargs and calls
the executor, keeping the call-site in main.py a clean O(1) lookup.

COSMIC / Wayland notes for Category B executors:
  COSMIC (System76's Wayland compositor as of 2026-09) does not expose any
  D-Bus or Wayland-protocol interface for per-window operations (close,
  focus, list, maximize, minimize-all, workspace-switch).  The CosmicComp
  D-Bus service (/com/system76/CosmicComp) only exposes a raw emulated-input
  socket (Ei) — not usable for window management.  X11 tools (wmctrl,
  xdotool) are non-functional under COSMIC's native Wayland session.
  Affected skills are marked COSMIC_LIMITATION and are no-ops that print a
  clear message; the LLM fallback layer can still attempt these if desired.
"""

import datetime
import json
import os
import re
import socket
import subprocess
import urllib.parse
import urllib.request


# ===========================================================================
# ─── Category A — System & power control ───────────────────────────────────
# ===========================================================================

# ── A01  volume_up ──────────────────────────────────────────────────────────

def volume_up() -> None:
    """Increase default sink volume by 5%."""
    subprocess.run(
        ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+5%"], check=False
    )


# ── A02  volume_down ────────────────────────────────────────────────────────

def volume_down() -> None:
    """Decrease default sink volume by 5%."""
    subprocess.run(
        ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-5%"], check=False
    )


# ── A03  mute ───────────────────────────────────────────────────────────────

def mute() -> None:
    """Mute the default audio sink."""
    subprocess.run(
        ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1"], check=False
    )


# ── A04  unmute ─────────────────────────────────────────────────────────────

def unmute() -> None:
    """Unmute the default audio sink."""
    subprocess.run(
        ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"], check=False
    )


# ── A05  set_volume ─────────────────────────────────────────────────────────

def set_volume(percent: int) -> None:
    percent = max(0, min(100, int(percent)))
    subprocess.run(
        ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%"], check=False
    )


# ── A06  brightness_up ──────────────────────────────────────────────────────

def brightness_up() -> None:
    """Increase screen brightness by 10% via brightnessctl."""
    subprocess.run(["brightnessctl", "set", "+10%"], check=False)


# ── A07  brightness_down ────────────────────────────────────────────────────

def brightness_down() -> None:
    """Decrease screen brightness by 10% via brightnessctl."""
    subprocess.run(["brightnessctl", "set", "10%-"], check=False)


# ── A08  set_brightness ─────────────────────────────────────────────────────

def set_brightness(percent: int) -> None:
    percent = max(0, min(100, int(percent)))
    subprocess.run(["brightnessctl", "set", f"{percent}%"], check=False)


# ── A09  toggle_dark_mode ───────────────────────────────────────────────────

def toggle_dark_mode(dark: bool) -> None:
    """Toggle dark / light theme by writing COSMIC's theme-mode config file.

    COSMIC stores the active theme mode in:
      ~/.config/cosmic/com.system76.CosmicTheme.Mode/v1/is_dark

    The file contains the RON literal  true  or  false  (no quotes).
    Writing it and sending SIGHUP to the cosmic-settings-daemon process causes
    it to re-read and broadcast the change — the same mechanism cosmic-settings
    itself uses.  This is confirmed against the live config file on this system.

    IMPORTANT: pkill's process-name match truncates at 15 characters (a Linux
    /proc/[pid]/comm limit), and "cosmic-settings-daemon" is 22 — a plain
    `pkill -HUP cosmic-settings-daemon` silently matches nothing. -f (match
    against the full command line instead of the truncated name) is required.
    Verified live: with -f, the daemon receives SIGHUP and survives with the
    same PID (confirmed via /proc/<pid>/status — SigCgt has bit 0 set, i.e.
    it explicitly catches SIGHUP rather than dying to the default disposition).
    """
    config_path = os.path.expanduser(
        "~/.config/cosmic/com.system76.CosmicTheme.Mode/v1/is_dark"
    )
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w") as f:
        f.write("true" if dark else "false")
    # Signal cosmic-settings-daemon to reload; ignore if not running
    subprocess.run(
        ["pkill", "-f", "-HUP", "cosmic-settings-daemon"], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


# ── A10  lock_screen ────────────────────────────────────────────────────────

def lock_screen() -> None:
    """Lock the screen via loginctl (works on any systemd-logind session)."""
    subprocess.run(["loginctl", "lock-session"], check=False)


# ── A11  shutdown ────────────────────────────────────────────────────────────

def shutdown() -> None:
    """Power off the system.  🔒 DANGEROUS — no confirm loop yet (Step 6)."""
    subprocess.run(["systemctl", "poweroff"], check=False)


# ── A12  restart ─────────────────────────────────────────────────────────────

def restart() -> None:
    """Reboot the system.  🔒 DANGEROUS — no confirm loop yet (Step 6)."""
    subprocess.run(["systemctl", "reboot"], check=False)


# ── A13  logout ──────────────────────────────────────────────────────────────

def logout() -> None:
    """Log out of the COSMIC session via its D-Bus interface.

    com.system76.CosmicSession exposes Exit() which ends the session cleanly,
    equivalent to clicking Log Out in the power menu.
    """
    subprocess.run(
        [
            "gdbus", "call", "--session",
            "--dest", "com.system76.CosmicSession",
            "--object-path", "/com/system76/CosmicSession",
            "--method", "com.system76.CosmicSession.Exit",
        ],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


# ── A14  sleep ───────────────────────────────────────────────────────────────

def sleep() -> None:
    """Suspend the system to RAM via systemctl."""
    subprocess.run(["systemctl", "suspend"], check=False)


# ── A15  screenshot ──────────────────────────────────────────────────────────

def screenshot() -> None:
    """Take an immediate full-screen screenshot using the COSMIC screenshot tool.

    cosmic-screenshot is the native COSMIC compositor screenshot utility.
    --interactive=true (the tool's own default) opens a portal UI and waits
    for the user to manually select a region before saving anything — wrong
    for a voice command, which should capture immediately. --interactive=false
    takes the shot right away. Verified live: saves to ~/Pictures/ directly,
    named Screenshot_<timestamp>.png, no hang, exit code 0.
    """
    subprocess.Popen(
        ["cosmic-screenshot", "--interactive=false", "--notify=true"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


# ── A16  start_recording ─────────────────────────────────────────────────────

def start_recording() -> None:
    """Start screen recording using wf-recorder (Wayland screen recorder).

    COSMIC_STATUS: wf-recorder uses the Wayland screencopy protocol which
    cosmic-comp supports (same mechanism cosmic-screenshot uses).
    If wf-recorder is not installed, falls back to printing an error.
    Recording is saved to ~/Videos/recording_<timestamp>.mp4.
    """
    try:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.expanduser(f"~/Videos/recording_{timestamp}.mp4")
        os.makedirs(os.path.expanduser("~/Videos"), exist_ok=True)
        subprocess.Popen(
            ["wf-recorder", "-f", out_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        print(f"[recording] Started → {out_path}  (say 'stop recording' to stop)")
    except FileNotFoundError:
        print("[recording] wf-recorder not installed. Install with: sudo apt install wf-recorder")


# ── A17  stop_recording ──────────────────────────────────────────────────────

def stop_recording() -> None:
    """Stop any running wf-recorder instance by sending SIGINT."""
    result = subprocess.run(
        ["pkill", "-INT", "wf-recorder"],
        check=False, capture_output=True
    )
    if result.returncode == 0:
        print("[recording] Stopped.")
    else:
        print("[recording] No active recording found.")


# ── A18  toggle_wifi ─────────────────────────────────────────────────────────

def toggle_wifi(state: str | None = None) -> None:
    """Enable, disable, or toggle Wi-Fi via nmcli.

    Args:
        state: 'on', 'off', or None (toggle).
    """
    if state is None:
        # Query current state then flip
        result = subprocess.run(
            ["nmcli", "radio", "wifi"], capture_output=True, text=True, check=False
        )
        current = result.stdout.strip().lower()
        state = "off" if current == "enabled" else "on"
    subprocess.run(["nmcli", "radio", "wifi", state], check=False)


# ── A19  toggle_bluetooth ────────────────────────────────────────────────────

def toggle_bluetooth(state: str | None = None) -> None:
    """Enable, disable, or toggle Bluetooth via rfkill.

    Args:
        state: 'on', 'off', or None (toggle current state).
    """
    if state is None:
        result = subprocess.run(
            ["rfkill", "list", "bluetooth"], capture_output=True, text=True, check=False
        )
        # "Soft blocked: yes" means currently OFF
        blocked = "soft blocked: yes" in result.stdout.lower()
        state = "unblock" if blocked else "block"
    else:
        state = "unblock" if state == "on" else "block"
    subprocess.run(["rfkill", state, "bluetooth"], check=False)


# ── A20  toggle_dnd ──────────────────────────────────────────────────────────

def toggle_dnd(state: str | None = None) -> None:
    """Toggle Do Not Disturb by writing COSMIC's notifications config file.

    COSMIC stores DnD config at:
      ~/.config/cosmic/com.system76.CosmicNotifications/v1/do_not_disturb

    The value is the RON boolean  true  (DnD on) or  false  (DnD off).
    """
    config_dir = os.path.expanduser(
        "~/.config/cosmic/com.system76.CosmicNotifications/v1"
    )
    config_path = os.path.join(config_dir, "do_not_disturb")

    current: bool = False
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                current = f.read().strip().lower() == "true"
        except OSError:
            pass

    if state is None:
        target = not current
    else:
        target = state == "on"

    os.makedirs(config_dir, exist_ok=True)
    with open(config_path, "w") as f:
        f.write("true" if target else "false")

    # NOT sending a reload signal here, unlike toggle_dark_mode. Checked
    # /proc/<pid>/status for the live cosmic-notifications process: its
    # SigCgt bitmask has bit 0 (SIGHUP) UNSET, meaning it does not catch
    # SIGHUP — the default disposition for an uncaught SIGHUP is process
    # termination. `pkill -HUP cosmic-notifications` (22 chars, truncates
    # past pkill's 15-char comm match, so it silently does nothing today)
    # must NOT be "fixed" with -f — that would actually kill the live
    # daemon instead of reloading it. The config file is written correctly;
    # it takes effect on the daemon's next natural restart/login, and
    # DND has no other verified-safe way to apply immediately on this
    # COSMIC version.
    print(f"[dnd] Do Not Disturb {'ON' if target else 'OFF'} "
          f"(saved; takes effect on next COSMIC session restart)")


# ── A21  toggle_airplane ─────────────────────────────────────────────────────

def toggle_airplane(state: str | None = None) -> None:
    """Toggle airplane mode (all radios off/on) via nmcli + rfkill.

    nmcli radio all on/off handles Wi-Fi + mobile data.
    rfkill block/unblock all handles Bluetooth as well.
    """
    if state is None:
        result = subprocess.run(
            ["nmcli", "radio", "all"], capture_output=True, text=True, check=False
        )
        # Output: "WIFI-HW  WIFI     WWAN-HW  WWAN"  then values on next line
        lines = result.stdout.strip().splitlines()
        if len(lines) >= 2:
            values = lines[1].split()
            # If wifi is already disabled, airplane is on → toggle to off
            state = "off" if (values[1].lower() == "disabled") else "on"
        else:
            state = "on"  # default to enabling airplane if uncertain

    if state == "on":
        subprocess.run(["nmcli", "radio", "all", "off"], check=False)
        subprocess.run(["rfkill", "block", "all"], check=False)
        print("[airplane] Airplane mode ON — all radios disabled")
    else:
        subprocess.run(["nmcli", "radio", "all", "on"], check=False)
        subprocess.run(["rfkill", "unblock", "all"], check=False)
        print("[airplane] Airplane mode OFF — all radios enabled")


# ── A22  toggle_night_light ──────────────────────────────────────────────────

def toggle_night_light(state: str | None = None) -> None:
    """Toggle night light / warm colour temperature.

    COSMIC_LIMITATION: COSMIC does not currently expose night-light or colour
    temperature control via any accessible D-Bus interface or CLI tool.
    The gsettings schema org.gnome.settings-daemon.plugins.color exists in
    the schema registry but the GSD colour plugin does not run under COSMIC's
    compositor, so writing to it has no effect.
    No wlr-gamma-control Wayland protocol is exposed by cosmic-comp today.
    This executor is a documented no-op pending a COSMIC stable API.
    Tracking: https://github.com/pop-os/cosmic-comp (check for night-light
    support in future releases).
    """
    print(
        "[night_light] COSMIC does not currently support night-light control "
        "via any accessible API. This feature is unavailable until COSMIC adds "
        "a D-Bus or CLI interface for colour temperature adjustment."
    )


# ── A23  battery_status ──────────────────────────────────────────────────────

def battery_status() -> None:
    """Report battery status via upower."""
    result = subprocess.run(
        ["upower", "-i", "/org/freedesktop/UPower/devices/battery_BAT0"],
        capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        # Try to find the battery path dynamically
        enum = subprocess.run(
            ["upower", "--enumerate"], capture_output=True, text=True, check=False
        )
        for line in enum.stdout.splitlines():
            if "battery" in line.lower():
                result = subprocess.run(
                    ["upower", "-i", line.strip()],
                    capture_output=True, text=True, check=False
                )
                break

    # Extract key fields for a concise report
    info: dict[str, str] = {}
    for line in result.stdout.splitlines():
        for key in ("state", "percentage", "time to empty", "time to full"):
            if line.strip().lower().startswith(key):
                val = line.split(":", 1)[-1].strip()
                info[key] = val
                break

    if info:
        parts = []
        if "percentage" in info:
            parts.append(f"{info['percentage']}")
        if "state" in info:
            parts.append(f"({info['state']})")
        if "time to empty" in info:
            parts.append(f"— {info['time to empty']} remaining")
        elif "time to full" in info:
            parts.append(f"— {info['time to full']} to full")
        print(f"[battery] {' '.join(parts)}")
    else:
        print("[battery] Could not read battery information.")


# ── A24  toggle_kbd_backlight ────────────────────────────────────────────────

def toggle_kbd_backlight(state: str | None = None) -> None:
    """Toggle ASUS keyboard backlight via sysfs.

    The keyboard backlight is controlled via:
      /sys/class/leds/asus::kbd_backlight/brightness
    Max value: 3 (confirmed on this system).
    The sysfs file is writable by the 'input' group; the user must be in
    the 'input' group (or use udev rules) for this to work without sudo.
    """
    sysfs_path = "/sys/class/leds/asus::kbd_backlight/brightness"
    max_path = "/sys/class/leds/asus::kbd_backlight/max_brightness"

    try:
        with open(max_path) as f:
            max_val = int(f.read().strip())
    except (OSError, ValueError):
        max_val = 3  # Confirmed max on this ASUS system

    try:
        with open(sysfs_path) as f:
            current = int(f.read().strip())
    except (OSError, ValueError):
        current = 0

    if state == "on":
        target = max_val
    elif state == "off":
        target = 0
    else:
        # Toggle: off if on, on (full) if off
        target = 0 if current > 0 else max_val

    try:
        with open(sysfs_path, "w") as f:
            f.write(str(target))
        print(f"[kbd_backlight] {'ON' if target > 0 else 'OFF'} (brightness={target})")
    except OSError as e:
        print(f"[kbd_backlight] Cannot write sysfs: {e}. "
              "Ensure user is in the 'input' group or add a udev rule.")


# ── A25  toggle_mic_mute ─────────────────────────────────────────────────────

def toggle_mic_mute(state: str | None = None) -> None:
    """Mute, unmute, or toggle the default microphone (audio source) via pactl.

    Args:
        state: 'mute', 'unmute', or None (toggle).
    """
    if state == "mute":
        target = "1"
    elif state == "unmute":
        target = "0"
    else:
        target = "toggle"
    subprocess.run(
        ["pactl", "set-source-mute", "@DEFAULT_SOURCE@", target], check=False
    )
    print(f"[mic] Microphone {'muted' if target == '1' else 'unmuted' if target == '0' else 'toggled'}")


# ── A26  toggle_display ──────────────────────────────────────────────────────

def toggle_display(mode: str | None = None) -> None:
    """Toggle, mirror, or extend an external display using cosmic-randr.

    cosmic-randr is COSMIC's native display management CLI tool; it uses the
    wlr-output-management Wayland protocol which cosmic-comp supports.

    If no mode is specified, cycles through available outputs and toggles.
    For mirror/extend, we use cosmic-randr to configure the outputs.
    """
    # List current outputs
    result = subprocess.run(
        ["cosmic-randr"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        print("[display] cosmic-randr not available or failed.")
        return

    if mode == "mirror":
        print("[display] Mirror mode: use COSMIC Settings → Display for mirroring. "
              "cosmic-randr CLI does not yet expose a one-shot mirror command.")
    elif mode == "extend":
        print("[display] Extend mode: use COSMIC Settings → Display for extending. "
              "cosmic-randr CLI does not yet expose a one-shot extend command.")
    else:
        # Without a specific mode, open the COSMIC display settings page
        subprocess.Popen(
            ["cosmic-settings", "displays"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        print("[display] Opened COSMIC display settings.")


# ===========================================================================
# ─── Category B — App & window management ──────────────────────────────────
# ===========================================================================

# ── B01  open_app ────────────────────────────────────────────────────────────

def open_app(app_name: str) -> None:
    """Launch a desktop application by its desktop_id via gtk-launch."""
    result = subprocess.run(
        ["gtk-launch", app_name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    if result.returncode != 0:
        # Fallback: try running the desktop_id as a bare command. Not every
        # desktop_id is an executable on PATH (flatpak/snap wrappers, NoDisplay
        # entries, TryExec mismatches) — that raises FileNotFoundError, which
        # must not crash the whole assistant over one bad app name.
        try:
            subprocess.Popen(
                [app_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except FileNotFoundError:
            print(f"[open_app] Could not launch '{app_name}' — not found on PATH.")


# ── B02  close_app ──────────────────────────────────────────────────────────
# COSMIC_LIMITATION: cosmic-comp provides no D-Bus or Wayland-protocol API
# for closing a specific application window by name.  wmctrl and xdotool
# do not work in COSMIC's native Wayland session (no XWayland bridge).
# Tracking: https://github.com/pop-os/cosmic-comp

def close_app(app_name: str) -> None:
    print(
        f"[close_app] COSMIC LIMITATION: No window-management API available "
        f"to close '{app_name}' in COSMIC's native Wayland session. "
        f"Use force_quit to kill the process, or close the window manually."
    )


# ── B03  switch_to_app ──────────────────────────────────────────────────────
# COSMIC_LIMITATION: same — no window-focus API.

def switch_to_app(app_name: str) -> None:
    print(
        f"[switch_to_app] COSMIC LIMITATION: No window-focus API available "
        f"in COSMIC's native Wayland session. Cannot switch focus to '{app_name}'."
    )


# ── B04  list_open_apps ─────────────────────────────────────────────────────
# COSMIC_LIMITATION: cannot enumerate windows via compositor.
# Fallback: list running processes with GUI windows heuristically via ps.

def list_open_apps() -> None:
    print(
        "[list_open_apps] COSMIC LIMITATION: COSMIC's Wayland compositor does "
        "not expose an API to enumerate open windows. "
        "Showing running non-system processes as a heuristic:"
    )
    result = subprocess.run(
        ["ps", "-eo", "comm", "--no-headers"],
        capture_output=True, text=True, check=False
    )
    # Filter to plausibly user-launched processes (not kernel threads, not daemons)
    known_noise = {
        "ps", "bash", "sh", "python", "python3", "systemd", "dbus-daemon",
        "pipewire", "wireplumber", "Xwayland", "kworker", "ksoftirqd",
    }
    apps = sorted({
        line.strip() for line in result.stdout.splitlines()
        if line.strip() and line.strip() not in known_noise and not line.startswith("[")
    })
    print(f"[list_open_apps] Running: {', '.join(apps[:20])}"
          + (" …" if len(apps) > 20 else ""))


# ── B05  switch_workspace ───────────────────────────────────────────────────
# COSMIC_LIMITATION: no workspace-switch API on cosmic-comp via D-Bus or CLI.

def switch_workspace(number: int) -> None:
    print(
        f"[switch_workspace] COSMIC LIMITATION: cosmic-comp does not expose a "
        f"D-Bus or CLI interface to switch to workspace {number}. "
        f"Use the keyboard shortcut (Super+{number}) or the COSMIC workspace UI."
    )


# ── B06  show_desktop ───────────────────────────────────────────────────────
# COSMIC_LIMITATION: no minimize-all API.

def show_desktop() -> None:
    print(
        "[show_desktop] COSMIC LIMITATION: cosmic-comp does not expose a "
        "D-Bus or CLI interface to minimize all windows. "
        "Use the COSMIC workspace overview (Super key) instead."
    )


# ── B07  maximize_window ────────────────────────────────────────────────────
# COSMIC_LIMITATION: no per-window state API.

def maximize_window() -> None:
    print(
        "[maximize_window] COSMIC LIMITATION: cosmic-comp does not expose a "
        "D-Bus or CLI interface to maximize the current window. "
        "Use the window's maximize button or Super+↑ keyboard shortcut."
    )


# ── B08  force_quit ─────────────────────────────────────────────────────────
# Uses pkill by process name — works across all POSIX systems.  🔒

def force_quit(app_name: str) -> None:
    """Force-kill an application by its desktop_id using pkill.

    The desktop_id is used as the process-name pattern.  pkill -i performs
    a case-insensitive substring match on the process name, which covers the
    most common cases (e.g. "code" matches "code", "firefox" matches "firefox").
    🔒 DANGEROUS — force-killing discards unsaved data.

    NOT LIVE-TESTED: pkill without -f matches against the truncated 15-char
    /proc/comm name (the same class of bug fixed in toggle_dark_mode/
    toggle_dnd above), so a desktop_id longer than 15 characters, or an app
    that runs under a different real binary name than its desktop_id (common
    for Electron apps launched via a wrapper script), may silently fail to
    match here. Deliberately not testing this live — verifying it would mean
    actually force-killing a real running app and risking real unsaved data.
    Revisit with a deliberate, low-stakes test app before relying on this for
    anything unfamiliar.
    """
    # Use the basename of the desktop_id as the kill target
    proc_name = os.path.basename(app_name)
    result = subprocess.run(
        ["pkill", "-i", proc_name], check=False, capture_output=True
    )
    if result.returncode == 0:
        print(f"[force_quit] Killed process matching '{proc_name}'")
    else:
        print(f"[force_quit] No running process found matching '{proc_name}'")


# ── B09  open_launcher ──────────────────────────────────────────────────────

def open_launcher() -> None:
    """Open the COSMIC application launcher.

    cosmic-launcher is COSMIC's app search/launch UI; invoking it directly
    opens the launcher overlay.
    """
    subprocess.Popen(
        ["cosmic-launcher"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


# ── B10  reopen_last_closed ─────────────────────────────────────────────────
# COSMIC_LIMITATION: no session-tracking API; would need our own state store.
# Step 1 scope does not include session tracking — deferred.

def reopen_last_closed() -> None:
    print(
        "[reopen_last_closed] Not implemented: this would require the assistant "
        "to maintain its own history of closed applications (COSMIC provides no "
        "such API). Deferred to a later step."
    )


# ===========================================================================
# ─── Category C1 — Web search & general knowledge ──────────────────────────
# ===========================================================================

def _open_url(url: str) -> None:
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ── C01  google_search ──────────────────────────────────────────────────────

def google_search(query: str) -> None:
    _open_url(f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}")


# ── C02  youtube_search ─────────────────────────────────────────────────────

def youtube_search(query: str) -> None:
    _open_url(f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(query)}")


# ── C03  wikipedia_search ───────────────────────────────────────────────────

def wikipedia_search(query: str) -> None:
    _open_url(f"https://en.wikipedia.org/w/index.php?search={urllib.parse.quote_plus(query)}")


# ── C04  duckduckgo_search ──────────────────────────────────────────────────

def duckduckgo_search(query: str) -> None:
    _open_url(f"https://duckduckgo.com/?q={urllib.parse.quote_plus(query)}")


# ── C05  define_word ────────────────────────────────────────────────────────

def define_word(word: str) -> None:
    """Look up a word definition — opens Merriam-Webster."""
    _open_url(f"https://www.merriam-webster.com/dictionary/{urllib.parse.quote_plus(word)}")


# ── C06  spell_word ─────────────────────────────────────────────────────────

def spell_word(word: str) -> None:
    """Show spelling — spells the word letter by letter and opens the dictionary."""
    clean = word.strip().lower()
    spelled = " ".join(clean)
    print(f"[spell] '{clean}' is spelled: {spelled}")
    _open_url(f"https://www.merriam-webster.com/dictionary/{urllib.parse.quote_plus(clean)}")


# ── C07  get_date ───────────────────────────────────────────────────────────

def get_date() -> None:
    now = datetime.datetime.now()
    print(f"[date] Today is {now.strftime('%A, %B %d, %Y')}")


# ── C08  get_time ───────────────────────────────────────────────────────────

def get_time() -> None:
    now = datetime.datetime.now()
    print(f"[time] Current time is {now.strftime('%I:%M %p')}")


# ── C09  timezone_conversion ────────────────────────────────────────────────

def timezone_conversion(location: str) -> None:
    """Show the current time in a given location — opens a time.is page."""
    slug = urllib.parse.quote_plus(location.replace(" ", "_"))
    _open_url(f"https://time.is/{slug}")
    print(f"[timezone] Opening time.is for: {location}")


# ── C10  unit_conversion ────────────────────────────────────────────────────

def unit_conversion(query: str) -> None:
    """Perform unit conversion — opens Google with the query."""
    _open_url(f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}")


# ── C11  calculator ─────────────────────────────────────────────────────────

def calculator(expression: str) -> None:
    """Evaluate a mathematical expression locally; fall back to Google."""
    # Try to evaluate safely using Python — only allow math literals and ops
    clean = re.sub(r"[^0-9+\-*/.() %^]", "", expression.replace("^", "**").replace("×", "*").replace("÷", "/"))
    try:
        result = eval(clean, {"__builtins__": {}})  # noqa: S307 — sandboxed
        print(f"[calculator] {expression} = {result}")
    except Exception:
        # Fall back to Google
        _open_url(f"https://www.google.com/search?q={urllib.parse.quote_plus(expression)}")


# ── C12  my_ip_address ──────────────────────────────────────────────────────

def my_ip_address() -> None:
    """Report the public IP address by querying a lightweight API."""
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as resp:
            ip = resp.read().decode().strip()
        print(f"[ip] Your public IP address is: {ip}")
    except Exception:
        # Fallback: local hostname IP
        try:
            hostname = socket.gethostname()
            local_ip = socket.gethostbyname(hostname)
            print(f"[ip] Local IP: {local_ip} (could not reach api.ipify.org)")
        except Exception:
            print("[ip] Could not determine IP address.")


# ── C13  speed_test ─────────────────────────────────────────────────────────

def speed_test() -> None:
    """Open fast.com for a browser-based speed test."""
    _open_url("https://fast.com")
    print("[speed_test] Opening fast.com for speed test.")


# ── C14  today_in_history ───────────────────────────────────────────────────

def today_in_history() -> None:
    """Open the 'On This Day' page for today's date."""
    now = datetime.datetime.now()
    url = f"https://www.onthisday.com/events/date/{now.strftime('%B').lower()}/{now.day}"
    _open_url(url)
    print(f"[history] Opening historical events for {now.strftime('%B %d')}.")


# ── Legacy: web_search ──────────────────────────────────────────────────────
# Kept for backwards compatibility with llm_intent.py which may return this
# skill name.  Routes to google_search or youtube_search.

def web_search(query: str, site: str = "google") -> None:
    if site == "youtube":
        youtube_search(query)
    else:
        google_search(query)


# ── Legacy: run_terminal ────────────────────────────────────────────────────

def run_terminal(command: str) -> None:
    """Run a shell command.  🔒 DANGEROUS — no confirm loop yet (Step 6)."""
    subprocess.run(command, shell=True, check=False)


# ===========================================================================
# ─── Dispatch table ────────────────────────────────────────────────────────
# ===========================================================================

DISPATCH: dict[str, object] = {
    # ── Category A ────────────────────────────────────────────────────────
    "volume_up":            lambda args: volume_up(**args),
    "volume_down":          lambda args: volume_down(**args),
    "mute":                 lambda args: mute(**args),
    "unmute":               lambda args: unmute(**args),
    "set_volume":           lambda args: set_volume(**args),
    "brightness_up":        lambda args: brightness_up(**args),
    "brightness_down":      lambda args: brightness_down(**args),
    "set_brightness":       lambda args: set_brightness(**args),
    "toggle_dark_mode":     lambda args: toggle_dark_mode(**args),
    "lock_screen":          lambda args: lock_screen(**args),
    "shutdown":             lambda args: shutdown(**args),
    "restart":              lambda args: restart(**args),
    "logout":               lambda args: logout(**args),
    "sleep":                lambda args: sleep(**args),
    "screenshot":           lambda args: screenshot(**args),
    "start_recording":      lambda args: start_recording(**args),
    "stop_recording":       lambda args: stop_recording(**args),
    "toggle_wifi":          lambda args: toggle_wifi(**args),
    "toggle_bluetooth":     lambda args: toggle_bluetooth(**args),
    "toggle_dnd":           lambda args: toggle_dnd(**args),
    "toggle_airplane":      lambda args: toggle_airplane(**args),
    "toggle_night_light":   lambda args: toggle_night_light(**args),
    "battery_status":       lambda args: battery_status(**args),
    "toggle_kbd_backlight": lambda args: toggle_kbd_backlight(**args),
    "toggle_mic_mute":      lambda args: toggle_mic_mute(**args),
    "toggle_display":       lambda args: toggle_display(**args),
    # ── Category B ────────────────────────────────────────────────────────
    "open_app":             lambda args: open_app(**args),
    "close_app":            lambda args: close_app(**args),
    "switch_to_app":        lambda args: switch_to_app(**args),
    "list_open_apps":       lambda args: list_open_apps(**args),
    "switch_workspace":     lambda args: switch_workspace(**args),
    "show_desktop":         lambda args: show_desktop(**args),
    "maximize_window":      lambda args: maximize_window(**args),
    "force_quit":           lambda args: force_quit(**args),
    "open_launcher":        lambda args: open_launcher(**args),
    "reopen_last_closed":   lambda args: reopen_last_closed(**args),
    # ── Category C1 ───────────────────────────────────────────────────────
    "google_search":        lambda args: google_search(**args),
    "youtube_search":       lambda args: youtube_search(**args),
    "wikipedia_search":     lambda args: wikipedia_search(**args),
    "duckduckgo_search":    lambda args: duckduckgo_search(**args),
    "define_word":          lambda args: define_word(**args),
    "spell_word":           lambda args: spell_word(**args),
    "get_date":             lambda args: get_date(**args),
    "get_time":             lambda args: get_time(**args),
    "timezone_conversion":  lambda args: timezone_conversion(**args),
    "unit_conversion":      lambda args: unit_conversion(**args),
    "calculator":           lambda args: calculator(**args),
    "my_ip_address":        lambda args: my_ip_address(**args),
    "speed_test":           lambda args: speed_test(**args),
    "today_in_history":     lambda args: today_in_history(**args),
    # ── Legacy (Phase 0, for llm_intent.py compatibility) ─────────────────
    "web_search":           lambda args: web_search(**args),
    "run_terminal":         lambda args: run_terminal(**args),
}
