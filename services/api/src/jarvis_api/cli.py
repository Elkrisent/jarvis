"""The ``jarvis`` operator CLI. Currently one command: ``rotate-master-key`` (SPEC §11.2).

Keys come only from environment variables, never from arguments. Output carries key IDs and
counts only: no key material and no exception text that could hold a database URL.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping, Sequence
from typing import NoReturn

from jarvis_core.secretbox import (
    MASTER_KEY_ENV,
    AesGcmSecretBox,
    MasterKeyError,
    parse_master_key,
)
from sqlalchemy.exc import ArgumentError, InterfaceError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from jarvis_api.db.migrate import UnsupportedUrlOption, to_async_url
from jarvis_api.db.secrets import PostgresSecretStore, RotationError

NEW_MASTER_KEY_ENV = "JARVIS_NEW_MASTER_KEY"
DATABASE_URL_ENV = "DATABASE_URL"

EXIT_OK = 0
EXIT_RETRYABLE = 1
EXIT_USAGE = 2

_CONNECTION_ERRORS = (OSError, OperationalError, InterfaceError)  # SQLAlchemy wraps driver errors


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse's default prints a multi-line usage block and echoes the offending arguments,
        # which could be a key mistakenly passed on the command line. Print a fixed line instead.
        del message
        _fail("usage error: invalid arguments; keys are read from the environment, not arguments")
        raise SystemExit(EXIT_USAGE)


def _fail(message: str) -> int:
    print(" ".join(message.split()), file=sys.stderr)
    return EXIT_USAGE


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="jarvis", description="JARVIS operator commands.")
    sub = parser.add_subparsers(dest="command", parser_class=_Parser)
    sub.add_parser(
        "rotate-master-key",
        help=f"re-encrypt every stored secret from {MASTER_KEY_ENV} to {NEW_MASTER_KEY_ENV}",
        description=(
            f"Re-encrypt all secrets from the key in {MASTER_KEY_ENV} to the key in "
            f"{NEW_MASTER_KEY_ENV}, using the database in {DATABASE_URL_ENV}."
        ),
    )
    return parser


def _box_from(env: Mapping[str, str], name: str) -> AesGcmSecretBox | str:
    """The secret box for ``name``, or a one-line error naming the variable."""
    value = env.get(name)
    if value is None:
        return f"{name} is not set"
    try:
        return AesGcmSecretBox(parse_master_key(value))
    except MasterKeyError:
        return f"{name} is invalid: expected 32 bytes, base64-encoded"


async def _rotate(async_url: str, old: AesGcmSecretBox, new: AesGcmSecretBox) -> int:
    engine = create_async_engine(async_url)
    try:
        return await PostgresSecretStore(engine).rotate(old, new)
    finally:
        await engine.dispose()


def _rotate_master_key(env: Mapping[str, str]) -> int:
    old = _box_from(env, MASTER_KEY_ENV)
    if isinstance(old, str):
        return _fail(old)
    new = _box_from(env, NEW_MASTER_KEY_ENV)
    if isinstance(new, str):
        return _fail(new)
    if old.key_id == new.key_id:
        return _fail(f"{MASTER_KEY_ENV} and {NEW_MASTER_KEY_ENV} are identical; nothing to rotate")
    url = env.get(DATABASE_URL_ENV)
    if not url:
        return _fail(f"{DATABASE_URL_ENV} is not set")

    try:
        async_url = to_async_url(url)
    except UnsupportedUrlOption as exc:
        return _fail(f"{DATABASE_URL_ENV} is invalid: {exc}")
    except (ArgumentError, ValueError):
        return _fail(f"{DATABASE_URL_ENV} is invalid")

    try:
        count = asyncio.run(_rotate(async_url, old, new))
    except RotationError as exc:
        print(f"rotation refused: {' '.join(str(exc).split())}", file=sys.stderr)
        return EXIT_RETRYABLE
    except _CONNECTION_ERRORS as exc:
        # Never print the exception text: it can include the database URL or password.
        print(f"could not connect to the database ({type(exc).__name__})", file=sys.stderr)
        return EXIT_RETRYABLE
    except SQLAlchemyError as exc:
        # The database answered but refused: typically an unmigrated schema or missing privileges.
        print(
            f"database error ({type(exc).__name__}); is the schema migrated "
            "and may this user lock the secrets table?",
            file=sys.stderr,
        )
        return EXIT_RETRYABLE
    except Exception as exc:
        # Driver errors outside SQLAlchemy (bad port, unknown option): class name only.
        print(f"rotation could not complete ({type(exc).__name__})", file=sys.stderr)
        return EXIT_RETRYABLE

    print(f"re-encrypted {count} secret(s) from {old.key_id} to {new.key_id}")
    return EXIT_OK


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    source = os.environ if env is None else env
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    if args.command == "rotate-master-key":
        return _rotate_master_key(source)
    return _fail("usage: jarvis rotate-master-key")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
