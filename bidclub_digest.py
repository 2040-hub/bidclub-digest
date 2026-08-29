#!/usr/bin/env python3
"""BidClub daily podcast digest.

Fetches every podcast episode published on the previous day (Beijing time by
default) from the public, keyless BidClub API and pushes the ready-made
bilingual summaries to Telegram and/or Lark (Feishu).

Long digests are split into several messages so neither platform's hard limits
can make a send fail:
  - Telegram sendMessage caps text at 4096 characters *after entity parsing*
    (TDLib counts Unicode code points, not bytes).
  - A Lark custom-bot webhook caps the whole JSON request body at 20 KB.

Data source (no account, no API key):
  - https://bidclub.ai/api-docs
  - GET /api/v1/episodes?limit=&offset=   newest-first episode metadata
  - GET /api/v1/episodes/{slug}           full record incl. tldr_md / digest_md

Design goals: robustness first. One bad episode never aborts the digest, a
failed channel never blocks the other, and an episode is only marked delivered
for a destination once every chunk carrying it was accepted -- so a transient
outage retries tomorrow instead of silently dropping the episode.

Python 3.13+
"""

from __future__ import annotations

import argparse
import base64
import configparser
import contextlib
import dataclasses
import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import random
import re
import sys
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:  # pragma: no cover - dependency is declared in requirements.txt
    print("[ERROR] 'requests' library not found. Install with: pip install -r requirements.txt")
    sys.exit(1)

try:
    import fcntl  # POSIX-only; used for the cross-process single-instance lock.
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

# --------------------------------------------------------------------------- #
# Constants & logging
# --------------------------------------------------------------------------- #

VERSION = "1.0"

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.ini"

STATE_VERSION = 1

# Canonical episode page, verified to answer HTTP 200: https://bidclub.ai/e/{slug}
EPISODE_PAGE = "{base}/e/{slug}"

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# Telegram's own ceilings. 4096 is the documented cap on `text` *after entities
# parsing*; the Bot API server applies a second, byte-based guard of 32768 to the
# raw pre-parse string. Both are checked defensively before every send.
TG_HARD_CHARS = 4096
TG_HARD_BYTES = 32768
# Entities per message are silently capped server-side (over-cap entities are
# dropped with a 200 OK, the worst possible failure mode), so stay well under it.
TG_MAX_ENTITIES = 80

# A Lark custom-bot webhook rejects any request body over 20 KB. Everything --
# msg_type, timestamp, sign and every byte of JSON punctuation -- counts.
LARK_HARD_BODY_BYTES = 20480
# How many times one Lark chunk may be halved before we give up on it.
LARK_MAX_SPLIT_DEPTH = 4

# Lark error codes worth treating differently from "unknown failure".
LARK_TRANSIENT_CODES = {11232, 11233, 11247}          # throttled, retry later
LARK_PAYLOAD_CODES = {9499, 19002, 19036, 11246}      # bad/oversized body
LARK_CONTENT_CODES = {11248, 11312}                   # rejected content, drop chunk
LARK_FATAL_CODES = {19001, 19021, 19022, 19024}       # config/security, alert a human

# Patterns whose captured secret must never reach the logs. requests embeds the
# full URL (including a Lark webhook token or a Telegram bot token) in connection
# exceptions, so we redact those secrets from every log record defensively.
_SECRET_PATTERNS = [
    re.compile(r"(/hook/)[A-Za-z0-9_\-]+"),           # Lark / Feishu webhook token
    re.compile(r"(bot)\d+:[A-Za-z0-9_\-]+"),          # Telegram bot token
]


class _SecretRedactor(logging.Filter):
    """Mask credential tokens in any emitted log message."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = msg
        for pat in _SECRET_PATTERNS:
            redacted = pat.sub(r"\1***", redacted)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
for _h in logging.getLogger().handlers:
    _h.addFilter(_SecretRedactor())
logger = logging.getLogger("bidclub_digest")
# Also filter at the logger, not only at the handlers we happen to own right now:
# if configure_logging ever leaves the root with no handler, logging.lastResort
# still prints the record, and only a logger-level filter redacts it.
logger.addFilter(_SecretRedactor())

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


def configure_logging(level_name: str, log_file: str, max_bytes: int,
                      backup_count: int, console: bool) -> None:
    """Attach the configured handlers. Called before config.ini is fully validated
    so that the configuration warnings themselves land in the log file."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, (level_name or "INFO").upper(), logging.INFO))

    console_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]

    file_handler = None
    if log_file:
        path = Path(log_file)
        if not path.is_absolute():
            path = BASE_DIR / path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=max(0, max_bytes), backupCount=max(0, backup_count),
                encoding="utf-8",
            )
            file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
            file_handler.addFilter(_SecretRedactor())
            root.addHandler(file_handler)
        except OSError as exc:
            logger.warning("Cannot open the log file %s (%s); logging to the console only.",
                           path, exc)

    # Drop the console handlers only once a replacement exists. Leaving the root
    # with zero handlers would hand every record to logging.lastResort, which
    # carries no redaction filter.
    if not console and file_handler is not None:
        for handler in console_handlers:
            root.removeHandler(handler)


