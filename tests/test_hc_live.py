from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from herdrelay.config import AppConfig, HcRuntimeConfig, RuntimeConfig
from herdrelay.hc_client import HcClient
from herdrelay.hc_events import HcEventStream, HcTerminalState

ROOT = Path(__file__).parents[1]
RUN_LIVE = os.environ.get("HERDRELAY_RUN_HC_LIVE_TEST") == "1"

pytestmark = pytest.mark.skipif(
    not RUN_LIVE,
    reason="set HERDRELAY_RUN_HC_LIVE_TEST=1 for the isolated Core contract test",
)


@pytest.mark.asyncio
async def test_isolated_core_list_read_send_interrupt_and_events_contract():
    hc = shutil.which("hc")
    if hc is None:
        pytest.skip("hc is not installed")

    scratch = Path(tempfile.mkdtemp(prefix="herdrelay-hc-live.", dir="/tmp"))
    core_process: subprocess.Popen | None = None
    client: HcClient | None = None
    target: str | None = None
    owned_targets: list[str] = []
    log_handle = None
    try:
        directories = {
            name: scratch / name
            for name in ("home", "state", "config", "runtime", "cache", "data")
        }
        for directory in directories.values():
            directory.mkdir()
        socket_path = directories["runtime"] / "core.sock"
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(directories["home"]),
                "XDG_STATE_HOME": str(directories["state"]),
                "XDG_CONFIG_HOME": str(directories["config"]),
                "XDG_RUNTIME_DIR": str(directories["runtime"]),
                "XDG_CACHE_HOME": str(directories["cache"]),
                "XDG_DATA_HOME": str(directories["data"]),
                "HC_SESSION": f"herdrelay-live-{os.getpid()}",
                "HC_SOCKET": str(socket_path),
            }
        )
        for name in ("HC_RUNTIME", "HC_SCOPE", "HC_TERMINAL_ID", "HC_TERMINAL_SOCKET"):
            environment.pop(name, None)

        log_handle = (scratch / "core.log").open("wb")
        core_process = subprocess.Popen(
            [hc, "serve"],
            env=environment,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _wait_for_socket(socket_path, core_process)

        fixture = ROOT / "tests/fixtures/hc/echo-agent.py"
        target = _run_fixture(hc, environment, fixture, tab="echo")
        interrupt_target = _run_fixture(
            hc, environment, fixture, tab="interrupt", args=("--working",)
        )
        owned_targets.extend((target, interrupt_target))

        client = HcClient(
            AppConfig(
                discord_token="unused",
                runtime=RuntimeConfig(
                    provider="herdr-core",
                    core=HcRuntimeConfig(
                        socket_path=str(socket_path),
                        allowed_terminal_addresses=frozenset(owned_targets),
                    ),
                ),
            )
        )
        _wait_for_idle(client, target)
        subscription = await HcEventStream(client.config.runtime.core).connect(
            terminal=target
        )
        state = HcTerminalState(subscription.snapshot)
        try:
            client.send(target, "CORE_SMOKE_42")
            observed_states = []
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and "done" not in observed_states:
                notification = await subscription.next_notification(timeout=1)
                observed_states.extend(
                    transition.current_state
                    for transition in state.apply(notification)
                )
            assert "working" in observed_states
            assert "done" in observed_states
        finally:
            await subscription.close()
        _wait_for_echo(client, target, "ECHO:CORE_SMOKE_42")
        _wait_for_status(client, interrupt_target, "working")
        interrupt_receipt = client.interrupt_with_receipt(interrupt_target)
        assert interrupt_receipt.result.get("outcome") in {
            "sent",
            "stopped",
            "still_working",
        }
        assert client.core_id
    finally:
        if client is not None:
            for owned_target in owned_targets:
                try:
                    client.rpc.call(
                        "terminal.kill", {"terminal": owned_target, "caller": None}
                    )
                except Exception:
                    pass
            try:
                client.rpc.call("server.stop", {})
            except Exception:
                pass
        if core_process is not None and core_process.poll() is None:
            try:
                os.killpg(core_process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                core_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(core_process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                core_process.wait(timeout=2)
        _kill_processes_referencing(scratch)
        if log_handle is not None:
            log_handle.close()
        shutil.rmtree(scratch, ignore_errors=True)


def _run_fixture(
    hc: str,
    environment: dict[str, str],
    fixture: Path,
    *,
    tab: str,
    args: tuple[str, ...] = (),
) -> str:
    completed = subprocess.run(
        [
            hc,
            "run",
            "--json",
            "--bg",
            "--no-parent",
            "--label",
            "herdr.space=smoke",
            "--label",
            f"herdr.tab={tab}",
            "--",
            sys.executable,
            str(fixture),
            *args,
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    result = json.loads(completed.stdout)
    target = result.get("address") or result.get("terminal") or result.get("target")
    assert isinstance(target, str) and target
    return target


def _wait_for_socket(path: Path, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.is_socket():
            return
        if process.poll() is not None:
            raise AssertionError(f"isolated Core exited with status {process.returncode}")
        time.sleep(0.05)
    raise AssertionError("isolated Core socket did not appear")


def _wait_for_idle(client: HcClient, target: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        targets = client.list_targets()
        if targets and targets[0].target == target and targets[0].status == "idle":
            return
        time.sleep(0.05)
    raise AssertionError("synthetic Core agent did not become idle")


def _wait_for_status(client: HcClient, target: str, expected: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        found = {item.target: item for item in client.list_targets()}
        if target in found and found[target].status == expected:
            return
        time.sleep(0.05)
    raise AssertionError(f"synthetic Core agent did not become {expected}")


def _wait_for_echo(client: HcClient, target: str, expected: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if expected in client.read(target, lines=20):
            return
        time.sleep(0.05)
    raise AssertionError("synthetic Core agent did not echo the submitted prompt")


def _kill_processes_referencing(scratch: Path) -> None:
    listing = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    owned_pids = []
    marker = str(scratch)
    for line in listing.splitlines():
        pid_text, separator, command = line.strip().partition(" ")
        if separator and marker in command:
            owned_pids.append(int(pid_text))
    for pid in owned_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 1
    while owned_pids and time.monotonic() < deadline:
        owned_pids = [pid for pid in owned_pids if _process_exists(pid)]
        if owned_pids:
            time.sleep(0.05)
    for pid in owned_pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
