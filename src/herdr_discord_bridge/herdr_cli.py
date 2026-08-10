from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

from .config import HerdrCliConfig


class HerdrCliError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str


class HerdrCli:
    def __init__(self, config: HerdrCliConfig):
        self.config = config

    def agent_list(self) -> Any:
        return self._json(["agent", "list"])

    def pane_list(self) -> Any:
        return self._json(["pane", "list"])

    def workspace_list(self) -> Any:
        return self._json(["workspace", "list"])

    def agent_read(self, target: str, *, lines: int, fmt: str | None = None, source: str | None = None) -> str:
        args = ["agent", "read", target, "--lines", str(lines)]
        if source:
            args += ["--source", source]
        if fmt:
            args += ["--format", fmt]
        return _read_text(self._run(args).stdout)

    def pane_read(self, target: str, *, lines: int, source: str | None = None, fmt: str | None = None) -> str:
        source = source or self.config.default_source
        args = ["pane", "read", target, "--source", source, "--lines", str(lines)]
        if fmt:
            args += ["--format", fmt]
        return _read_text(self._run(args).stdout)

    def agent_prompt(self, target: str, message: str) -> None:
        self._run(["agent", "prompt", target, message])

    def pane_send_text(self, target: str, message: str) -> None:
        self._run(["pane", "send-text", target, message])

    def pane_send_keys(self, target: str, *keys: str) -> None:
        for key in keys:
            self._run(["pane", "send-keys", target, key])

    def pane_run(self, target: str, message: str) -> None:
        self._run(["pane", "run", target, message])

    def tab_create(self, *, workspace: str, cwd: str | None = None) -> Any:
        args = ["tab", "create", "--workspace", workspace, "--no-focus"]
        if cwd:
            args += ["--cwd", cwd]
        return self._json(args)

    def pane_split(
        self,
        pane_id: str,
        *,
        direction: str = "right",
        cwd: str | None = None,
    ) -> Any:
        args = ["pane", "split", pane_id, "--direction", direction, "--no-focus"]
        if cwd:
            args += ["--cwd", cwd]
        return self._json(args)

    def workspace_create(self, *, cwd: str | None = None) -> Any:
        args = ["workspace", "create", "--no-focus"]
        if cwd:
            args += ["--cwd", cwd]
        return self._json(args)

    def agent_start(
        self,
        name: str,
        *,
        kind: str,
        pane_id: str,
        argv: list[str] | None = None,
    ) -> Any:
        args = ["agent", "start", name, "--kind", kind, "--pane", pane_id]
        if argv:
            args += ["--", *argv]
        return self._json(args)

    def pane_close(self, pane_id: str) -> None:
        self._run(["pane", "close", pane_id])

    def _json(self, args: list[str]) -> Any:
        raw = self._run(args).stdout
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HerdrCliError(f"Herdr did not return JSON for {' '.join(args)}") from exc

    def _run(self, args: list[str]) -> CommandResult:
        command = [self.config.cli_path, *args]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.config.command_timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise HerdrCliError(f"Herdr CLI not found: {self.config.cli_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise HerdrCliError(f"Herdr command timed out: {' '.join(command)}") from exc

        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise HerdrCliError(f"Herdr command failed: {' '.join(command)}\n{detail}")
        return CommandResult(stdout=completed.stdout, stderr=completed.stderr)


def _read_text(stdout: str) -> str:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    if not isinstance(payload, dict):
        return stdout

    result = payload.get("result")
    if isinstance(result, dict):
        read = result.get("read")
        if isinstance(read, dict) and isinstance(read.get("text"), str):
            return read["text"]
        if isinstance(result.get("text"), str):
            return result["text"]
    read = payload.get("read")
    if isinstance(read, dict) and isinstance(read.get("text"), str):
        return read["text"]
    if isinstance(payload.get("text"), str):
        return payload["text"]
    return stdout