_CONTROL_CHARS_RE = re.compile(r"[\r\t\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+")
# Unpaired surrogates survive json.loads but raise UnicodeEncodeError on any
# .encode() -- which would break the length measurement, the Lark payload and
# the ledger write. Scrub them at the boundary instead of guarding every use.
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def scrub(value) -> str:
    """Coerce an upstream value to a str that is guaranteed UTF-8 encodable."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return _SURROGATE_RE.sub("�", text) if _SURROGATE_RE.search(text) else text


def _sanitize_inline(value, limit: int = 300) -> str:
    """Collapse control characters and newlines out of a single-line upstream field.

    Upstream titles and show names are attacker-influenceable text; without this a
    crafted title could forge extra lines inside a message.
    """
    if value is None:
        return ""
    text = _CONTROL_CHARS_RE.sub(" ", scrub(value)).replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()[:limit]


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class Config:
    # [bidclub]
    api_base: str
    request_timeout: int
    max_retries: int
    retry_backoff: float
    page_size: int
    max_pages: int
    cache_buster: bool
    # [window]
    timezone_name: str
    date_basis: str
    lookback_days: int
    notify_on_first_run: bool
    # [content]
    summary_field: str
    language: str
    max_episodes: int
    max_summary_chars: int
    include_dek: bool
    include_episode_link: bool
    include_source_link: bool
    include_show: bool
    include_duration: bool
    sort: str
    # [telegram]
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_ids: list[str]
    telegram_parse_mode: str
    telegram_disable_notification: bool
    telegram_thread_id: str
    # [lark]
    lark_enabled: bool
    lark_webhook_url: str
    lark_secret: str
    lark_msg_type: str
    lark_card_template: str
    lark_lang: str
    # [output]
    telegram_chunk_chars: int
    telegram_max_chunks: int
    telegram_delay_seconds: float
    lark_chunk_bytes: int
    lark_max_chunks: int
    lark_delay_seconds: float
    startup_jitter_seconds: int
    header_template: str
    title: str
    footer_template: str
    notify_empty: bool
    empty_message: str
    date_format: str
    # [state]
    state_file: Path
    state_retention_days: int
    lock_file: Path
    # resolved once so a bad timezone is reported exactly one time
    tz: object

    @property
    def telegram_on(self) -> bool:
        return bool(self.telegram_enabled and self.telegram_bot_token and self.telegram_chat_ids)

    @property
    def lark_on(self) -> bool:
        return bool(self.lark_enabled and self.lark_webhook_url)


_PLACEHOLDER_TOKENS = {
    "", "your_bot_token", "your_chat_id", "xxxxxxxxxxxxxxx",
    "your_telegram_bot_token", "your_telegram_chat_id", "your_lark_webhook_url",
    "your_lark_secret", "123456:abc-def1234567890", "-1001234567890",
    "https://open.feishu.cn/open-apis/bot/v2/hook/your-token",
    "https://open.larksuite.com/open-apis/bot/v2/hook/your-token",
}


def _clean(value: str) -> str:
    """Strip a config value; treat known template placeholders as empty."""
    value = (value or "").strip()
    if value.lower() in _PLACEHOLDER_TOKENS or value.upper().startswith("YOUR_"):
        return ""
    return value


def load_timezone(name: str):
    """Resolve an IANA timezone name, falling back to UTC+08:00 (Beijing).

    A missing system tz database must not take the cron job down; `tzdata` is
    pinned in requirements.txt so this fallback should never fire in practice.
    """
    try:
        return ZoneInfo(name)
    except Exception as exc:
        logger.error("Unknown timezone %r (%s); falling back to UTC+08:00.", name, exc)
        return timezone(timedelta(hours=8), "UTC+08:00")


def new_parser() -> configparser.ConfigParser:
    """A parser configured the way every read of config.ini needs it.

    interpolation=None is MANDATORY: [output] date_format defaults to %Y-%m-%d
    and the default interpolation raises on a bare '%'. Inline comments use ';'
    only, so a '#' inside a URL or a header/footer template is never silently
    truncated -- full-line '#' comments still work.
    """
    return configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";",))


def read_config_file(config_path: Path) -> configparser.ConfigParser:
    if not config_path.is_file():
        raise FileNotFoundError(
            f"Configuration file not found: {config_path}. "
            "Copy config.ini.template to config.ini and fill in your credentials."
        )
    parser = new_parser()
    try:
        with open(config_path, encoding="utf-8") as fh:
            parser.read_file(fh)
    except configparser.ParsingError as exc:
        # exc's message quotes the offending lines verbatim, and one of them could
        # be a bot token or a webhook URL. Report line numbers only.
        lines = ", ".join(str(lineno) for lineno, _ in getattr(exc, "errors", []) or [])
        raise ValueError(f"Failed to parse {config_path}: malformed line(s) "
                         f"{lines or '?'} -- every setting must sit under a [section] "
                         f"header and use 'key = value'.") from exc
    except configparser.Error as exc:
        raise ValueError(f"Failed to parse {config_path}: {type(exc).__name__}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"Failed to read {config_path}: {exc}") from exc
    return parser


KNOWN_SECTIONS = {"bidclub", "window", "content", "telegram", "lark",
                  "output", "state", "logging"}


def warn_unknown_sections(parser: configparser.ConfigParser) -> None:
    """Flag sections this loader never reads.

    configparser section names are case-sensitive, so a stray [Telegram] would
    otherwise disable a whole channel with no message at all.
    """
    for section in parser.sections():
        if section in KNOWN_SECTIONS:
            continue
        match = next((k for k in KNOWN_SECTIONS if k == section.strip().lower()), None)
        if match:
            logger.warning("config.ini has a [%s] section but the names are case-sensitive; "
                           "every key in it is IGNORED. Rename it to [%s].", section, match)
        else:
            logger.warning("config.ini has an unknown section [%s]; it is ignored.", section)


def load_config(parser: configparser.ConfigParser) -> Config:
    """Validate an already-parsed config.ini. Raises on fatal misconfiguration."""
    warn_unknown_sections(parser)

    def get(section: str, option: str, fallback: str = "") -> str:
        try:
            return parser.get(section, option, fallback=fallback)
        except configparser.Error as exc:
            logger.warning("Cannot read [%s] %s (%s); using the default.", section, option, exc)
            return fallback

    def get_int(section: str, option: str, fallback: int,
                lo: int | None = None, hi: int | None = None) -> int:
        raw = get(section, option, str(fallback)).strip()
        try:
            value = int(raw)
        except (TypeError, ValueError):
            logger.warning("Invalid int for [%s] %s=%r; using %d", section, option, raw, fallback)
            value = fallback
        return _clamp(value, lo, hi, section, option)

    def get_float(section: str, option: str, fallback: float,
                  lo: float | None = None, hi: float | None = None) -> float:
        raw = get(section, option, str(fallback)).strip()
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning("Invalid number for [%s] %s=%r; using %s",
                           section, option, raw, fallback)
            value = fallback
        return _clamp(value, lo, hi, section, option)

    def get_bool(section: str, option: str, fallback: bool) -> bool:
        raw = get(section, option, "").strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
        if raw:
            logger.warning("Invalid boolean for [%s] %s=%r; using %s",
                           section, option, raw, fallback)
        return fallback

    def get_choice(section: str, option: str, fallback: str, allowed: set[str]) -> str:
        raw = get(section, option, fallback).strip().lower()
        if raw in allowed:
            return raw
        if raw:
            logger.warning("Unknown [%s] %s=%r (allowed: %s); using %r",
                           section, option, raw, ", ".join(sorted(allowed)), fallback)
        return fallback

    api_base = get("bidclub", "api_base", "https://bidclub.ai").strip().rstrip("/")
    if not api_base.startswith(("http://", "https://")):
        logger.warning("Invalid [bidclub] api_base=%r; using https://bidclub.ai", api_base)
        api_base = "https://bidclub.ai"

    # chat_id accepts one OR many targets, separated by comma/whitespace/newline.
    # Every id must be a chat the bot belongs to. Dedup while preserving order so a
    # copy-paste mistake cannot double-send.
    chat_ids: list[str] = []
    seen_chat: set[str] = set()
    for piece in re.split(r"[\s,]+", get("telegram", "chat_id")):
        cid = _clean(piece)
        if not cid:
            continue
        # @usernames are case-insensitive: canonicalize so two casings of one
        # channel cannot become two destinations (a double send) and the ledger's
        # destination id stays stable across casing edits.
        if cid.startswith("@"):
            cid = cid.lower()
        if cid not in seen_chat:
            seen_chat.add(cid)
            chat_ids.append(cid)

    timezone_name = get("window", "timezone", "Asia/Shanghai").strip() or "Asia/Shanghai"

    cfg = Config(
        api_base=api_base,
        request_timeout=get_int("bidclub", "request_timeout", 30, 5, 300),
        max_retries=get_int("bidclub", "max_retries", 3, 1, 10),
        retry_backoff=get_float("bidclub", "retry_backoff", 2.0, 0.0, 60.0),
        page_size=get_int("bidclub", "page_size", 100, 1, 100),
        max_pages=get_int("bidclub", "max_pages", 3, 1, 50),
        cache_buster=get_bool("bidclub", "cache_buster", True),

        timezone_name=timezone_name,
        date_basis=get_choice("window", "date_basis", "published_at", {"published_at", "date"}),
        lookback_days=get_int("window", "lookback_days", 3, 1, 30),
        notify_on_first_run=get_bool("window", "notify_on_first_run", False),

        summary_field=get_choice("content", "summary_field", "tldr", {"tldr", "digest", "dek"}),
        language=get_choice("content", "language", "zh", {"zh", "en", "primary", "both"}),
        max_episodes=get_int("content", "max_episodes", 12, 1, 100),
        max_summary_chars=get_int("content", "max_summary_chars", 4000, 0, 200000),
        include_dek=get_bool("content", "include_dek", False),
        include_episode_link=get_bool("content", "include_episode_link", True),
        include_source_link=get_bool("content", "include_source_link", True),
        include_show=get_bool("content", "include_show", True),
        include_duration=get_bool("content", "include_duration", True),
        sort=get_choice("content", "sort", "published_asc", {"published_asc", "published_desc"}),

        telegram_enabled=get_bool("telegram", "enabled", True),
        telegram_bot_token=_clean(get("telegram", "bot_token")),
        telegram_chat_ids=chat_ids,
        telegram_parse_mode=get_choice("telegram", "parse_mode", "html", {"html", "plain"}),
        telegram_disable_notification=get_bool("telegram", "disable_notification", True),
        telegram_thread_id=_clean(get("telegram", "message_thread_id")),

        lark_enabled=get_bool("lark", "enabled", True),
        lark_webhook_url=_clean(get("lark", "webhook_url")),
        lark_secret=_clean(get("lark", "secret")),
        lark_msg_type=get_choice("lark", "msg_type", "interactive",
                                 {"interactive", "post", "text"}),
        lark_card_template=get("lark", "card_template", "blue").strip().lower() or "blue",
        lark_lang=get_choice("lark", "lang", "zh_cn", {"zh_cn", "en_us"}),

        telegram_chunk_chars=get_int("output", "telegram_chunk_chars", 3500, 500, 3900),
        telegram_max_chunks=get_int("output", "telegram_max_chunks", 20, 1, 200),
        telegram_delay_seconds=get_float("output", "telegram_delay_seconds", 3.2, 0.0, 60.0),
        lark_chunk_bytes=get_int("output", "lark_chunk_bytes", 8000, 1000, 16000),
        lark_max_chunks=get_int("output", "lark_max_chunks", 20, 1, 200),
        lark_delay_seconds=get_float("output", "lark_delay_seconds", 1.0, 0.0, 60.0),
        startup_jitter_seconds=get_int("output", "startup_jitter_seconds", 180, 0, 3600),
        header_template=get("output", "header_template",
                            "{title} · {date} ({index}/{total})").strip(),
        title=get("output", "title", "BidClub Daily Podcast").strip() or "BidClub Daily Podcast",
        footer_template=get("output", "footer_template",
                            "— {count} episodes · https://bidclub.ai").strip(),
        notify_empty=get_bool("output", "notify_empty", False),
        empty_message=get("output", "empty_message",
                          "No BidClub episodes published on {date}.").strip(),
        date_format=get("output", "date_format", "%Y-%m-%d").strip() or "%Y-%m-%d",

        state_file=_resolve_path(get("state", "state_file", "sent_episodes.json").strip()
                                 or "sent_episodes.json"),
        state_retention_days=get_int("state", "state_retention_days", 90, 1, 3650),
        lock_file=_resolve_path(get("state", "lock_file", "bidclub_digest.lock").strip()
                                or "bidclub_digest.lock"),
        tz=load_timezone(timezone_name),
    )

    # A token without any usable chat id is a silent foot-gun: surface it loudly
    # instead of pretending Telegram is on.
    if cfg.telegram_enabled and cfg.telegram_bot_token and not cfg.telegram_chat_ids:
        logger.warning(
            "[telegram] bot_token is set but no valid chat_id was found; "
            "Telegram channel is DISABLED. Set [telegram] chat_id to a chat the bot belongs to."
        )
    if cfg.telegram_enabled and cfg.telegram_chat_ids and not cfg.telegram_bot_token:
        logger.warning(
            "[telegram] chat_id is set but bot_token is missing or still a placeholder; "
            "Telegram channel is DISABLED. Set [telegram] bot_token to the @BotFather token."
        )
    if cfg.lark_enabled and cfg.lark_webhook_url and "/hook/" not in cfg.lark_webhook_url:
        logger.warning("[lark] webhook_url does not look like a custom-bot webhook "
                       "(expected .../open-apis/bot/v2/hook/<token>).")

    if not cfg.telegram_on and not cfg.lark_on:
        raise ValueError(
            "No notification channel configured. Set [telegram] bot_token + chat_id "
            "and/or [lark] webhook_url in config.ini."
        )
    return cfg


def _clamp(value, lo, hi, section: str, option: str):
    if lo is not None and value < lo:
        logger.warning("[%s] %s=%s below the minimum %s; clamped.", section, option, value, lo)
        return lo
    if hi is not None and value > hi:
        logger.warning("[%s] %s=%s above the maximum %s; clamped.", section, option, value, hi)
        return hi
    return value


def _resolve_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


# --------------------------------------------------------------------------- #
# BidClub API client
# --------------------------------------------------------------------------- #

SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


def is_valid_slug(value) -> bool:
    """Reject anything that could escape the /api/v1/episodes/{slug} path.

    A slug containing '/' falls through to the site's catch-all route and returns
    an HTML body; a traversal slug can land on a completely different endpoint
    that answers 200 with an unrelated schema. Validate before interpolating.
    """
    return isinstance(value, str) and bool(SLUG_RE.fullmatch(value))


class ApiError(Exception):
    """An API call that could not be completed.

    `transient` distinguishes "the upstream said no" (a 404, a malformed
    query) from "we could not reach a verdict" (timeouts, 5xx, retries
    exhausted). Only the former justifies recording a degraded episode as
    delivered; the latter must be retried on the next run.
    """

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class BidClubClient:
    """Thin, retrying JSON client for the public BidClub API."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": f"bidclub-digest/{VERSION} (+https://bidclub.ai/api-docs)",
        })

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.session.close()

    def get_json(self, path: str, params: dict | None = None) -> dict:
        """GET a JSON document. Retries transient failures, raises ApiError otherwise."""
        url = f"{self.cfg.api_base}{path}"
        query = dict(params or {})
        if self.cfg.cache_buster:
            # A once-a-day caller is exactly the request a CDN answers from a stale
            # edge copy. Unknown query parameters are ignored by the API.
            query["_cb"] = str(int(time.time()))

        last_error = "unknown error"
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                resp = self.session.get(url, params=query, timeout=self.cfg.request_timeout)
            except requests.exceptions.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                status = resp.status_code
                if status == 404:
                    raise ApiError(f"GET {path} -> 404 not found")
                if 400 <= status < 500 and status != 429:
                    body = (resp.text or "")[:200].replace("\n", " ")
                    raise ApiError(f"GET {path} -> HTTP {status}: {body}")
                if status == 429 or status >= 500:
                    last_error = f"HTTP {status}"
                elif "json" not in (resp.headers.get("Content-Type") or "").lower():
                    # A 200 that is not JSON means an edge/error page slipped
                    # through; treat it as transient rather than crashing on a
                    # JSONDecodeError.
                    last_error = f"non-JSON Content-Type {resp.headers.get('Content-Type')!r}"
                else:
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        last_error = f"malformed JSON ({exc})"
                    else:
                        if isinstance(data, dict):
                            return data
                        raise ApiError(f"GET {path} -> unexpected JSON type "
                                       f"{type(data).__name__}")
            logger.warning("GET %s failed (attempt %d/%d): %s",
                           path, attempt, self.cfg.max_retries, last_error)
            if attempt < self.cfg.max_retries and self.cfg.retry_backoff > 0:
                wait = self.cfg.retry_backoff * (2 ** (attempt - 1))
                logger.info("Retrying %s in %.1fs...", path, wait)
                time.sleep(wait)
        raise ApiError(f"GET {path} failed after {self.cfg.max_retries} attempts: "
                       f"{last_error}", transient=True)

    def list_episodes(self, limit: int, offset: int) -> dict:
        return self.get_json("/api/v1/episodes", {"limit": limit, "offset": offset})

    def get_episode(self, slug: str) -> dict:
        if not is_valid_slug(slug):
            raise ApiError(f"refusing to fetch the invalid slug {slug!r}")
        return self.get_json(f"/api/v1/episodes/{slug}")


