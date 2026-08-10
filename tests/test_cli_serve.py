"""`poedex serve`.

The command itself is four lines; what is worth asserting is that the refusal
survives the trip through argparse. `--host` is `argparse.SUPPRESS`-ed rather than
absent so that the one interesting failure — somebody deciding to "just expose it
on the LAN for a minute" — is reachable and demonstrably refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.main import build_parser
from cli.serve import cmd_serve
from runtime.registry import Registry
from transports.http.server import DEFAULT_PORT


def test_serve_defaults_to_loopback_and_7331():
    args = build_parser().parse_args(["serve"])
    assert args.host == "127.0.0.1"
    assert args.port == DEFAULT_PORT  # 7331: avoids Decky's 1337


async def test_serving_on_a_non_loopback_host_fails_rather_than_binding(
    registry: Registry, capsys: pytest.CaptureFixture[str], tmp_path: Path
):
    code = await cmd_serve(registry, host="0.0.0.0", port=0, static_dir=str(tmp_path))
    assert code == 2
    assert "refusing to bind" in capsys.readouterr().err


async def test_a_missing_spa_build_is_reported_before_the_server_starts(
    registry: Registry, capsys: pytest.CaptureFixture[str], tmp_path: Path
):
    """The API is still usable; a blank page with no explanation is not."""
    await cmd_serve(registry, host="0.0.0.0", port=0, static_dir=str(tmp_path / "nope"))
    err = capsys.readouterr().err
    assert "pnpm build" in err
