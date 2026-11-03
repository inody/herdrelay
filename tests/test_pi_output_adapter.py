"""Execute the actual Pi adapter with isolated event contexts and inboxes."""

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]


def test_pi_adapter_event_contract():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Pi adapter tests require Node >=22.6")
    version = subprocess.run(
        [node, "--version"], capture_output=True, text=True, check=True, timeout=5,
    ).stdout.strip().removeprefix("v")
    if tuple(int(part) for part in version.split(".")[:2]) < (22, 6):
        pytest.skip("Pi adapter tests require Node >=22.6 for TypeScript stripping")
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test", "tests/pi_output_adapter.test.mjs"],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