# --------------------------------------------------------------------------- #
# Episode model & day-window selection
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class Episode:
    slug: str
    local_date: date
    sort_key: datetime
    row: dict                       # list-endpoint row (always present)
    detail: dict | None = None      # full record; None when the detail fetch failed

    @property
    def data(self) -> dict:
        return self.detail if self.detail is not None else self.row

    @property
    def degraded(self) -> bool:
        return self.detail is None


def _parse_instant(value) -> datetime | None:
    """Parse an ISO-8601 timestamp; assume UTC when it carries no offset."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def episode_local_date(row: dict, tz, date_basis: str) -> tuple[date | None, datetime | None]:
    """Resolve which local calendar day an episode belongs to.

    `published_at` is the exact publication instant and the only strictly ordered
    key; `date` is the show-declared episode date and disagrees with the local
    date of `published_at` for roughly a third of rows. Returns (None, None) when
    the row carries no usable timestamp at all.
    """
    instant = _parse_instant(row.get("published_at"))
    if date_basis == "published_at" and instant is not None:
        return instant.astimezone(tz).date(), instant

    raw_date = row.get("date")
    if isinstance(raw_date, str) and _DATE_RE.fullmatch(raw_date.strip()):
        try:
            local = date.fromisoformat(raw_date.strip())
        except ValueError:
            local = None
        if local is not None:
            # A bare date has no instant; anchor it at local noon purely so the
            # digest still has a stable sort key.
            anchor = instant or datetime.combine(
                local, dtime(12, 0), tzinfo=tz).astimezone(timezone.utc)
            return local, anchor

    if instant is not None:  # date_basis == 'date' but `date` is missing/garbage
        return instant.astimezone(tz).date(), instant
    return None, None


def collect_episodes(client: BidClubClient, cfg: Config, window_start: date,
                     window_end: date) -> tuple[list[Episode], bool]:
    """Page /api/v1/episodes newest-first and return every episode in the window.

    Paging always keys on `published_at` -- even when date_basis is `date` --
    because it is the only field the API orders by. The stop condition therefore
    overshoots the window by a safety margin instead of trusting the filter field.
    """
    tz = cfg.tz
    overshoot = 2 if cfg.date_basis == "published_at" else 20
    stop_before = window_start - timedelta(days=overshoot)

    found: dict[str, Episode] = {}
    skipped = 0
    exhausted = False

    for page in range(cfg.max_pages):
        body = client.list_episodes(cfg.page_size, page * cfg.page_size)
        rows = body.get("episodes")
        if not isinstance(rows, list):
            raise ApiError("unexpected response shape: 'episodes' is not a list")
        if not rows:
            exhausted = True
            break

        page_floor: datetime | None = None
        for row in rows:
            if not isinstance(row, dict):
                skipped += 1
                continue
            slug = row.get("slug")
            if not is_valid_slug(slug):
                logger.warning("Skipping row with an unusable slug: %r", slug)
                skipped += 1
                continue
            local, key = episode_local_date(row, tz, cfg.date_basis)
            if local is None:
                logger.warning("Skipping %s: no usable published_at/date.", slug)
                skipped += 1
                continue
            instant = _parse_instant(row.get("published_at")) or key
            if instant is not None and (page_floor is None or instant < page_floor):
                page_floor = instant
            # Dedupe by slug: a handful of episodes share a published_at, and a tie
            # group straddling an offset boundary could otherwise repeat a row.
            found.setdefault(slug, Episode(slug=slug, local_date=local, sort_key=key, row=row))

        pagination = body.get("pagination")
        pagination = pagination if isinstance(pagination, dict) else {}
        if page_floor is not None and page_floor.astimezone(tz).date() < stop_before:
            exhausted = True
            break
        if pagination.get("next_offset") is None:
            exhausted = True
            break
    if not exhausted:
        logger.warning("max_pages=%d reached before the window was covered; this scan is "
                       "INCOMPLETE. Raise [bidclub] max_pages or narrow lookback_days.",
                       cfg.max_pages)
    if skipped:
        logger.info("Skipped %d unusable row(s) while scanning.", skipped)

    selected = [ep for ep in found.values() if window_start <= ep.local_date <= window_end]
    selected.sort(key=lambda e: (e.sort_key or datetime.min.replace(tzinfo=timezone.utc), e.slug))
    logger.info("Scanned %d row(s); %d fall inside %s..%s.",
                len(found), len(selected), window_start, window_end)
    return selected, exhausted


def hydrate_episodes(client: BidClubClient, episodes: list[Episode]) -> set[str]:
    """Fetch the full record for each episode. Returns the transiently-failed slugs.

    A PERMANENT failure (404 -- the episode was withdrawn) degrades that one
    episode to its list-endpoint fields and still ships. A TRANSIENT failure
    (timeout, 5xx, retries exhausted) is reported to the caller so the episode
    is held back entirely rather than being recorded as delivered with a
    permanent "summary unavailable" -- the lookback window retries it tomorrow.
    """
    deferred: set[str] = set()
    for ep in episodes:
        try:
            detail = client.get_episode(ep.slug)
        except ApiError as exc:
            if exc.transient:
                logger.warning("Detail fetch for %s could not be completed (%s); holding "
                               "it back for the next run.", ep.slug, exc)
                deferred.add(ep.slug)
            else:
                logger.warning("Detail fetch for %s was refused (%s); using the list "
                               "metadata only.", ep.slug, exc)
            continue
        # transcript_md is ~80% of the payload and is never rendered -- drop it so
        # a 15-episode digest does not hold megabytes of text in memory.
        detail.pop("transcript_md", None)
        ep.detail = detail
    return deferred


# --------------------------------------------------------------------------- #
# Document model: Line / Span
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Span:
    kind: str          # 'text' | 'bold' | 'code' | 'link'
    text: str
    href: str = ""


@dataclasses.dataclass
class Line:
    kind: str          # 'text' | 'bullet' | 'ordered' | 'heading' | 'quote' | 'rule' | 'blank'
    spans: list[Span] = dataclasses.field(default_factory=list)
    prefix: str = ""   # ordered-list marker, e.g. "2. "
    owner: str = ""    # slug of the episode this line belongs to ("" = chrome)

    @property
    def plain(self) -> str:
        """The line as unmarked text -- the safe base for a hard cut."""
        if self.kind == "rule":
            return "---"
        if self.kind == "blank":
            return ""
        body = "".join(s.text for s in self.spans)
        if self.kind == "bullet":
            return f"- {body}"
        if self.kind == "ordered":
            return f"{self.prefix}{body}"
        if self.kind == "quote":
            return f"> {body}"
        return body


def text_line(text: str, owner: str = "") -> Line:
    return Line(kind="text", spans=[Span("text", text)], owner=owner)


def blank_line(owner: str = "") -> Line:
    return Line(kind="blank", spans=[], owner=owner)


# --------------------------------------------------------------------------- #
# Upstream Markdown -> Line list
# --------------------------------------------------------------------------- #

# One left-to-right pass gives the right precedence for free: a '**' inside a code
# span or a link target can never start a bold run. Only three constructs are
# recognised. Single '*' / '_' emphasis is deliberately NOT parsed -- episode
# titles and slugs are full of snake_case and "3-5%", and every attempt to
# interpret those produces mismatched entities.
_INLINE_RE = re.compile(
    r"(?P<code>`+[^`\n]+?`+)"
    r"|(?P<link>\[(?P<ltext>[^\]\n]*)\]\((?P<lhref>[^)\s]+)(?:\s+\"[^\"\n]*\")?\))"
    r"|(?P<bold>\*\*(?P<btext>[^\n]+?)\*\*)"
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^(\d{1,3})[.)]\s+(.*)$")
_RULE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")


def _strip_markers(text: str) -> str:
    """Drop nested markup markers from a span body we render as flat text."""
    return text.replace("**", "").replace("`", "")


def safe_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def parse_inline(text: str) -> list[Span]:
    """Split one line of Markdown into typed spans."""
    spans: list[Span] = []
    pos = 0
    for match in _INLINE_RE.finditer(text):
        if match.start() > pos:
            spans.append(Span("text", text[pos:match.start()]))
        if match.group("code"):
            spans.append(Span("code", match.group("code").strip("`")))
        elif match.group("link") is not None and match.group("lhref"):
            label = _strip_markers(match.group("ltext") or "")
            href = match.group("lhref")
            if safe_url(href):
                spans.append(Span("link", label or href, href))
            else:
                spans.append(Span("text", label or href))
        else:
            spans.append(Span("bold", _strip_markers(match.group("btext") or "")))
        pos = match.end()
    if pos < len(text):
        spans.append(Span("text", text[pos:]))
    return [s for s in spans if s.text]


def parse_markdown(md: str, owner: str = "") -> list[Line]:
    """Convert upstream Markdown into a flat list of Lines.

    Every Line is self-contained: no inline markup ever spans a newline, so the
    packer can cut between any two Lines without ever bisecting an entity.
    """
    lines: list[Line] = []
    in_fence = False
    for raw in scrub(md).splitlines():
        stripped = raw.rstrip()
        if _FENCE_RE.match(stripped):
            in_fence = not in_fence
            continue  # the fence delimiter itself is never rendered
        if in_fence:
            if stripped.strip():
                lines.append(Line("text", [Span("code", stripped)], owner=owner))
            continue
        if not stripped.strip():
            if lines and lines[-1].kind != "blank":
                lines.append(blank_line(owner))
            continue
        if _RULE_RE.match(stripped):
            lines.append(Line("rule", [], owner=owner))
            continue
        heading = _HEADING_RE.match(stripped)
        if heading:
            lines.append(Line("heading", [Span("bold", _strip_markers(heading.group(2).strip()))],
                              owner=owner))
            continue
        body = stripped.lstrip()
        bullet = _BULLET_RE.match(body)
        if bullet:
            lines.append(Line("bullet", parse_inline(bullet.group(1)), owner=owner))
            continue
        ordered = _ORDERED_RE.match(body)
        if ordered:
            lines.append(Line("ordered", parse_inline(ordered.group(2)),
                              prefix=f"{ordered.group(1)}. ", owner=owner))
            continue
        if body.startswith(">"):
            lines.append(Line("quote", parse_inline(body.lstrip(">").strip()), owner=owner))
            continue
        lines.append(Line("text", parse_inline(body), owner=owner))

    while lines and lines[-1].kind == "blank":
        lines.pop()
    return lines


# --------------------------------------------------------------------------- #
# Per-platform rendering
# --------------------------------------------------------------------------- #

# Lark parses <at user_id="all"></at> inside text and card markdown, so an
# upstream title could ping an entire work group. Strip every tag Lark knows about
# (the inner text survives) rather than escaping into entities, which the
# plain-text message type would show literally.
_LARK_TAG_RE = re.compile(r"</?\s*(?:at|a|span|text_tag|img|b|i|u|font)\b[^>]*>", re.IGNORECASE)
_LARK_BARE_TAG_RE = re.compile(r"<(?=\s*/?\s*(?:at|a|span|text_tag|img|b|i|u|font)\b)",
                               re.IGNORECASE)


def tg_escape(text: str) -> str:
    """Escape for Telegram's HTML parse mode. '&' must be replaced first."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg_escape_attr(text: str) -> str:
    return tg_escape(text).replace('"', "&quot;")


