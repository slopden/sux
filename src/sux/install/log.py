import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

MIN_SECRET_LEN = 8
MAX_OUTPUT = 16_384  # per captured stream


class Fail(Exception):  # noqa: N818
    """A check or step failed; the message is user-facing."""


def say(msg=""):
    print(msg, flush=True)


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "sux" / "install"


class RunLog:
    """Append-only JSONL event log, flushed per event.

    Every record has ts (UTC), t (seconds since start) and event. Registered
    secrets are replaced with "<redacted>" in every string before it is
    written. `path=None` keeps the same interface and writes nothing.
    """

    def __init__(self, path=None):
        self.path = path
        self._f = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._f = path.open("a", encoding="utf-8")
        self._secrets = set()
        self._t0 = time.monotonic()

    @classmethod
    def for_recipe(cls, name):
        stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
        return cls(state_dir() / f"{name}.{stamp}.jsonl")

    def secret(self, value):
        if value and len(value) >= MIN_SECRET_LEN:
            self._secrets.add(value)

    def scrub(self, obj):
        if isinstance(obj, str):
            for s in self._secrets:
                obj = obj.replace(s, "<redacted>")
            return obj
        if isinstance(obj, dict):
            return {k: self.scrub(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set)):
            return [self.scrub(v) for v in obj]
        return obj

    @staticmethod
    def clip(text):
        if text is None or len(text) <= MAX_OUTPUT:
            return text
        return text[:MAX_OUTPUT] + f"\n...<truncated {len(text) - MAX_OUTPUT} chars>"

    def event(self, event, **fields):
        if self._f is None:
            return
        rec = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "t": round(time.monotonic() - self._t0, 3),
            "event": event,
            **fields,
        }
        self._f.write(json.dumps(self.scrub(rec), default=str) + "\n")
        self._f.flush()

    def close(self):
        if self._f is not None:
            self._f.close()
            self._f = None
