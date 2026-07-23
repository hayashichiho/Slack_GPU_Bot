#!/usr/bin/env python3
"""Show the current NVIDIA GPU status in Slack."""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

COMMAND_TIMEOUT_SECONDS = 10
SSH_CONNECT_TIMEOUT_SECONDS = 5
DEFAULT_SSH_KEY = "~/.ssh/gpu_bot_ed25519"
STATUS_MESSAGE_PATTERN = re.compile(r"^\s*状況\s*[!！?？]?\s*$")


@dataclass(frozen=True)
class Server:
    name: str
    ssh_target: str | None = None


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


def run_server_command(command: list[str], server: Server) -> str:
    """Run a command locally, or remotely over non-interactive SSH."""
    if server.ssh_target is None:
        return run_command(command)

    ssh_key = os.path.expanduser(os.environ.get("GPU_SSH_KEY", DEFAULT_SSH_KEY))
    return run_command(
        [
            "ssh",
            "-i",
            ssh_key,
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={SSH_CONNECT_TIMEOUT_SECONDS}",
            server.ssh_target,
            *command,
        ]
    )


def configured_servers() -> list[Server]:
    """Return local server plus optional label=ssh-target entries from .env."""
    local_name = os.environ.get("SERVER_NAME", socket.gethostname())
    servers = [Server(name=local_name)]

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
            servers.append(Server(name=label, ssh_target=ssh_target))
    return servers


def parse_csv(text: str) -> list[list[str]]:
    if not text:
        return []
    return [
        [value.strip() for value in row]
        for row in csv.reader(io.StringIO(text))
        if row
    ]


def linux_user_for_pid(pid: str, server: Server) -> str | None:
    """Resolve a process ID to its Linux user without exposing command lines."""
    try:
        user = run_server_command(["ps", "-o", "user=", "-p", pid], server).strip()
        return user or None
    except (subprocess.SubprocessError, OSError):
        return None


def read_gpu_status(server: Server) -> list[GPU]:
    gpu_rows = parse_csv(
        run_server_command(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            server,
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
                server,
            )
        )
    except subprocess.CalledProcessError:
        process_rows = []

    for row in process_rows:
        if len(row) != 2:
            continue
        gpu_uuid, pid = row
        gpu = by_uuid.get(gpu_uuid)
        user = linux_user_for_pid(pid, server)
        if gpu is not None and user:
            gpu.users.add(user)

    return gpus


def status_icon(gpu: GPU) -> str:
    if gpu.users or gpu.utilization >= 10:
        return "🔴"
    if gpu.memory_used >= 1024:
        return "🟡"
    return "🟢"


def format_gpu(gpu: GPU) -> list[str]:
    users = ", ".join(sorted(gpu.users)) if gpu.users else "なし"
    return [
        f"*GPU {gpu.index}* {status_icon(gpu)}　{gpu.name}",
        f"使用率：{gpu.utilization}%　メモリ：{gpu.memory_used} / {gpu.memory_total} MiB",
        f"利用者：{users}",
        "",
    ]


def build_message(logger: logging.Logger | None = None) -> str:
    lines = [
        f"*🖥 GPUサーバー状況*　{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    for server in configured_servers():
        lines.extend([f"*―― {server.name} ――*", ""])
        try:
            gpus = read_gpu_status(server)
        except (subprocess.SubprocessError, OSError, ValueError):
            if logger:
                logger.exception("Failed to read GPU status from %s", server.name)
            lines.extend(["⚠️ GPU情報を取得できませんでした。", ""])
            continue

        if not gpus:
            lines.extend(["⚠️ GPUが見つかりませんでした。", ""])
            continue

        for gpu in gpus:
            lines.extend(format_gpu(gpu))
    return "\n".join(lines).rstrip()


def require_environment_variable(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"環境変数 {name} が設定されていません。")
    return value


def safe_build_message(logger: logging.Logger) -> str:
    try:
        return build_message(logger)
    except Exception:
        logger.exception("Failed to build GPU status message")
        return "⚠️ GPU状態を取得できませんでした。"


def send_gpu_status(say, logger, *, thread_ts: str | None = None) -> None:
    message = safe_build_message(logger)

    if thread_ts:
        say(text=message, thread_ts=thread_ts)
    else:
        say(text=message)


def build_home_view(status_text: str | None = None) -> dict[str, Any]:
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "GPUサーバー状況",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "GPU状況を確認",
                    },
                    "style": "primary",
                    "action_id": "show_gpu_status",
                }
            ],
        },
    ]

    if status_text:
        blocks.extend(
            [
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": status_text,
                    },
                },
            ]
        )

    return {
        "type": "home",
        "blocks": blocks,
    }


def show_home(event, client):
    if event.get("tab") != "home":
        return

    client.views_publish(
        user_id=event["user"],
        view=build_home_view(),
    )


def handle_home_button(ack, body, client, logger):
    ack()
    client.views_publish(
        user_id=body["user"]["id"],
        view=build_home_view(safe_build_message(logger)),
    )


def handle_status_message(message, say, logger) -> None:
    """Reply in the channel when a human sends only '状況'."""
    if message.get("bot_id") or message.get("subtype"):
        return
    send_gpu_status(say, logger)


def handle_app_mention(event, say, logger) -> None:
    """Keep mention-based requests available as a fallback."""
    send_gpu_status(
        say,
        logger,
        thread_ts=event.get("thread_ts") or event.get("ts"),
    )


def create_app() -> App:
    app = App(token=require_environment_variable("SLACK_BOT_TOKEN"))
    app.event("app_home_opened")(show_home)
    app.action("show_gpu_status")(handle_home_button)
    app.message(STATUS_MESSAGE_PATTERN)(handle_status_message)
    app.event("app_mention")(handle_app_mention)
    return app


if __name__ == "__main__":
    app_token = require_environment_variable("SLACK_APP_TOKEN")
    app = create_app()
    print("GPU Botを起動します。終了するには Ctrl+C を押してください。")
    SocketModeHandler(app, app_token).start()