def lark_defuse(text: str) -> str:
    return _LARK_BARE_TAG_RE.sub("", _LARK_TAG_RE.sub("", text))


def render_line(line: Line, fmt: str) -> str:
    """Render one Line for 'tg' (HTML), 'lark' (card markdown) or 'plain'."""
    if line.kind == "blank":
        return ""
    if line.kind == "rule":
        return "———" if fmt == "tg" else "---"

    parts: list[str] = []
    for span in line.spans:
        if fmt == "tg":
            body = tg_escape(span.text)
            if span.kind == "bold":
                parts.append(f"<b>{body}</b>")
            elif span.kind == "code":
                parts.append(f"<code>{body}</code>")
            elif span.kind == "link":
                parts.append(f'<a href="{tg_escape_attr(span.href)}">{body}</a>')
            else:
                parts.append(body)
        elif fmt == "lark":
            body = lark_defuse(span.text)
            if span.kind == "bold":
                parts.append(f"**{body}**")
            elif span.kind == "code":
                parts.append(f"`{body}`")
            elif span.kind == "link":
                # A ')' inside the target would close the link early.
                parts.append(f"[{body}]({lark_defuse(span.href).replace(')', '%29')})")
            else:
                parts.append(body)
        else:  # plain
            # Defused here too: the plain renderer feeds Lark's `text` message
            # type, which still parses <at> mentions out of its content.
            if span.kind == "link":
                label = lark_defuse(span.text.strip())
                parts.append(span.href if label == span.href else f"{label}: {span.href}")
            else:
                parts.append(lark_defuse(span.text))

    body = "".join(parts)
    if fmt != "tg":
        # Defuse the joined line as well: a tag split across two adjacent
        # spans survives per-span defusing and is reassembled by the join.
        body = lark_defuse(body)
    if line.kind == "bullet":
        return ("• " if fmt == "tg" else "- ") + body
    if line.kind == "ordered":
        return f"{line.prefix}{body}"
    if line.kind == "quote":
        return f"» {body}"
    return body


def line_to_post_paragraph(line: Line) -> list[dict]:
    """Render one Line as a Lark `post` paragraph (a list of tag objects)."""
    if line.kind == "blank":
        return [{"tag": "text", "text": ""}]
    if line.kind == "rule":
        return [{"tag": "text", "text": "---"}]
    prefix = {"bullet": "- ", "ordered": line.prefix, "quote": "» "}.get(line.kind, "")

    tags: list[dict] = []
    if prefix:
        tags.append({"tag": "text", "text": prefix})
    for span in line.spans:
        if span.kind == "link":
            tags.append({"tag": "a", "text": lark_defuse(span.text) or span.href,
                         "href": span.href})
        elif span.kind == "bold":
            tags.append({"tag": "text", "text": lark_defuse(span.text), "style": ["bold"]})
        else:
            tags.append({"tag": "text", "text": lark_defuse(span.text)})
    return tags or [{"tag": "text", "text": ""}]


# --------------------------------------------------------------------------- #
# Length measurement & packing
# --------------------------------------------------------------------------- #


