# A6.3 · activation-mechanism spike — blocker, SOLVED

Throwaway. De-risks VISION_PHASES.md A6.3's spike question #3: *"does
COSMIC's compositor implement `org.freedesktop.portal.GlobalShortcuts`
today?"* — asked explicitly rather than assumed, given this project's history
with COSMIC/Wayland surprises (B15/B16).

## Result: no

Ran `probe_global_shortcuts.sh` against this machine's live session
(`XDG_CURRENT_DESKTOP=COSMIC`, `XDG_SESSION_TYPE=wayland`, verified 2026-09-14).
It introspects `org.freedesktop.portal.Desktop` over the session bus — purely
read-only, no shortcut is requested or registered — and lists every interface
the running `xdg-desktop-portal` backend implements.

28 interfaces are exposed (Notification, ScreenCast, Camera, RemoteDesktop,
Settings, ...). `org.freedesktop.portal.GlobalShortcuts` is **not** among
them.

## Consequence for the A6.3 build plan

This settles the open question cleanly, in the fallback's favor:

- `voice_activation_mode` should **default to `"tray"`, not `"hotkey"`** —
  there is no portal to fall back *from* on this machine today.
- The tray-click activation path (new trigger-file plumbing, since the
  current tray is read-only — see the master plan's Q2) is not a fallback
  for this build, it is the only working path. `interface/activation.py`'s
  hotkey branch should still be built (a future compositor update, or a
  different machine, may implement the portal), but it must **degrade to
  reporting "unavailable" cleanly**, never assume it will connect.
- Revisit this probe after any COSMIC/`xdg-desktop-portal` upgrade — this is
  a live-environment fact, not a permanent architectural constraint.
