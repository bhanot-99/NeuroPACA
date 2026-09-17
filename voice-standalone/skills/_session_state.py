"""
Minimal in-memory session state, shared between main.py and Category J
(assistant meta) skills. Not persisted — resets every process restart, which
is correct: "asleep" and "last response" are properties of a running
conversation, not durable facts.
"""

_asleep = False
_last_response: str | None = None


def go_to_sleep() -> None:
    global _asleep
    _asleep = True


def wake_up() -> None:
    global _asleep
    _asleep = False


def is_asleep() -> bool:
    return _asleep


def set_last_response(text: str) -> None:
    global _last_response
    if text.strip():
        _last_response = text.strip()


def get_last_response() -> str | None:
    return _last_response


_user_name: str | None = None


def set_user_name(name: str | None) -> None:
    global _user_name
    if name and name.strip():
        _user_name = name.strip()
    elif name is None:
        _user_name = None


def get_user_name() -> str | None:
    return _user_name