def tg_units(text: str) -> int:
    """Length in the unit Telegram's 4096 cap uses.

    TDLib counts Unicode code points (`len`), while message entity offsets are in
    UTF-16 code units. Gating on the larger of the two satisfies both readings at
    no practical cost.
    """
    return max(len(text), len(text.encode("utf-16-le")) // 2)


def utf8_bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def measure_for(fmt: str):
    return tg_units if fmt == "tg" else utf8_bytes


def render_chunk(lines: list[Line], fmt: str) -> str:
    return "\n".join(render_line(line, fmt) for line in lines)


_SENTENCE_RE = re.compile(r"(?<=[。！？；：!?;])\s*|(?<=[.!?])\s+")


def split_oversized(line: Line, budget: int, fmt: str) -> list[Line]:
    """Break one over-budget Line into several plain-text Lines.

    Markup is dropped first, so a hard cut can never bisect a tag or an HTML
    entity reference. Losing bold on one very long line beats a rejected message.
    """
    measure = measure_for(fmt)
    text = line.plain
    if not text:
        return [line]

    def size(piece: str) -> int:
        return measure(render_line(text_line(piece), fmt))

    pending = [p for p in _SENTENCE_RE.split(text) if p] or [text]
    grouped: list[str] = []
    current = ""
    for piece in pending:
        # _SENTENCE_RE splits after CJK punctuation with a zero-width separator,
        # so rejoining with a space would insert whitespace that was never in the
        # source. Only the ASCII terminators get their space back -- and the set
        # must match the ASCII half of _SENTENCE_RE exactly, ";" included.
        joiner = " " if current and current[-1] in ".!?;" else ""
        candidate = (current + joiner + piece) if current else piece
        if current and size(candidate) > budget:
            grouped.append(current)
            current = piece
        else:
            current = candidate
    if current:
        grouped.append(current)

    # Anything still too long has no sentence boundary left: hard-cut it.
    # The cut point is found by binary search rather than by scaling the length,
    # because cost per character is not uniform -- an emoji is 1 code point but
    # 2 Telegram units, a CJK character is 1 character but 3 UTF-8 bytes -- and a
    # proportional estimate overshoots the budget on mixed text.
    final: list[str] = []
    for piece in grouped:
        while len(piece) > 1 and size(piece) > budget:
            lo, hi, best = 1, len(piece) - 1, 0
            while lo <= hi:
                mid = (lo + hi) // 2
                if size(piece[:mid] + "…") <= budget:
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            if best == 0:
                best = 1  # degenerate: emit at least one character per cut
            final.append(piece[:best] + "…")
            piece = piece[best:]
        if piece:
            final.append(piece)

    logger.warning("A line from %s exceeded the chunk budget; split into %d plain piece(s).",
                   line.owner or "the digest", len(final))
    return [text_line(p, owner=line.owner) for p in final]


def pack(lines: list[Line], budget: int, fmt: str, header_reserve: int) -> list[list[Line]]:
    """Greedy pack of atomic Lines into chunks that fit `budget`."""
    measure = measure_for(fmt)
    effective = max(1, budget - header_reserve)
    chunks: list[list[Line]] = []
    current: list[Line] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        while current and current[-1].kind == "blank":
            current.pop()
        if current:
            chunks.append(current)
        current = []
        current_len = 0

    for line in lines:
        cost = measure(render_line(line, fmt)) + 1  # +1 for the joining newline
        if cost > effective:
            for piece in split_oversized(line, effective, fmt):
                piece_cost = measure(render_line(piece, fmt)) + 1
                if current and current_len + piece_cost > effective:
                    flush()
                current.append(piece)
                current_len += piece_cost
            continue
        if current and current_len + cost > effective:
            flush()
        if not current and line.kind == "blank":
            continue  # never open a chunk with a blank line
        current.append(line)
        current_len += cost
    flush()
    return chunks


# --------------------------------------------------------------------------- #
# Digest composition
# --------------------------------------------------------------------------- #


def show_name(data: dict) -> str:
    """The show name, across all three shapes the API uses for it."""
    value = data.get("shows")
    if value is None:
        value = data.get("show")
    if isinstance(value, dict):
        return _sanitize_inline(value.get("name"), 120)
    if isinstance(value, str):
        return _sanitize_inline(value, 120)
    return ""


def _language_side(data: dict, use_alt: bool) -> dict | None:
    suffix = "_alt" if use_alt else ""
    payload = {
        "title": _sanitize_inline(data.get(f"title{suffix}"), 300),
        "dek": _sanitize_inline(data.get(f"dek{suffix}"), 1200),
        "tldr": data.get(f"tldr_md{suffix}") or "",
        "digest": data.get(f"digest_md{suffix}") or "",
    }
    return payload if any(payload.values()) else None


def pick_language_fields(data: dict, want: str) -> list[dict]:
    """Return the title/dek/summary set(s) for the requested language.

    Each episode carries one primary language plus an `_alt` mirror of the other.
    'zh' / 'en' pick whichever side matches, 'primary' never prefers the mirror,
    and 'both' returns the primary followed by the mirror.
    """
    alt_lang = str(data.get("lang_alt") or "").strip().upper()
    want = (want or "zh").lower()

    if want == "both":
        order = [False, True]
    elif want == "primary":
        order = [False]
    elif alt_lang and want.upper() == alt_lang:
        order = [True, False]
    else:
        order = [False, True]

    variants: list[dict] = []
    for use_alt in order:
        side = _language_side(data, use_alt)
        if side:
            variants.append(side)
        if want != "both" and variants:
            break

    if not variants:
        return [{"title": "", "dek": "", "tldr": "", "digest": ""}]
    if want != "both":
        # A half-translated episode still renders: fill blanks from the other side.
        other = _language_side(data, not order[0])
        if other:
            for key, value in variants[0].items():
                if not value:
                    variants[0][key] = other.get(key, "")
    return variants


def truncate_markdown(md: str, limit: int) -> str:
    """Cap one episode's summary, preferring a paragraph then a line boundary."""
    if limit <= 0 or len(md) <= limit:
        return md
    head = md[:limit]
    for sep in ("\n\n", "\n"):
        cut = head.rfind(sep)
        if cut > limit * 0.5:
            return head[:cut].rstrip() + "\n\n…"
    return head.rstrip() + "…"


def episode_lines(ep: Episode, index: int, cfg: Config) -> list[Line]:
    """Render one episode as a self-contained block of Lines."""
    data = ep.data
    slug = ep.slug
    lines: list[Line] = []
    variants = pick_language_fields(data, cfg.language)

    fallback_title = _sanitize_inline(data.get("title") or data.get("title_orig") or slug, 300)
    heading_text = variants[0].get("title") or fallback_title
    lines.append(Line("heading", [Span("bold", f"{index}. {heading_text}")], owner=slug))

    meta_bits: list[str] = []
    if cfg.include_show:
        name = show_name(data)
        if name:
            meta_bits.append(name)
    if cfg.include_duration:
        duration = data.get("duration_min")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0:
            meta_bits.append(f"{int(duration)} min")
    published = _parse_instant(data.get("published_at"))
    if published is not None:
        meta_bits.append(published.astimezone(cfg.tz).strftime("%Y-%m-%d %H:%M"))
    elif isinstance(data.get("date"), str):
        meta_bits.append(_sanitize_inline(data.get("date"), 20))
    if ep.degraded:
        meta_bits.append("summary unavailable")
    if meta_bits:
        lines.append(text_line(" · ".join(meta_bits), owner=slug))

    for position, variant in enumerate(variants):
        if position > 0:
            alt_title = variant.get("title")
            if alt_title and alt_title != heading_text:
                lines.append(blank_line(slug))
                lines.append(Line("heading", [Span("bold", alt_title)], owner=slug))
        body = ""
        if cfg.summary_field == "tldr":
            body = variant.get("tldr") or variant.get("digest") or ""
        elif cfg.summary_field == "digest":
            body = variant.get("digest") or variant.get("tldr") or ""
        if cfg.include_dek or cfg.summary_field == "dek" or not body:
            dek = variant.get("dek")
            if dek:
                lines.append(blank_line(slug))
                lines.append(text_line(dek, owner=slug))
        if body:
            lines.append(blank_line(slug))
            lines.extend(parse_markdown(truncate_markdown(body, cfg.max_summary_chars), slug))

    links: list[Span] = []
    if cfg.include_episode_link:
        links.append(Span("link", "BidClub", EPISODE_PAGE.format(base=cfg.api_base, slug=slug)))
    if cfg.include_source_link:
        # scrub() here too: an unpaired surrogate in a URL survives json.loads
        # but raises UnicodeEncodeError in every length measurement downstream.
        source = scrub(data.get("source_url"))
        if safe_url(source):
            label = _sanitize_inline(data.get("source_label"), 40) or "Source"
            links.append(Span("link", label, source.strip()))
    if links:
        spans: list[Span] = []
        for position, span in enumerate(links):
            if position:
                spans.append(Span("text", " · "))
            spans.append(span)
        lines.append(blank_line(slug))
        lines.append(Line("text", spans, owner=slug))
    return lines


def build_digest_lines(episodes: list[Episode], cfg: Config) -> tuple[list[Line], list[str]]:
    """Render the whole digest. Returns (lines, slugs_named_only_in_the_tail)."""
    shown = episodes[:cfg.max_episodes]
    overflow = episodes[cfg.max_episodes:]
    lines: list[Line] = []

    for index, ep in enumerate(shown, start=1):
        if lines:
            lines.append(blank_line())
            lines.append(Line("rule"))
            lines.append(blank_line())
        lines.extend(episode_lines(ep, index, cfg))

    if overflow:
        lines.append(blank_line())
        lines.append(Line("rule"))
        lines.append(Line("heading", [Span("bold", f"+{len(overflow)} more episodes")]))
        for ep in overflow:
            title = _sanitize_inline(ep.data.get("title") or ep.slug, 200)
            lines.append(Line("bullet",
                              [Span("link", title,
                                    EPISODE_PAGE.format(base=cfg.api_base, slug=ep.slug))],
                              owner=ep.slug))
    return lines, [ep.slug for ep in overflow]


def _format_day(cfg: Config, day: date) -> str:
    try:
        return day.strftime(cfg.date_format)
    except (ValueError, TypeError):
        return day.isoformat()


def format_header(cfg: Config, day: date, index: int, total: int, count: int) -> str:
    template = cfg.header_template or "{title} · {date} ({index}/{total})"
    try:
        return template.format(title=cfg.title, date=_format_day(cfg, day),
                               index=index, total=total, count=count)
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning("Invalid [output] header_template (%s); using the default.", exc)
        return f"{cfg.title} · {_format_day(cfg, day)} ({index}/{total})"


def format_footer(cfg: Config, day: date, count: int) -> str:
    if not cfg.footer_template:
        return ""
    try:
        return cfg.footer_template.format(count=count, date=_format_day(cfg, day),
                                          source="https://bidclub.ai")
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning("Invalid [output] footer_template (%s); dropping the footer.", exc)
        return ""


@dataclasses.dataclass
class Chunk:
    lines: list[Line]
    index: int
    total: int
    header: str
    depth: int = 0     # how many times this chunk has already been halved

    @property
    def slugs(self) -> set[str]:
        return {line.owner for line in self.lines if line.owner}


def build_chunks(episodes: list[Episode], cfg: Config, day: date, fmt: str,
                 budget: int, max_chunks: int) -> tuple[list[Chunk], list[str]]:
    """Render, pack and header the digest for one platform."""
    lines, dropped = build_digest_lines(episodes, cfg)
    count = len(episodes)
    footer = format_footer(cfg, day, count)
    if footer:
        lines.append(blank_line())
        lines.append(text_line(footer))

    measure = measure_for(fmt)
    # Reserve the worst-case header up front -- escaped, since that is what is
    # actually prepended -- so the real header can never push a chunk over budget.
    worst_header = format_header(cfg, day, 999, 999, 999)
    header_reserve = measure(tg_escape(worst_header) if fmt == "tg" else worst_header) + 2
    packed = pack(lines, budget, fmt, header_reserve)

    if len(packed) > max_chunks:
        titles = {ep.slug: _sanitize_inline(ep.data.get("title") or ep.slug, 200)
                  for ep in episodes}

        def tail_for(keep: int) -> tuple[list[str], list[list[Line]]]:
            """Slugs beyond `keep` chunks, rendered as a packed link-only tail."""
            seen: set[str] = set()
            slugs = [line.owner for group in packed[keep:] for line in group
                     if line.owner and not (line.owner in seen or seen.add(line.owner))]
            lines_ = [Line("heading", [Span("bold", f"+{len(slugs)} more episodes")])]
            for slug in slugs:
                lines_.append(Line("bullet",
                                   [Span("link", titles.get(slug) or slug,
                                         EPISODE_PAGE.format(base=cfg.api_base, slug=slug))],
                                   owner=slug))
            # The tail is a chunk like any other and MUST go through pack(): a
            # 150-episode window renders 150 bullets, many times either platform's
            # hard limit, and an over-limit chunk is skipped at send time -- which
            # would lose the very episodes the tail exists to announce.
            return slugs, pack(lines_, budget, fmt, header_reserve)

        # Give the tail as many of the max_chunks slots as it actually needs, then
        # keep as much full content as still fits. Converges in a couple of passes.
        keep = max(0, max_chunks - 1)
        tail_slugs, tail_groups = tail_for(keep)
        for _ in range(5):
            fitted = max(0, max_chunks - len(tail_groups))
            if fitted >= keep:
                break
            keep = fitted
            tail_slugs, tail_groups = tail_for(keep)

        lost: list[str] = []
        if keep + len(tail_groups) > max_chunks:
            tail_groups = tail_groups[:max(1, max_chunks - keep)]
            emitted = {line.owner for group in tail_groups for line in group if line.owner}
            lost = [s for s in tail_slugs if s not in emitted]
            logger.warning("%d episode(s) do not fit even the link-only tail and are not "
                           "announced at all: %s", len(lost), ", ".join(lost))
            tail_slugs = [s for s in tail_slugs if s in emitted]
            # The heading was rendered from the pre-truncation slug list; correct
            # it so the message cannot promise more links than it carries. The
            # replacement is never longer than the original, so the chunk still fits.
            for line in tail_groups[0]:
                if line.kind == "heading":
                    line.spans = [Span("bold", f"+{len(tail_slugs)} more episodes")]
                    break

        logger.warning("The digest needed %d chunks but the cap is %d; %d episode(s) were "
                       "reduced to a link-only tail across %d chunk(s).",
                       len(packed), max_chunks, len(tail_slugs), len(tail_groups))
        packed = packed[:keep] + tail_groups
        # build_digest_lines already returned the max_episodes overflow slugs and
        # those same episodes reappear here. Dedupe, and subtract anything the
        # truncation above dropped -- reporting a slug as "announced by link only"
        # when it appears in no chunk at all is worse than not reporting it.
        lost_set = set(lost)
        dropped = [x for x in dict.fromkeys(dropped + tail_slugs) if x not in lost_set]

    total = len(packed)
    chunks = [Chunk(lines=group, index=i, total=total,
                    header=format_header(cfg, day, i, total, count))
              for i, group in enumerate(packed, start=1)]
    return chunks, dropped


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #


class _TagBalanceChecker(HTMLParser):
    """Verify that a rendered chunk has no unbalanced Telegram HTML entity."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.broken = False

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.broken = True

    def balanced(self) -> bool:
        return not self.broken and not self.stack


def html_is_balanced(text: str) -> bool:
    checker = _TagBalanceChecker()
    try:
        checker.feed(text)
        checker.close()
    except Exception:
        return False
    return checker.balanced()


def strip_html(text: str) -> str:
    """Turn a rendered HTML chunk back into plain text (parse-error fallback)."""
    without_tags = re.sub(r"<[^>]+>", "", text)
    return (without_tags
            .replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&amp;", "&"))


def _tg_description(status: int, body: dict | None) -> str:
    if body and isinstance(body.get("description"), str):
        return f"HTTP {status}: {body['description']}"
    return "network error" if status == 0 else f"HTTP {status}"


class TelegramSender:
    """Chunked sendMessage with the documented retry and error semantics."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.session = requests.Session()
        self.token_dead = False

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.session.close()

    def call(self, method: str, payload: dict) -> tuple[int, dict | None]:
        url = TELEGRAM_API.format(token=self.cfg.telegram_bot_token, method=method)
        try:
            resp = self.session.post(url, json=payload, timeout=self.cfg.request_timeout)
        except requests.exceptions.RequestException as exc:
            logger.warning("Telegram %s failed: %s", method, exc)
            return 0, None
        try:
            body = resp.json()
        except ValueError:
            body = None  # a 5xx from the edge returns HTML, not JSON
        return resp.status_code, body if isinstance(body, dict) else None

    def get_me(self) -> tuple[bool, str]:
        status, body = self.call("getMe", {})
        if status == 200 and body and body.get("ok"):
            user = body.get("result") or {}
            return True, f"@{user.get('username', '?')} (id={user.get('id', '?')})"
        return False, _tg_description(status, body)

    def get_chat(self, chat_id: str) -> tuple[bool, str]:
        status, body = self.call("getChat", {"chat_id": chat_id})
        if status == 200 and body and body.get("ok"):
            chat = body.get("result") or {}
            label = chat.get("title") or chat.get("username") or chat.get("first_name") or "?"
            return True, f"{chat.get('type', '?')} {label}"
        return False, _tg_description(status, body)

    def send_text(self, chat_id: str, text: str, *, silent: bool) -> str:
        """Send one already-sized message. Returns 'ok' / 'failed' / 'abort'.

        'abort' means the destination is permanently unusable and the remaining
        chunks for it must be skipped rather than burning rate budget.
        """
        if not text.strip():
            logger.warning("Refusing to send an empty Telegram chunk.")
            return "failed"
        if self.token_dead:
            return "abort"

        use_html = self.cfg.telegram_parse_mode == "html"
        if use_html and not html_is_balanced(text):
            logger.error("The rendered chunk has unbalanced HTML; sending it as plain text.")
            text = strip_html(text)
            use_html = False
        if use_html and text.count("<a href=") + text.count("<b>") + text.count("<code>") \
                > TG_MAX_ENTITIES:
            logger.warning("This chunk carries many entities; Telegram silently drops them "
                           "past roughly 100 per message.")

        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": bool(silent),
            "link_preview_options": {"is_disabled": True},
        }
        if use_html:
            payload["parse_mode"] = "HTML"
        if self.cfg.telegram_thread_id:
            payload["message_thread_id"] = self.cfg.telegram_thread_id

        # Three independent budgets: transient retries (5xx / network), flood
        # waits, and one-shot corrections. Sharing a single counter meant a
        # correction computed on the final attempt was discarded without ever
        # being sent, and a 429 on the final attempt slept up to 300 s for nothing.
        transient_left = self.cfg.max_retries
        floods_left = 3
        corrections_left = 2

        while True:
            status, body = self.call("sendMessage", payload)
            if status == 200 and body and body.get("ok"):
                return "ok"
            description = _tg_description(status, body).lower()

            if status == 429:
                retry_after = 5
                params = (body or {}).get("parameters")
                if isinstance(params, dict):
                    with contextlib.suppress(TypeError, ValueError):
                        retry_after = int(params.get("retry_after", retry_after))
                retry_after = max(1, min(retry_after, 300))
                floods_left -= 1
                if floods_left < 0:
                    logger.error("Telegram kept flood-waiting (%ds); giving up on this chunk.",
                                 retry_after)
                    return "failed"
                logger.warning("Telegram flood wait: sleeping %ds.", retry_after)
                time.sleep(retry_after + random.uniform(0.5, 1.0))
                continue

            if status == 401:
                logger.error("Telegram rejected the bot token (401). Disabling the channel.")
                self.token_dead = True
                return "abort"
            if status == 403:
                logger.error("Telegram refused chat %s permanently: %s", chat_id, description)
                return "abort"

            if status == 400:
                if "can't parse entities" in description and use_html and corrections_left > 0:
                    corrections_left -= 1
                    logger.error("Telegram could not parse the chunk (%s); resending it as "
                                 "plain text.", description)
                    payload.pop("parse_mode", None)
                    payload["text"] = strip_html(payload["text"])
                    use_html = False
                    continue
                migrate = ((body or {}).get("parameters") or {}).get("migrate_to_chat_id")
                if migrate and corrections_left > 0:
                    corrections_left -= 1
                    logger.error("Chat %s was upgraded to supergroup %s; update config.ini. "
                                 "Retrying once against the new id.", chat_id, migrate)
                    payload["chat_id"] = migrate
                    continue
                if "message text is empty" in description:
                    logger.warning("Telegram rejected an empty chunk; skipping it.")
                    return "failed"
                if "too long" in description:
                    logger.error("Chunk rejected as too long (%d units / %d bytes) -- this is a "
                                 "splitter bug.", tg_units(payload["text"]),
                                 utf8_bytes(payload["text"]))
                    return "failed"
                if "chat not found" in description or "chat_id is empty" in description:
                    logger.error("Telegram chat %s is unusable: %s", chat_id, description)
                    return "abort"
                logger.error("Telegram rejected the chunk: %s", description)
                return "failed"

            attempt = self.cfg.max_retries - transient_left + 1
            transient_left -= 1
            logger.warning("Telegram send failed (attempt %d/%d): %s",
                           attempt, self.cfg.max_retries, description)
            if transient_left <= 0:
                return "failed"
            time.sleep(self.cfg.retry_backoff * (2 ** (attempt - 1)) + random.uniform(0, 1))


