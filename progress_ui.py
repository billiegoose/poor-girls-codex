from __future__ import annotations

import sys


def color(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def response_status_line(status: str, *, prefix: str | None = None) -> str:
    leader = f"{prefix} " if prefix else "  "
    if status in {"[sent]", "[sent summary]"}:
        rendered_status = color(status, "1;32")
    elif status == "[pending]" or "retry" in status:
        rendered_status = color(status, "1;33")
    else:
        rendered_status = status
    return f"{leader}{'response':<11}{rendered_status}"


class ResponseProgress:
    """A response-delivery row that rewrites in place while it owns the live TTY line."""

    _live_owner: ResponseProgress | None = None

    def __init__(self, *, prefix: str | None = None) -> None:
        self.prefix = prefix
        self.status: str | None = None
        self.live = False

    @classmethod
    def commit_live(cls) -> None:
        if cls._live_owner is not None:
            cls._live_owner.commit()

    def set_status(self, status: str) -> None:
        if status == self.status:
            return
        self.status = status
        line = response_status_line(status, prefix=self.prefix)
        if not sys.stdout.isatty():
            print(line, flush=True)
            return
        if ResponseProgress._live_owner not in (None, self):
            ResponseProgress._live_owner.commit()
        if self.live:
            sys.stdout.write(f"\r\033[2K{line}")
        else:
            sys.stdout.write(line)
            self.live = True
            ResponseProgress._live_owner = self
        sys.stdout.flush()

    def commit(self) -> None:
        if sys.stdout.isatty() and self.live:
            sys.stdout.write("\n")
            sys.stdout.flush()
        self.live = False
        if ResponseProgress._live_owner is self:
            ResponseProgress._live_owner = None
