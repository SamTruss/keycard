"""ASCII banners: one for the operator's terminal, two for the SSH client."""

from __future__ import annotations

_WIDTH = 26
_BOLD_CYAN = "\x1b[1;36m"
_BOLD_YELLOW = "\x1b[1;33m"
_DIM = "\x1b[2m"
_RESET = "\x1b[0m"


def _card(*lines: str) -> str:
    # Plain ASCII, deliberately — this prints through whatever encoding the
    # operator's local console happens to use, which isn't always UTF-8.
    top = f"+{'-' * _WIDTH}+"
    bottom = top
    stripe = f"|{'#' * _WIDTH}|"
    body = "\n".join(f"|{line.ljust(_WIDTH)}|" for line in lines)
    return f"{top}\n{stripe}\n{body}\n{bottom}"


LOGO = _card(
    "",
    "  k e y c a r d",
    "  disposable ssh sandboxes",
    "",
)


def accepted(room_name: str, image: str) -> bytes:
    lines = [
        "",
        f"{_BOLD_CYAN}KEYCARD ACCEPTED{_RESET}",
        f"your room is {room_name} {_DIM}({image}){_RESET}",
        "",
    ]
    return ("\r\n".join(lines) + "\r\n").encode()


def resumed(room_name: str, image: str) -> bytes:
    lines = [
        "",
        f"{_BOLD_CYAN}KEYCARD RESUMED{_RESET}",
        f"welcome back to {room_name} {_DIM}({image}){_RESET}",
        "",
    ]
    return ("\r\n".join(lines) + "\r\n").encode()


def destroyed(reason: str) -> bytes:
    lines = [
        "",
        f"{_BOLD_YELLOW}ROOM DESTROYED{_RESET} {_DIM}— {reason}{_RESET}",
        "",
    ]
    return ("\r\n".join(lines) + "\r\n").encode()
