"""Presenter notes written by the Claude Code CLI.

Every call to Claude goes through ``ClaudeCliWriter``, which runs
``claude -p`` as a subprocess:

* with every tool turned off (``--tools ""``) and no MCP servers, so whatever a
  name says, the most it can change is the text that comes back;
* with our own system prompt instead of Claude Code's coding prompt;
* with a minimal environment (no inherited variables) whose home and config
  directory are under the state directory, and an empty working directory;
* with a time limit, and killed when the job is cancelled.

A prompt holds taxon names, ranks and common names only. Favorites, observers,
places and dates never go into it: favorite counts never go into the deck, and
the rest is not needed for notes about a species.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from collections import deque
from typing import Callable

from .config import Settings
from .diagnostics import get_logger, log_event
from .inaturalist import GROUP_RANKS
from .models import ObservationState, clean_multiline
from .sorting import effective_name

log = get_logger(__name__)

MAX_BULLETS = 8
MAX_BULLET_CHARS = 300
MAX_NOTES_TEXT = 3000
BULLET = "• "
NOTES_BUDGET_MESSAGE = ("This site has written its daily allowance of presenter notes. "
                        "Please try again tomorrow (UTC).")

SYSTEM_PROMPT = """\
You write presenter notes for a natural-history slide talk, often given to a \
mushroom club. The presenter glances at them while a photo of the organism is \
on the screen.

For each organism, write up to 8 bullet points, in this order, leaving out any \
you cannot fill reliably. Each one starts with its label and a colon, then a \
short sentence or two, at most about 30 words.
- "Described:" who described it and in what year, under what name if that was \
different, and, if it was later moved to another genus, by whom and when \
(for example "Linnaeus, 1753, as Agaricus muscarius; moved to Amanita by \
Lamarck, 1783").
- "Type locality:" where the type specimen was collected.
- "Describer:" one noteworthy fact about the person who described it, if there \
is one worth telling (other famous work, an unusual life, a famous dispute).
- "Name:" what the genus name and the species epithet each mean, and the \
language they come from.
- "Edibility:" edible, poisonous, deadly or psychoactive, naming the compounds \
where they are known. State the facts plainly. Never give advice on eating or \
using it.
- "Ecology:" habitat, substrate, mycorrhizal or other partners, season.
- "Range:" where in the world it is found.
- "Story:" history, cultural uses, changes in its classification or another \
surprising fact.
Do not describe how to identify it.

Use only well-established facts you are confident about. Never guess or invent. \
Years, people and type localities are easy to get wrong: give one only when you \
are sure of it, and leave the point out (or that part of it) rather than guess. \
If you know little about an organism (an obscure species, a provisional name \
such as one marked "nom. prov.", or a name you do not recognize), write fewer \
bullets about what is known of its genus or family, and say that they are about \
the genus or family. A provisional name has not been formally described: say \
so instead of giving a describer, a year or a type locality. A name ending in \
"sp.", or a genus, family or higher rank, means the species is not known: write \
about that group (who named the genus and when, its type species, what its \
name means).

"name" is the name on the slide. "inaturalist_name", when given, is \
iNaturalist's current identification; write about "name".