# --------------------------------------------------------------------------- #
# Lark / Feishu
# --------------------------------------------------------------------------- #


def lark_sign(timestamp: int, secret: str) -> str:
    """Feishu custom-bot signature.

    The concatenation "<timestamp>\\n<secret>" is the HMAC-SHA256 *key* and the
    signed message is the EMPTY string -- not the request body. `timestamp` is in
    seconds and must be within one hour of the server's clock.
    """
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _lark_result(resp) -> tuple[int | None, str]:
    """Extract (code, message) from a Lark response.

    A custom bot answers HTTP 200 even on failure, in either a lowercase (`code`/
    `msg`) or a legacy PascalCase (`StatusCode`/`StatusMessage`) envelope. Require
    an explicit zero in one of them: a missing key must never read as success.
    """
    try:
        data = resp.json()
    except ValueError:
        return None, f"non-JSON body: {(resp.text or '')[:200]}"
    if not isinstance(data, dict):
        return None, f"unexpected body type {type(data).__name__}"
    code = data.get("code", data.get("StatusCode"))
    message = str(data.get("msg") or data.get("StatusMessage") or "")
    if isinstance(code, bool) or not isinstance(code, int):
        return None, message or json.dumps(data, ensure_ascii=False)[:200]
    return code, message


def _lark_hint(code: int) -> str:
    return {
        19001: "The webhook token is invalid or was regenerated -- copy the new URL from the "
               "group's bot settings.",
        19021: "Signature check failed: wrong [lark] secret, or the host clock is more than an "
               "hour off (the timestamp is in seconds).",
        19022: "This host's IP is not on the bot's IP allowlist.",
        19024: "The bot requires a keyword: make sure [output] title contains it (only text "
               "and title fields are scanned).",
    }.get(code, "")


class LarkSender:
    """Custom-bot webhook sender with an interactive -> post -> text fallback."""

    LADDER = ["interactive", "post", "text"]

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.session = requests.Session()
        self.msg_type = cfg.lark_msg_type
        self.disabled = False

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.session.close()

    def build_payload(self, header: str, lines: list[Line], msg_type: str) -> dict:
        if msg_type == "interactive":
            return {
                "msg_type": "interactive",
                "card": {
                    "schema": "2.0",
                    "config": {"wide_screen_mode": True, "update_multi": True},
                    "header": {
                        "title": {"tag": "plain_text", "content": lark_defuse(header)},
                        "template": self.cfg.lark_card_template,
                    },
                    "body": {
                        "direction": "vertical",
                        "elements": [{"tag": "markdown",
                                      "content": render_chunk(lines, "lark"),
                                      "text_align": "left"}],
                    },
                },
            }
        if msg_type == "post":
            return {
                "msg_type": "post",
                "content": {"post": {self.cfg.lark_lang: {
                    "title": lark_defuse(header),
                    "content": [line_to_post_paragraph(line) for line in lines],
                }}},
            }
        return {
            "msg_type": "text",
            "content": {"text": f"{lark_defuse(header)}\n\n"
                                f"{lark_defuse(render_chunk(lines, 'plain'))}"},
        }

    def _serialize(self, payload: dict) -> bytes:
        # requests' json= helper serialises with ensure_ascii=True, which inflates
        # every CJK character from 3 bytes to a 6-byte \\uXXXX escape and silently
        # halves the usable 20 KB budget. Serialise once and post those exact bytes.
        signed = dict(payload)
        if self.cfg.lark_secret:
            timestamp = int(time.time())
            signed["timestamp"] = str(timestamp)
            signed["sign"] = lark_sign(timestamp, self.cfg.lark_secret)
        return json.dumps(signed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def body_bytes(self, payload: dict) -> int:
        return len(self._serialize(payload))

    def send(self, header: str, lines: list[Line]) -> str:
        """Send one chunk. Returns 'ok' / 'split' / 'failed' / 'abort'."""
        if self.disabled:
            return "abort"
        start = self.LADDER.index(self.msg_type) if self.msg_type in self.LADDER else 2
        saw_payload_error = False

        for msg_type in self.LADDER[start:]:
            payload = self.build_payload(header, lines, msg_type)
            size = self.body_bytes(payload)
            if size > LARK_HARD_BODY_BYTES:
                logger.warning("The Lark %s payload is %d bytes (limit %d); asking for a split.",
                               msg_type, size, LARK_HARD_BODY_BYTES)
                return "split"
            outcome = self._post(payload)
            if outcome == "ok":
                if msg_type != self.msg_type:
                    logger.warning("Lark accepted msg_type=%s after %s failed; using %s for the "
                                   "rest of this run. Consider setting [lark] msg_type = %s.",
                                   msg_type, self.msg_type, msg_type, msg_type)
                    self.msg_type = msg_type
                return "ok"
            if outcome == "abort":
                return "abort"
            if outcome == "content":
                return "failed"
            if outcome == "payload":
                saw_payload_error = True
                continue  # try the next, simpler message type
            return "failed"  # transient retries exhausted

        # Every message type rejected the body: it is probably still too big.
        return "split" if saw_payload_error else "failed"

    def _post(self, payload: dict) -> str:
        """Send one payload with retries.

        Returns 'ok' / 'payload' (malformed or oversized) / 'content' (rejected
        text) / 'abort' (configuration error) / 'failed' (transient, exhausted).
        """
        for attempt in range(1, self.cfg.max_retries + 1):
            body = self._serialize(payload)  # re-signed each attempt: the timestamp ages
            try:
                resp = self.session.post(
                    self.cfg.lark_webhook_url, data=body,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    timeout=self.cfg.request_timeout,
                )
            except requests.exceptions.RequestException as exc:
                logger.warning("Lark send failed (attempt %d/%d): %s",
                               attempt, self.cfg.max_retries, exc)
            else:
                code, message = _lark_result(resp)
                if code == 0:
                    return "ok"
                if code in LARK_TRANSIENT_CODES:
                    wait = 3 * (3 ** (attempt - 1))
                    logger.warning("Lark throttled (code=%s %s); sleeping %ds.",
                                   code, message, wait)
                    time.sleep(wait)
                    continue
                if code in LARK_PAYLOAD_CODES:
                    logger.warning("Lark rejected the payload (code=%s %s).", code, message)
                    return "payload"
                if code in LARK_CONTENT_CODES:
                    logger.error("Lark rejected the content (code=%s %s); dropping this chunk.",
                                 code, message)
                    return "content"
                if code in LARK_FATAL_CODES:
                    logger.error("Lark configuration error (code=%s %s). %s",
                                 code, message, _lark_hint(code))
                    self.disabled = True
                    return "abort"
                if code is None:
                    logger.warning("Lark returned an unrecognised body (HTTP %d): %s",
                                   resp.status_code, message)
                else:
                    # An unknown non-zero code is NOT assumed to be a size problem.
                    # Classifying it as 'payload' would walk the whole msg_type
                    # ladder and then halve the chunk four times -- dozens of POSTs
                    # that all fail for the same reason, against a 100/min quota.
                    logger.error("Lark send failed with an unrecognised code=%s (%s); "
                                 "dropping this chunk without splitting.", code, message)
                    return "content"
            if attempt < self.cfg.max_retries:
                time.sleep(self.cfg.retry_backoff * (2 ** (attempt - 1)))
        return "failed"


# --------------------------------------------------------------------------- #
# State ledger
# --------------------------------------------------------------------------- #

DEST_LARK = "lark"


def dest_telegram(chat_id: str) -> str:
    return f"telegram:{chat_id}"


def configured_destinations(cfg: Config) -> list[str]:
    dests: list[str] = []
    if cfg.telegram_on:
        dests.extend(dest_telegram(cid) for cid in cfg.telegram_chat_ids)
    if cfg.lark_on:
        dests.append(DEST_LARK)
    return dests


def load_state(path: Path) -> tuple[dict, bool]:
    """Load the delivery ledger. Returns (sent, is_first_run).

    A corrupt file is quarantined and treated as a first run: at worst one day is
    re-posted, never a week.
    """
    if not path.exists():
        return {}, True
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raise ValueError("the top level is not an object")
        sent = raw.get("sent", {})
        if not isinstance(sent, dict):
            raise ValueError("'sent' is not an object")
        clean: dict = {}
        for slug, entry in sent.items():
            if not isinstance(entry, dict):
                continue
            delivered = entry.get("delivered")
            clean[str(slug)] = {
                "first_seen": str(entry.get("first_seen") or ""),
                "local_date": str(entry.get("local_date") or ""),
                "title": str(entry.get("title") or ""),
                "delivered": {str(k): str(v) for k, v in delivered.items()}
                if isinstance(delivered, dict) else {},
            }
        return clean, False
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.error("State file %s is unreadable (%s); treating this as a first run.", path, exc)
        with contextlib.suppress(OSError):
            path.rename(path.with_name(f"{path.name}.corrupt-{int(time.time())}"))
        return {}, True


def save_state(path: Path, sent: dict, retention_days: int) -> bool:
    """Atomically persist the ledger, pruning entries past the retention window.

    Returns False when nothing was persisted. The caller must surface that: a
    lost ledger means the next run sees a first run, re-seeds the window and
    silently drops that day rather than catching up.
    """
    today = datetime.now(timezone.utc).date()
    pruned: dict = {}
    for slug, entry in sent.items():
        raw_day = entry.get("local_date")
        try:
            entry_day = date.fromisoformat(raw_day) if raw_day else None
        except (TypeError, ValueError):
            entry_day = None
        if entry_day is not None and (today - entry_day).days > retention_days:
            continue
        pruned[slug] = entry

    payload = {
        "version": STATE_VERSION,
        "service": "bidclub_digest",
        "script_version": VERSION,
        "updated": datetime.now(timezone.utc).isoformat(),
        "sent": pruned,
    }
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError) as exc:
        # ValueError covers UnicodeEncodeError: an unpaired surrogate that
        # survived json.loads would otherwise take the whole ledger down.
        logger.error("Failed to write the state file %s: %s: %s",
                     path, type(exc).__name__, exc)
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        return False


