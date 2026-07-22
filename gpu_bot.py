#!/usr/bin/env python3
"""Reply to Slack mentions with the current NVIDIA GPU status."""

from __future__ import annotations

import csv
import io
import os
import re
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler


COMMAND_TIMEOUT_SECONDS = 10
SSH_CONNECT_TIMEOUT_SECONDS = 5


@dataclass
class GPU:
    index: str
    uuid: str
    name: str
    utilization: int
    memory_used: int
    memory_total: int
    users: set[str] = field(default_factory=set)


def run_command(command: list[str]) -> str:
    """Run a read-only command and return stdout."""
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    return result.stdout.strip()


def run_server_command(command: list[str], ssh_target: str | None = None) -> str:
    """Run a command locally, or remotely over non-interactive SSH."""
    if ssh_target is None:
        return run_command(command)

    ssh_key = os.path.expanduser(
        os.environ.get("GPU_SSH_KEY", "~/.ssh/gpu_bot_ed25519")
    )
    return run_command(
        [
            "ssh",
            "-i",
            ssh_key,
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={SSH_CONNECT_TIMEOUT_SECONDS}",
            ssh_target,
            *command,
        ]
    )


def configured_servers() -> list[tuple[str, str | None]]:
    """Return local server plus optional label=ssh-target entries from .env."""
    local_name = os.environ.get("SERVER_NAME", socket.gethostname())
    servers: list[tuple[str, str | None]] = [(local_name, None)]

    raw_servers = os.environ.get("REMOTE_GPU_SERVERS", "")
    for entry in raw_servers.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" in entry:
            label, ssh_target = entry.split("=", 1)
        else:
            label = ssh_target = entry
        label = label.strip()
        ssh_target = ssh_target.strip()
        if label and ssh_target:
            servers.append((label, ssh_target))
    return servers


def parse_csv(text: str) -> list[list[str]]:
    if not text:
        return []
    return [
        [value.strip() for value in row]
        for row in csv.reader(io.StringIO(text))
        if row
    ]


def linux_user_for_pid(pid: str, ssh_target: str | None = None) -> str | None:
    """Resolve a process ID to its Linux user without exposing command lines."""
    try:
        user = run_server_command(
            ["ps", "-o", "user=", "-p", pid],
            ssh_target,
        ).strip()
        return user or None
    except (subprocess.SubprocessError, OSError):
        return None


def read_gpu_status(ssh_target: str | None = None) -> list[GPU]:
    gpu_rows = parse_csv(
        run_server_command(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            ssh_target,
        )
    )

    gpus: list[GPU] = []
    by_uuid: dict[str, GPU] = {}
    for row in gpu_rows:
        if len(row) != 6:
            continue
        gpu = GPU(
            index=row[0],
            uuid=row[1],
            name=row[2],
            utilization=int(row[3]),
            memory_used=int(row[4]),
            memory_total=int(row[5]),
        )
        gpus.append(gpu)
        by_uuid[gpu.uuid] = gpu

    try:
        process_rows = parse_csv(
            run_server_command(
                [
                    "nvidia-smi",
                    "--query-compute-apps=gpu_uuid,pid",
                    "--format=csv,noheader,nounits",
                ],
                ssh_target,
            )
        )
    except subprocess.CalledProcessError:
        process_rows = []

    for row in process_rows:
        if len(row) != 2:
            continue
        gpu_uuid, pid = row
        gpu = by_uuid.get(gpu_uuid)
        user = linux_user_for_pid(pid, ssh_target)
        if gpu is not None and user:
            gpu.users.add(user)

    return gpus


def status_icon(gpu: GPU) -> str:
    if gpu.users or gpu.utilization >= 10:
        return "🔴"
    if gpu.memory_used >= 1024:
        return "🟡"
    return "🟢"


def build_message() -> str:
    lines = [
        f"*🖥 GPUサーバー状況*　{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    for server_name, ssh_target in configured_servers():
        lines.extend([f"*―― {server_name} ――*", ""])
        try:
            gpus = read_gpu_status(ssh_target)
        except (subprocess.SubprocessError, OSError, ValueError):
            lines.extend(["⚠️ GPU情報を取得できませんでした。", ""])
            continue

        if not gpus:
            lines.extend(["⚠️ GPUが見つかりませんでした。", ""])
            continue

        for gpu in gpus:
            users = ", ".join(sorted(gpu.users)) if gpu.users else "なし"
            lines.extend(
                [
                    f"*GPU {gpu.index}* {status_icon(gpu)}　{gpu.name}",
                    f"使用率：{gpu.utilization}%　メモリ：{gpu.memory_used} / {gpu.memory_total} MiB",
                    f"利用者：{users}",
                    "",
                ]
            )
    return "\n".join(lines).rstrip()


def require_environment_variable(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"環境変数 {name} が設定されていません。")
    return value


def send_gpu_status(say, logger, *, thread_ts: str | None = None) -> None:
    try:
        message = build_message()
    except FileNotFoundError:
        message = "⚠️ このサーバーで `nvidia-smi` が見つかりません。"
    except subprocess.TimeoutExpired:
        message = "⚠️ GPU状態の取得がタイムアウトしました。"
    except (subprocess.CalledProcessError, ValueError) as error:
        logger.exception("GPU status command failed")
        message = f"⚠️ GPU状態を取得できませんでした（{type(error).__name__}）。"

    if thread_ts:
        say(text=message, thread_ts=thread_ts)
    else:
        say(text=message)


app = App(token=require_environment_variable("SLACK_BOT_TOKEN"))


@app.message(re.compile(r"^\s*状況\s*[!！?？]?\s*$"))
def handle_status_message(message, say, logger) -> None:
    """Reply in the channel when a human sends only '状況'."""
    if message.get("bot_id") or message.get("subtype"):
        return
    send_gpu_status(say, logger)


@app.event("app_mention")
def handle_app_mention(event, say, logger) -> None:
    """Keep mention-based requests available as a fallback."""
    send_gpu_status(
        say,
        logger,
        thread_ts=event.get("thread_ts") or event.get("ts"),
    )


if __name__ == "__main__":
    app_token = require_environment_variable("SLACK_APP_TOKEN")
    print("GPU Botを起動します。終了するには Ctrl+C を押してください。")
    SocketModeHandler(app, app_token).start()
