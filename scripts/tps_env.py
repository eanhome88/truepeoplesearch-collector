#!/usr/bin/env python3
"""Minimal, non-executing runtime configuration loader.

The project historically used small, ad-hoc ``.env`` readers in a few entry
points.  This module centralizes the safe subset needed by runtime launchers:
it never evaluates shell syntax, never overwrites an actual process
environment value, and never prints configuration values.

For the customer dashboard path, only local database/Redis settings and the
fixed dashboard port can come from a file.  Release mode, its launch token,
update endpoints, proxy settings, and webhook destinations must be supplied
by the trusted launcher process instead.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path
from typing import MutableMapping, Optional


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LAUNCH_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
_MAX_ENV_FILE_BYTES = 1024 * 1024

# These are the only file-provided values a customer dashboard may consume.
# In particular, neither the restricted release mode nor any outbound endpoint
# can be selected by an editable local .env file.
CUSTOMER_ENV_KEYS = frozenset({
    "TPS_DB_HOST",
    "TPS_DB_PORT",
    "TPS_DB_USER",
    "TPS_DB_PASSWORD",
    "TPS_DB_NAME",
    "TPS_REDIS_HOST",
    "TPS_REDIS_PORT",
    "TPS_DASHBOARD_PORT",
})

# A release mode and its proof-of-launch are process capabilities.  Never
# source them from a file, including in a standard local development launch.
_FILE_DISABLED_KEYS = frozenset({
    "TPS_RELEASE_MODE",
    "TPS_RELEASE_LAUNCH_TOKEN",
})


def customer_release_mode(environ: Optional[MutableMapping[str, str]] = None) -> bool:
    """Return whether the already-existing process environment selects customer mode."""
    environment = os.environ if environ is None else environ
    value = environment.get("TPS_RELEASE_MODE", "")
    return isinstance(value, str) and value.strip().casefold() == "customer"


def release_launch_token(environ: Optional[MutableMapping[str, str]] = None) -> Optional[str]:
    """Return an exact, safe customer launch token, or ``None`` if unavailable."""
    environment = os.environ if environ is None else environ
    value = environment.get("TPS_RELEASE_LAUNCH_TOKEN")
    if not isinstance(value, str) or not _LAUNCH_TOKEN_RE.fullmatch(value):
        return None
    return value


def dashboard_port(environ: Optional[MutableMapping[str, str]] = None, default: int = 5001) -> int:
    """Read a strict dashboard port without exposing an invalid value in errors."""
    environment = os.environ if environ is None else environ
    value = environment.get("TPS_DASHBOARD_PORT", default)
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValueError("TPS_DASHBOARD_PORT must be an integer from 1 to 65535") from None
    if not 1 <= port <= 65535:
        raise ValueError("TPS_DASHBOARD_PORT must be an integer from 1 to 65535")
    return port


def _file_pairs(path: Path):
    """Yield basic KEY=VALUE entries from a regular, bounded UTF-8 file only."""
    try:
        info = path.lstat()
    except OSError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_ENV_FILE_BYTES:
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    for index, line in enumerate(text.splitlines()):
        if index == 0:
            line = line.lstrip("\ufeff")
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not _ENV_NAME_RE.fullmatch(name):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            continue
        yield name, value


def load_project_env(
    root: Path,
    environ: Optional[MutableMapping[str, str]] = None,
    *,
    customer_safe: Optional[bool] = None,
) -> None:
    """Load root then deploy .env values without overriding process environment.

    Parsing intentionally has no shell expansion, interpolation, command
    execution, logging, or exception output.  A process-owned value wins over
    both files; root ``.env`` wins over ``deploy/.env`` for values absent from
    the process environment.
    """
    environment = os.environ if environ is None else environ
    root = Path(root)
    if customer_safe is None:
        customer_safe = customer_release_mode(environment)
    allowed = CUSTOMER_ENV_KEYS if customer_safe else None

    for candidate in (root / ".env", root / "deploy" / ".env"):
        for name, value in _file_pairs(candidate) or ():
            if name in _FILE_DISABLED_KEYS:
                continue
            if allowed is not None and name not in allowed:
                continue
            if name not in environment:
                environment[name] = value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TPS local runtime configuration helper")
    parser.add_argument(
        "--dashboard-port",
        action="store_true",
        help="print the validated customer-dashboard port only",
    )
    return parser


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.dashboard_port:
        return 0
    # This CLI is for the customer launcher, so keep its file input restricted
    # even before the launcher has set TPS_RELEASE_MODE for the child process.
    load_project_env(Path(__file__).resolve().parent.parent, customer_safe=True)
    try:
        print(dashboard_port())
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