def ensure_entry(sent: dict, ep: Episode, now_iso: str) -> dict:
    entry = sent.get(ep.slug)
    if entry is None:
        entry = {
            "first_seen": now_iso,
            "local_date": ep.local_date.isoformat(),
            "title": _sanitize_inline(ep.data.get("title") or ep.slug, 160),
            "delivered": {},
        }
        sent[ep.slug] = entry
    else:
        entry.setdefault("delivered", {})
        entry["local_date"] = ep.local_date.isoformat()
    return entry


@contextlib.contextmanager
def instance_lock(lock_path: Path):
    """Cross-process lock so two overlapping runs cannot double-post.

    Yields True when this process may proceed, False only when another process
    demonstrably holds the lock. Any other problem (an unopenable path, a
    filesystem with no lock support) fails OPEN with a warning -- a digest that
    is never sent is a worse outcome than a theoretical double-post.
    """
    handle = None
    acquired = True
    if fcntl is not None:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path, "w")
        except OSError as exc:
            logger.warning("Could not create the lock file %s (%s); proceeding without a lock.",
                           lock_path, exc)
            handle = None
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                acquired = False
            except OSError as exc:
                # ENOLCK / EOPNOTSUPP on NFS or an exotic filesystem: locking is
                # unavailable here, which is not the same as "someone else holds it".
                logger.warning("Locking is unsupported on %s (%s); proceeding without a lock.",
                               lock_path, exc)
    try:
        # The body runs OUTSIDE the try/except above: an OSError raised by the
        # caller must propagate, not be swallowed as a lock-file failure.
        yield acquired
    finally:
        if handle is not None:
            if acquired:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                handle.close()


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #


def deliver_telegram(sender: TelegramSender, cfg: Config, chat_id: str,
                     chunks: list[Chunk]) -> tuple[set[str], int, bool]:
    """Send every chunk to one chat. Returns (failed_slugs, accepted_chunks, aborted)."""
    failed: set[str] = set()
    accepted = 0
    for position, chunk in enumerate(chunks):
        text = f"{tg_escape(chunk.header)}\n\n{render_chunk(chunk.lines, 'tg')}".strip()
        if tg_units(text) > TG_HARD_CHARS or utf8_bytes(text) > TG_HARD_BYTES:
            logger.error("Chunk %d/%d exceeds Telegram's hard limit (%d units, %d bytes); "
                         "skipping it.", chunk.index, chunk.total,
                         tg_units(text), utf8_bytes(text))
            failed |= chunk.slugs
            continue
        silent = cfg.telegram_disable_notification or position > 0
        outcome = sender.send_text(chat_id, text, silent=silent)
        if outcome == "abort":
            for pending in chunks[position:]:
                failed |= pending.slugs
            return failed, accepted, True
        if outcome == "ok":
            accepted += 1
        else:
            failed |= chunk.slugs
        if position < len(chunks) - 1 and cfg.telegram_delay_seconds > 0:
            time.sleep(cfg.telegram_delay_seconds)
    return failed, accepted, False


def deliver_lark(sender: LarkSender, cfg: Config,
                 chunks: list[Chunk]) -> tuple[set[str], int, bool]:
    """Send every chunk to the Lark webhook. Returns (failed_slugs, accepted, aborted)."""
    failed: set[str] = set()
    accepted = 0
    queue: list[Chunk] = list(chunks)
    while queue:
        chunk = queue.pop(0)
        outcome = sender.send(chunk.header, chunk.lines)
        if outcome == "split":
            if len(chunk.lines) > 1 and chunk.depth < LARK_MAX_SPLIT_DEPTH:
                middle = len(chunk.lines) // 2
                logger.warning("Splitting an oversized Lark chunk into two halves (depth %d).",
                               chunk.depth + 1)
                # The second half carries a "(cont.)" header so two cards cannot
                # claim to be the same numbered part. The original header text is
                # kept in front of it, because a bot configured with a required
                # keyword only has its title scanned for that keyword.
                queue.insert(0, Chunk(chunk.lines[middle:], chunk.index, chunk.total,
                                      f"{chunk.header} (cont.)", chunk.depth + 1))
                queue.insert(0, Chunk(chunk.lines[:middle], chunk.index, chunk.total,
                                      chunk.header, chunk.depth + 1))
                continue
            logger.error("A Lark chunk is still unsendable after %d split(s); dropping it.",
                         chunk.depth)
            failed |= chunk.slugs
            continue
        if outcome == "abort":
            failed |= chunk.slugs
            for pending in queue:
                failed |= pending.slugs
            return failed, accepted, True
        if outcome == "ok":
            accepted += 1
        else:
            failed |= chunk.slugs
        if queue and cfg.lark_delay_seconds > 0:
            time.sleep(cfg.lark_delay_seconds)
    return failed, accepted, False


# --------------------------------------------------------------------------- #
# Preflight / test
# --------------------------------------------------------------------------- #


def run_check(cfg: Config) -> int:
    """Validate every configured destination without sending a digest."""
    ok = True
    if cfg.telegram_on:
        sender = TelegramSender(cfg)
        try:
            good, detail = sender.get_me()
            print(f"[{'PASS' if good else 'FAIL'}] telegram getMe: {detail}")
            ok = ok and good
            if good:
                for chat_id in cfg.telegram_chat_ids:
                    chat_ok, chat_detail = sender.get_chat(chat_id)
                    print(f"[{'PASS' if chat_ok else 'FAIL'}] telegram chat {chat_id}: "
                          f"{chat_detail}")
                    ok = ok and chat_ok
        finally:
            sender.close()
    else:
        print("[SKIP] telegram: not configured")

    if cfg.lark_on:
        parsed = urlparse(cfg.lark_webhook_url)
        shape_ok = parsed.scheme == "https" and "/open-apis/bot/v2/hook/" in parsed.path
        print(f"[{'PASS' if shape_ok else 'FAIL'}] lark webhook URL shape: "
              f"{parsed.scheme}://{parsed.netloc}/open-apis/bot/v2/hook/***")
        ok = ok and shape_ok
        if cfg.lark_secret:
            sign_ok = lark_sign(1599360473, "SECRET") == \
                "dkDGhTZacsd3yXW3YtXxmQjKIazeY68phrEMdV1uoMo="
            print(f"[{'PASS' if sign_ok else 'FAIL'}] lark signature algorithm self-test")
            ok = ok and sign_ok
        else:
            print("[SKIP] lark signature: no secret configured")
    else:
        print("[SKIP] lark: not configured")

    print(f"[INFO] timezone={cfg.timezone_name} basis={cfg.date_basis} "
          f"lookback={cfg.lookback_days}d language={cfg.language} field={cfg.summary_field}")
    return 0 if ok else 2


def run_test(cfg: Config) -> int:
    """Push a short live message through the real send path."""
    now = datetime.now(cfg.tz)
    header = f"{cfg.title} · test"
    lines = [
        text_line(f"bidclub_digest {VERSION} test notification."),
        text_line(f"Local time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}"),
        Line("text", [Span("text", "API docs: "),
                      Span("link", "bidclub.ai/api-docs", "https://bidclub.ai/api-docs")]),
    ]
    delivered = 0
    attempted = 0

    if cfg.telegram_on:
        sender = TelegramSender(cfg)
        try:
            text = f"{tg_escape(header)}\n\n{render_chunk(lines, 'tg')}"
            for chat_id in cfg.telegram_chat_ids:
                attempted += 1
                outcome = sender.send_text(chat_id, text, silent=False)
                print(f"[{'PASS' if outcome == 'ok' else 'FAIL'}] telegram {chat_id}")
                delivered += 1 if outcome == "ok" else 0
        finally:
            sender.close()
    else:
        print("[SKIP] telegram: not configured")

    if cfg.lark_on:
        sender = LarkSender(cfg)
        try:
            attempted += 1
            outcome = sender.send(header, lines)
            print(f"[{'PASS' if outcome == 'ok' else 'FAIL'}] lark webhook")
            delivered += 1 if outcome == "ok" else 0
        finally:
            sender.close()
    else:
        print("[SKIP] lark: not configured")

    # Every ENABLED destination must accept the message: reporting success
    # because one of two channels worked hides a broken channel.
    return 0 if attempted and delivered == attempted else 2


# --------------------------------------------------------------------------- #
# Main run
# --------------------------------------------------------------------------- #


def resolve_target_day(cfg: Config, override: str | None) -> date:
    if override:
        try:
            return date.fromisoformat(override)
        except ValueError as exc:
            raise ValueError(f"Invalid --date {override!r}: expected YYYY-MM-DD") from exc
    return datetime.now(timezone.utc).astimezone(cfg.tz).date() - timedelta(days=1)


def notify_empty(cfg: Config, day: date) -> None:
    """Push the configured "nothing published" notice."""
    try:
        text = cfg.empty_message.format(date=_format_day(cfg, day))
    except (KeyError, IndexError, ValueError):
        text = f"No BidClub episodes published on {day.isoformat()}."
    lines = [text_line(text)]
    header = format_header(cfg, day, 1, 1, 0)
    if cfg.telegram_on:
        sender = TelegramSender(cfg)
        try:
            body = f"{tg_escape(header)}\n\n{render_chunk(lines, 'tg')}"
            for chat_id in cfg.telegram_chat_ids:
                sender.send_text(chat_id, body, silent=True)
        finally:
            sender.close()
    if cfg.lark_on:
        sender = LarkSender(cfg)
        try:
            sender.send(header, lines)
        finally:
            sender.close()


def dry_run_preview(cfg: Config, pending_by_dest: dict[str, list[Episode]], day: date) -> int:
    """Render and print every chunk without touching the network or the ledger."""
    preview = LarkSender(cfg)
    try:
        for dest, pending in pending_by_dest.items():
            is_telegram = dest.startswith("telegram:")
            fmt = "tg" if is_telegram else "lark"
            budget = cfg.telegram_chunk_chars if is_telegram else cfg.lark_chunk_bytes
            cap = cfg.telegram_max_chunks if is_telegram else cfg.lark_max_chunks
            chunks, dropped = build_chunks(pending, cfg, day, fmt, budget, cap)
            print(f"\n===== {dest} — {len(pending)} episode(s), {len(chunks)} chunk(s) =====")
            if dropped:
                print(f"(link-only tail: {', '.join(dropped)})")
            for chunk in chunks:
                text = (f"{tg_escape(chunk.header) if is_telegram else chunk.header}\n\n"
                        f"{render_chunk(chunk.lines, fmt)}")
                if is_telegram:
                    size = (f"units={tg_units(text)}/{cfg.telegram_chunk_chars} "
                            f"hard={TG_HARD_CHARS} bytes={utf8_bytes(text)}")
                else:
                    payload = preview.build_payload(chunk.header, chunk.lines, cfg.lark_msg_type)
                    size = (f"content_bytes={utf8_bytes(render_chunk(chunk.lines, fmt))}"
                            f"/{cfg.lark_chunk_bytes} "
                            f"body_bytes={preview.body_bytes(payload)}/{LARK_HARD_BODY_BYTES}")
                print(f"\n----- [{chunk.index}/{chunk.total}] {size} -----")
                print(text)
    finally:
        preview.close()
    return 0


