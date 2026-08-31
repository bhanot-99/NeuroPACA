import select
import time

from pywayland.client import Display
from pywayland.protocol.ext_idle_notify_v1 import ExtIdleNotifierV1
from pywayland.protocol.wayland import WlSeat

d = Display()
d.connect()
st = {}
reg = d.get_registry()


def on_global(reg, name, iface, version):
    if iface == "ext_idle_notifier_v1":
        st["n"] = reg.bind(name, ExtIdleNotifierV1, min(version, 2))
    elif iface == "wl_seat":
        st["s"] = reg.bind(name, WlSeat, min(version, 4))


reg.dispatcher["global"] = on_global
d.roundtrip()
notif = st["n"].get_idle_notification(2000, st["s"])  # 2s idle threshold
hits = []
notif.dispatcher["idled"] = lambda n: (
    hits.append(("idled", time.time())),
    print(time.strftime("%H:%M:%S"), "IDLED"),
)
notif.dispatcher["resumed"] = lambda n: (
    hits.append(("resumed", time.time())),
    print(time.strftime("%H:%M:%S"), "RESUMED"),
)
d.flush()
fd = d.get_fd()
print("capturing 12s, 2s idle threshold — will fire if input pauses >=2s once")
end = time.time() + 12
while time.time() < end:
    r, _, _ = select.select([fd], [], [], 0.3)
    if r:
        d.read()
    d.dispatch(block=False)
    d.flush()
print("RESULT:", [h[0] for h in hits] or "NONE")
d.disconnect()