Plain text only: no markdown, no bullet characters, no em dashes. Write in English.
"""

WEB_SEARCH_PROMPT = """
You can search the web. Use it to check what you are unsure of, above all the \
describer, the year, the original name, the type locality and the meaning of \
the name. Prefer Index Fungorum, MycoBank, the original description and \
Wikipedia. Keep to one or two searches per organism. If good sources disagree, \
say so briefly or leave the point out. Do not include URLs, citations or \
specimen numbers, and do not mention the searches.
"""

NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_BULLETS},
                },
                "required": ["key", "bullets"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["notes"],
    "additionalProperties": False,
}

_AUTH_ERROR = re.compile(
    r"oauth|log ?in\b|authenticat|credential|invalid api key|token (has )?expired|invalid (access )?token", re.I)
_LIMIT_ERROR = re.compile(r"usage limit|rate limit|limit reached|hit your limit|limit will reset", re.I)
_DASH = re.compile(r"\s*—\s*")
_LEADING_BULLET = re.compile(r"^[\s•\-*·–]+")


class NotesError(Exception):
    """Claude could not write notes. The message is user-safe. ``fatal``:
    every later call would fail too (signed out, usage limit), so the job stops."""

    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


class NotesCancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# What is sent, and what comes back
# ---------------------------------------------------------------------------

def subject_for(obs: dict, state: ObservationState | None) -> dict:
    """What Claude is told about an observation's organism: names and ranks,
    nothing else."""
    taxon = obs.get("taxon") or {}
    ranks = obs.get("ranks") or {}
    subject = {"name": (effective_name(state, obs) or "").strip()}
    inat = (obs.get("inat_name") or "").strip()
    if inat and inat != subject["name"]:
        subject["inaturalist_name"] = inat
    if taxon.get("rank"):
        subject["rank"] = taxon["rank"]
    if obs.get("common_name"):
        subject["common_name"] = obs["common_name"]
    for rank in GROUP_RANKS:
        if ranks.get(rank):
            subject[rank] = ranks[rank]
    return subject


def build_prompt(subjects: dict[str, dict]) -> str:
    items = [{"key": key, **subject} for key, subject in subjects.items()]
    return ("Write presenter notes for each of these organisms, one entry per key.\n\n"
            + json.dumps(items, ensure_ascii=False, indent=1))


def clean_notes(bullets) -> str:
    """Claude's bullet points as the text kept in the project: one "• " line
    each, at most MAX_BULLETS, without control characters or em dashes."""
    lines = []
    for bullet in bullets if isinstance(bullets, list) else []:
        if not isinstance(bullet, str):
            continue
        text = " ".join(clean_multiline(bullet).split())
        text = _DASH.sub(", ", _LEADING_BULLET.sub("", text)).strip()
        if not text:
            continue
        if len(text) > MAX_BULLET_CHARS:
            text = text[: MAX_BULLET_CHARS - 1].rstrip() + "…"
        lines.append(BULLET + text)
        if len(lines) == MAX_BULLETS:
            break
    return "\n".join(lines)[:MAX_NOTES_TEXT]


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------

class ClaudeCliWriter:
    """Writes notes for a few organisms per ``claude -p`` call."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def command(self, web_search: bool = False) -> list[str]:
        s = self.settings
        # No tools, or only WebSearch (it runs at Anthropic; WebFetch would
        # fetch pages from this server and stays off).
        tools = ["--tools", "WebSearch", "--allowedTools", "WebSearch"] if web_search else ["--tools", ""]
        return [
            s.notes_cli, "-p",
            "--output-format", "json",
            "--json-schema", json.dumps(NOTES_SCHEMA, separators=(",", ":")),
            "--model", s.notes_model,
            "--effort", s.notes_effort,
            *tools,
            "--strict-mcp-config",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--system-prompt", SYSTEM_PROMPT + (WEB_SEARCH_PROMPT if web_search else ""),
        ]

    def environment(self) -> dict[str, str]:
        home = self.settings.claude_dir
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": str(home),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "CLAUDE_CODE_OAUTH_TOKEN": self.settings.notes_token,
            "LANG": "C.UTF-8",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
        }

    def write_batch(self, subjects: dict[str, dict], cancelled: Callable[[], bool],
                    web_search: bool = False) -> dict[str, str]:
        """Notes for each key of ``subjects`` that Claude answered; keys it left
        out are missing. Raises NotesError or NotesCancelled."""
        work = self.settings.claude_dir / "work"
        work.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                self.command(web_search), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=work, env=self.environment(), start_new_session=True,
            )
        except OSError as exc:
            log_event(log, logging.ERROR, "notes.cli_missing", error=type(exc).__name__)
            raise NotesError("Claude Code is not installed correctly on this server.", fatal=True) from None
        timeout = self.settings.notes_web_call_timeout if web_search else self.settings.notes_call_timeout
        out, err = self._communicate(proc, build_prompt(subjects).encode(), cancelled, timeout)
        data = _result(out)
        if data is None or data.get("is_error") or data.get("subtype") != "success":
            raise _error_for(data, err, proc.returncode)
        payload = data.get("structured_output")
        if not isinstance(payload, dict):
            try:
                payload = json.loads(data.get("result") or "")
            except ValueError:
                payload = None
        if not isinstance(payload, dict) or not isinstance(payload.get("notes"), list):
            log_event(log, logging.WARNING, "notes.bad_output", detail=str(data.get("result") or "")[:200])
            raise NotesError("Claude's answer could not be read.")
        written = {}
        for item in payload["notes"]:
            if isinstance(item, dict) and item.get("key") in subjects:
                text = clean_notes(item.get("bullets"))
                if text:
                    written[item["key"]] = text
        log_event(log, logging.INFO, "notes.call", names=len(subjects), written=len(written), web_search=web_search,
                  duration_ms=(time.monotonic() - started) * 1000, cost_usd=data.get("total_cost_usd"))
        return written

    def _communicate(self, proc: subprocess.Popen, prompt: bytes, cancelled: Callable[[], bool],
                     timeout: float) -> tuple[bytes, bytes]:
        deadline = time.monotonic() + timeout
        send = prompt
        while True:
            try:
                return proc.communicate(send, timeout=1.0)
            except subprocess.TimeoutExpired:
                send = None  # already written; a retry must not send it again
                if cancelled():
                    _kill(proc)
                    raise NotesCancelled() from None
                if time.monotonic() > deadline:
                    _kill(proc)
                    log_event(log, logging.WARNING, "notes.timeout", seconds=timeout)
                    raise NotesError("Claude took too long to answer.") from None


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # the CLI and anything it started
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.communicate(timeout=10)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass


def _result(out: bytes) -> dict | None:
    """The CLI's result object. With --output-format json it prints one; take
    the last "result" entry if it ever prints a list."""
    try:
        data = json.loads(out.decode("utf-8", "replace") or "null")
    except ValueError:
        return None
    if isinstance(data, list):
        data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), None)
    return data if isinstance(data, dict) else None


def _error_for(data: dict | None, stderr: bytes, returncode: int | None) -> NotesError:
    text = str((data or {}).get("result") or "") + " " + stderr.decode("utf-8", "replace")
    status = (data or {}).get("api_error_status")
    log_event(log, logging.WARNING, "notes.cli_error", returncode=returncode, status=status,
              subtype=(data or {}).get("subtype"), detail=" ".join(text.split())[:300])
    if status in (401, 403) or _AUTH_ERROR.search(text):
        return NotesError("Claude could not sign in. The Claude token on the server may have expired.", fatal=True)
    if status == 429 or _LIMIT_ERROR.search(text):
        return NotesError("Claude's usage limit has been reached. Please try again later.", fatal=True)
    return NotesError("Claude could not write these notes.")


# ---------------------------------------------------------------------------
# Access code
# ---------------------------------------------------------------------------

class AccessGuard:
    """Checks the access code sent with a notes request. A client with more
    than ``max_failures`` wrong codes in ``window`` seconds is refused until
    the window has passed."""

    def __init__(self, code: str, max_failures: int = 10, window: float = 3600, clock=time.monotonic):
        self._code = code.encode()
        self.max_failures, self.window, self._clock = max_failures, window, clock
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, client: str, supplied: str | None) -> str:
        """"ok", "missing" (no code sent), "denied" (wrong code) or "locked"."""
        with self._lock:
            now = self._clock()
            recent = self._failures.get(client)
            while recent and now - recent[0] > self.window:
                recent.popleft()
            if recent is not None and not recent:
                del self._failures[client]
                recent = None
            if recent and len(recent) >= self.max_failures:
                return "locked"
            if not supplied:
                return "missing"
            if self._code and secrets.compare_digest(supplied.encode(), self._code):
                return "ok"
            self._failures.setdefault(client, deque()).append(now)
            if len(self._failures) > 10_000:  # forget the oldest clients
                for key in list(self._failures)[:1000]:
                    del self._failures[key]
            return "denied"