def run_digest(cfg: Config, args: argparse.Namespace) -> int:
    """Fetch, render and deliver one day's digest. Returns the process exit code."""
    tz = cfg.tz
    target_day = resolve_target_day(cfg, args.date)
    today_local = datetime.now(timezone.utc).astimezone(tz).date()
    if target_day > today_local:
        logger.warning("Target day %s is in the future (local today is %s).",
                       target_day, today_local)

    lookback = cfg.lookback_days if args.lookback is None else max(1, min(args.lookback, 30))
    window_start = target_day - timedelta(days=lookback - 1)
    logger.info("bidclub_digest %s | target=%s window=%s..%s tz=%s basis=%s",
                VERSION, target_day, window_start, target_day,
                cfg.timezone_name, cfg.date_basis)

    destinations = configured_destinations(cfg)
    if not destinations:
        logger.error("No notification channel configured.")
        return 1
    logger.info("Destinations: %s", ", ".join(destinations))

    client = BidClubClient(cfg)
    try:
        episodes, complete = collect_episodes(client, cfg, window_start, target_day)
        on_target_day = [ep for ep in episodes if ep.local_date == target_day]
        previous_day = target_day - timedelta(days=1)
        if not on_target_day and any(ep.local_date == previous_day for ep in episodes):
            logger.warning("No episodes for %s while %s has some -- BidClub ingestion may be "
                           "lagging; the lookback window will pick them up tomorrow.",
                           target_day, previous_day)

        sent, first_run = load_state(cfg.state_file)
        now_iso = datetime.now(tz).isoformat()

        if first_run and not cfg.notify_on_first_run:
            seeded = 0
            for ep in episodes:
                if ep.local_date == target_day:
                    continue
                entry = ensure_entry(sent, ep, now_iso)
                entry["seeded"] = True
                for dest in destinations:
                    entry["delivered"].setdefault(dest, now_iso)
                seeded += 1
            if seeded:
                logger.info("First run: seeded %d older episode(s) as already delivered. "
                            "Set [window] notify_on_first_run = true to push them instead.",
                            seeded)
            episodes = on_target_day

        pending_by_dest: dict[str, list[Episode]] = {}
        for dest in destinations:
            pending = [ep for ep in episodes
                       if args.force
                       or dest not in ((sent.get(ep.slug) or {}).get("delivered") or {})]
            if pending:
                pending_by_dest[dest] = pending

        if not pending_by_dest:
            if not on_target_day:
                logger.info("No episodes published on %s (window %s..%s).",
                            target_day, window_start, target_day)
                if not complete:
                    # An incomplete scan and a genuinely empty day must not
                    # produce the same reassuring notice.
                    logger.error("The scan was incomplete, so the empty-day verdict is "
                                 "not trustworthy; suppressing the empty-day notice.")
                elif cfg.notify_empty and not args.dry_run:
                    notify_empty(cfg, target_day)
            else:
                logger.info("Every episode in the window was already delivered; nothing to send.")
            if not args.dry_run and not save_state(cfg.state_file, sent,
                                                   cfg.state_retention_days):
                return 3
            return 0

        # The same Episode object is shared across destination lists, so hydrating
        # it once makes the detail available everywhere.
        unique = {ep.slug: ep for eps in pending_by_dest.values() for ep in eps}
        ordered = sorted(unique.values(),
                         key=lambda e: (e.sort_key or datetime.min.replace(tzinfo=timezone.utc),
                                        e.slug))
        deferred = hydrate_episodes(client, ordered)
        if deferred and len(deferred) == len(ordered):
            logger.error("Every detail fetch failed; sending nothing so tomorrow can retry.")
            # Persist the first-run seeding even though nothing shipped. Losing it
            # would make tomorrow a first run too, and tomorrow's seeding would
            # write TODAY's target day off as already delivered without sending it.
            if not args.dry_run:
                save_state(cfg.state_file, sent, cfg.state_retention_days)
            return 1
        if deferred:
            # Held back rather than shipped as "summary unavailable": recording
            # the degraded version as delivered would make it permanent.
            logger.warning("Holding back %d episode(s) whose summary could not be "
                           "fetched: %s", len(deferred), ", ".join(sorted(deferred)))
            pending_by_dest = {dest: [ep for ep in eps if ep.slug not in deferred]
                               for dest, eps in pending_by_dest.items()}
            pending_by_dest = {d: e for d, e in pending_by_dest.items() if e}
            if not pending_by_dest:
                logger.error("Nothing left to send after holding back the failed fetches.")
                if not args.dry_run:
                    save_state(cfg.state_file, sent, cfg.state_retention_days)
                return 3
    finally:
        client.close()

    if cfg.sort == "published_desc":
        for eps in pending_by_dest.values():
            eps.reverse()

    if args.dry_run:
        return dry_run_preview(cfg, pending_by_dest, target_day)

    if cfg.startup_jitter_seconds > 0 and not args.no_jitter:
        # Lark's own docs warn that sends landing exactly on the hour draw
        # throttling errors, and this job fires at exactly 08:00.
        delay = random.uniform(0, cfg.startup_jitter_seconds)
        logger.info("Sleeping %.1fs of startup jitter before the first send.", delay)
        time.sleep(delay)

    exit_code = 0
    any_delivered = False
    tg_sender = TelegramSender(cfg) if cfg.telegram_on else None
    lark_sender = LarkSender(cfg) if cfg.lark_on else None

    try:
        for dest in destinations:
            pending = pending_by_dest.get(dest)
            if not pending:
                continue
            if dest.startswith("telegram:"):
                chunks, dropped = build_chunks(pending, cfg, target_day, "tg",
                                               cfg.telegram_chunk_chars, cfg.telegram_max_chunks)
                failed, accepted, aborted = deliver_telegram(
                    tg_sender, cfg, dest.split(":", 1)[1], chunks)
            else:
                chunks, dropped = build_chunks(pending, cfg, target_day, "lark",
                                               cfg.lark_chunk_bytes, cfg.lark_max_chunks)
                failed, accepted, aborted = deliver_lark(lark_sender, cfg, chunks)

            delivered_count = 0
            for ep in pending:
                if ep.slug in failed:
                    continue
                ensure_entry(sent, ep, now_iso)["delivered"][dest] = now_iso
                delivered_count += 1
            if dropped:
                logger.info("Episodes announced by link only on %s: %s",
                            dest, ", ".join(dropped))
            logger.info("%s: %d/%d chunk(s) accepted, %d/%d episode(s) delivered%s.",
                        dest, accepted, len(chunks), delivered_count, len(pending),
                        " (aborted)" if aborted else "")
            if delivered_count:
                any_delivered = True
            if failed or aborted:
                exit_code = 3
    finally:
        if tg_sender:
            tg_sender.close()
        if lark_sender:
            lark_sender.close()
        state_saved = save_state(cfg.state_file, sent, cfg.state_retention_days)

    if not any_delivered:
        logger.error("Failed to send the digest to any channel.")
        return 1
    if not state_saved:
        logger.error("The digest was sent but the ledger could not be written; the next "
                     "run will treat itself as a first run and may re-send or re-seed.")
        return 3
    return exit_code


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bidclub_digest.py",
        description="Push yesterday's BidClub podcast summaries to Telegram and Lark.",
    )
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG),
                        help="path to config.ini (default: alongside this script)")
    parser.add_argument("--date", metavar="YYYY-MM-DD",
                        help="target day to report on, in the configured timezone "
                             "(default: yesterday)")
    parser.add_argument("--lookback", type=int, metavar="N",
                        help="override [window] lookback_days for this run")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="override [content] max_episodes for this run")
    parser.add_argument("--force", action="store_true",
                        help="ignore the sent ledger when selecting episodes")
    parser.add_argument("--dry-run", action="store_true",
                        help="render and print every chunk; send nothing, write nothing")
    parser.add_argument("--check", action="store_true",
                        help="validate credentials and destinations, then exit")
    parser.add_argument("--test", action="store_true",
                        help="send a short test message to every enabled channel")
    parser.add_argument("--telegram-only", action="store_true", help="disable Lark for this run")
    parser.add_argument("--lark-only", action="store_true", help="disable Telegram for this run")
    parser.add_argument("--no-jitter", action="store_true",
                        help="skip [output] startup_jitter_seconds")
    parser.add_argument("-v", "--verbose", action="store_true", help="force DEBUG logging")
    parser.add_argument("--version", action="version", version=f"bidclub_digest {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.telegram_only and args.lark_only:
        print("[ERROR] --telegram-only and --lark-only are mutually exclusive.")
        return 1

    config_path = Path(args.config).expanduser()
    try:
        parser = read_config_file(config_path)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1
    except ValueError as exc:
        logger.error("Config error: %s", exc)
        return 1

    def log_opt(option: str, fallback: str) -> str:
        try:
            return parser.get("logging", option, fallback=fallback).strip()
        except configparser.Error:
            return fallback

    def log_int(option: str, fallback: int) -> int:
        try:
            return int(log_opt(option, str(fallback)))
        except (TypeError, ValueError):
            return fallback

    # Configure logging BEFORE validating the rest of the file so that the
    # configuration warnings themselves land in the log.
    configure_logging(
        "DEBUG" if args.verbose else log_opt("level", "INFO"),
        log_opt("file", "bidclub_digest.log"),
        log_int("max_bytes", 5 * 1024 * 1024),
        log_int("backup_count", 3),
        log_opt("console", "true").lower() not in {"0", "false", "no", "off"},
    )

    try:
        cfg = load_config(parser)
    except ValueError as exc:
        logger.error("Config error: %s", exc)
        return 1

    if args.telegram_only:
        cfg.lark_enabled = False
    if args.lark_only:
        cfg.telegram_enabled = False
    if not cfg.telegram_on and not cfg.lark_on:
        logger.error("No notification channel left after applying the CLI channel filters.")
        return 1
    if args.limit is not None:
        cfg.max_episodes = max(1, min(args.limit, 100))

    if args.check:
        return run_check(cfg)
    if args.test:
        return run_test(cfg)

    def guarded() -> int:
        try:
            return run_digest(cfg, args)
        except ApiError as exc:
            logger.error("BidClub API error: %s", exc)
            return 1
        except ValueError as exc:
            logger.error("%s", exc)
            return 1

    if args.dry_run:  # read-only: never contends for the lock
        return guarded()

    with instance_lock(cfg.lock_file) as acquired:
        if not acquired:
            logger.warning("Another run holds %s; exiting without sending.", cfg.lock_file)
            return 0
        return guarded()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        sys.exit(130)
    except Exception as exc:  # pragma: no cover - last-resort guard for cron
        logger.error("Unhandled exception: %s", exc)
        logger.debug("", exc_info=True)
        sys.exit(1)
