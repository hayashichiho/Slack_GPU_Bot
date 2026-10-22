#!/usr/bin/env python3
"""Show the current NVIDIA GPU status in Slack."""

from __future__ import annotations

import ast
import csv
import io
import json
import logging
import os
import re
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from gpu_notifications import NotificationStore, start_notification_worker

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
    users_complete: bool = True


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
    return [[value.strip() for value in row] for row in csv.reader(io.StringIO(text)) if row]


def linux_user_and_command_for_pid(pid: str, server: Server) -> tuple[str | None, str | None]:
    """Resolve a process ID to its Linux user and command without exposing command lines."""
    try:
        line = run_server_command(
            ["ps", "-ww", "-o", "user:32=,command=", "-p", pid], server
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return None, None

    if not line:
        return None, None
    parts = line.split(None, 1)
    if len(parts) != 2:
        return None, None
    user, command = parts
    return user, command


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

    try:
        exclude_processes_str = os.environ.get("EXCLUDE_PROCESSES", "[]")
        exclude_processes_list = ast.literal_eval(exclude_processes_str)
        if not isinstance(exclude_processes_list, (list, tuple)) or not all(
            isinstance(pair, (list, tuple))
            and len(pair) == 2
            and all(isinstance(item, str) for item in pair)
            for pair in exclude_processes_list
        ):
            raise ValueError("Expected pairs of user and command")
    except (ValueError, SyntaxError):
        print("Warning: EXCLUDE_PROCESSES environment variable is not a valid list. Ignoring.")
        exclude_processes_list = []

    for row in process_rows:
        if len(row) != 2:
            for gpu in gpus:
                gpu.users_complete = False
            continue
        gpu_uuid, pid = row
        gpu = by_uuid.get(gpu_uuid)
        user, command = linux_user_and_command_for_pid(pid, server)
        if gpu is not None and (not user or not command):
            gpu.users_complete = False
        if user and command:
            for exclude_user, exclude_command in exclude_processes_list:
                if user == exclude_user and command == exclude_command:
                    user = None
                    break
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
    if not gpu.users_complete:
        users = (
            f"{users}（一部の利用者を取得できませんでした）"
            if gpu.users
            else "取得できませんでした"
        )
    return [
        f"*GPU {gpu.index}* {status_icon(gpu)}　{gpu.name}",
        f"使用率：{gpu.utilization}%　メモリ：{gpu.memory_used} / {gpu.memory_total} MiB",
        f"利用者：{users}",
        "",
    ]


def build_message(
    logger: logging.Logger | None = None, blocks=None, notification_rows=None
) -> str:
    lines = [
        f"*GPUサーバー状況*　{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    if notification_rows is None:
        notification_rows = []
    if blocks is not None:
        blocks.extend(
            [
                {"type": "section", "text": {"type": "mrkdwn", "text": lines[0]}},
            ]
        )

    for server in configured_servers():
        lines.extend([f"*―― {server.name} ――*", ""])
        if blocks is not None:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": lines[-2]}})
        try:
            gpus = read_gpu_status(server)
        except (subprocess.SubprocessError, OSError, ValueError):
            if logger:
                logger.exception("Failed to read GPU status from %s", server.name)
            lines.extend(["⚠️ GPU情報を取得できませんでした。", ""])
            if blocks is not None:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": lines[-2]}})
            continue

        if not gpus:
            lines.extend(["⚠️ GPUが見つかりませんでした。", ""])
            if blocks is not None:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": lines[-2]}})
            continue

        for gpu in gpus:
            lines.extend(format_gpu(gpu))
            if blocks is not None:
                blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "\n".join(format_gpu(gpu)).rstrip()},
                    }
                )
            value = json.dumps([server.name, server.ssh_target, gpu.uuid, gpu.index])
            notification_rows.append((server, gpu.index, value))
    if blocks is not None and notification_rows:
        blocks.append({"type": "divider"})
        # Button values survive process restarts and avoid fetching GPUs on expansion.
        group = []
        for row in notification_rows:
            candidate = group + [row]
            value = json.dumps([json.loads(item[2]) for item in candidate])
            if group and (len(value) > 1900 or len(candidate) > 5):
                blocks.extend(build_notification_group(group))
                group = []
            group.append(row)
        if group:
            blocks.extend(build_notification_group(group))
    return "\n".join(lines).rstrip()


def build_notification_blocks(notification_rows):
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "通知したいGPUを登録してください。5分おきに確認し、利用者が0人になったら本人へDMで通知します。\n"
                ),
            },
        }
    ]
    previous_server = None
    for server, index, value in notification_rows:
        if server != previous_server:
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*―― {server.name} ――*"}}
            )
            previous_server = server
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": f"GPU {index}：登録"},
                        "action_id": "watch_gpu",
                        "value": value,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": f"GPU {index}：解除"},
                        "action_id": "cancel_gpu_watch",
                        "value": value,
                    },
                ],
            }
        )
    return blocks


def build_notification_group(rows, expanded=False, prefix=None):
    prefix = prefix or f"gpu_notifications_{uuid4().hex}"
    blocks = build_notification_blocks(rows) if expanded else []
    for index, block in enumerate(blocks):
        block["block_id"] = f"{prefix}:{index}"
    blocks.append(
        {
            "type": "actions",
            "block_id": f"{prefix}:toggle",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "🔔 通知設定を閉じる" if expanded else "🔔 通知設定を開く",
                    },
                    "style": "primary",
                    "action_id": "close_gpu_notifications"
                    if expanded
                    else "open_gpu_notifications",
                    "value": json.dumps([json.loads(row[2]) for row in rows]),
                }
            ],
        }
    )
    return blocks


def handle_toggle_notifications(ack, body, client):
    ack()
    action = body["actions"][0]
    rows = []
    for selection in json.loads(action["value"]):
        name, target, uuid, index = selection
        rows.append((Server(name, target), index, json.dumps(selection)))
    prefix = action["block_id"].rsplit(":", 1)[0]
    expanded = action["action_id"] == "open_gpu_notifications"
    replacement = build_notification_group(rows, expanded)
    is_home = body.get("view", {}).get("type") == "home"
    source = body["view"] if is_home else body["message"]
    blocks = []
    replaced = False
    for block in source["blocks"]:
        if block.get("block_id", "").startswith(prefix + ":"):
            if not replaced:
                blocks.extend(replacement)
                replaced = True
        else:
            blocks.append(block)
    if not replaced:
        return
    if is_home:
        client.views_publish(user_id=body["user"]["id"], view={"type": "home", "blocks": blocks})
    else:
        client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text=body["message"].get("text", "GPUサーバー状況"),
            blocks=blocks,
        )


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
    blocks = []
    try:
        message = build_message(logger, blocks)
    except Exception:
        logger.exception("Failed to build GPU status message")
        say(text="⚠️ GPU状態を取得できませんでした。", thread_ts=thread_ts)
        return
    for offset in range(0, len(blocks), 48):
        say(text=message, blocks=blocks[offset : offset + 48], thread_ts=thread_ts)


def build_home_view(status_text: str | None = None, status_blocks=None) -> dict[str, Any]:
    blocks = [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "💻 GPU状況を確認",
                    },
                    "style": "primary",
                    "action_id": "show_gpu_status",
                }
            ],
        },
    ]

    if status_blocks:
        blocks.extend(status_blocks[:98])
        if len(status_blocks) > 98:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "残りのGPUはチャンネルで「状況」と送って確認してください。",
                    },
                }
            )
    elif status_text:
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
    blocks = []
    try:
        message = build_message(logger, blocks)
    except Exception:
        logger.exception("Failed to build GPU status message")
        message, blocks = "⚠️ GPU状態を取得できませんでした。", []
    client.views_publish(
        user_id=body["user"]["id"],
        view=build_home_view(message, blocks),
    )


def handle_gpu_watch(ack, body, client, logger, store):
    ack()
    user_id = body["user"]["id"]
    try:
        action = body["actions"][0]
        name, target, uuid, index = json.loads(action["value"])
        server = next(
            (s for s in configured_servers() if (s.name, s.ssh_target) == (name, target)), None
        )
        if server is None or not all(isinstance(v, str) for v in (uuid, index)):
            raise ValueError("Invalid GPU selection")
        if action["action_id"] == "cancel_gpu_watch":
            store.cancel(server, uuid, user_id)
            text = f"{name} の GPU {index} の通知登録を解除しました。"
        else:
            store.subscribe(server, uuid, index, user_id)
            text = f"🔔 {name} の GPU {index} の通知を登録しました。\n"
    except Exception:
        logger.exception("Failed to update GPU notification subscription")
        text = "⚠️ 通知登録を更新できませんでした。GPU状況を再表示してやり直してください。"
    client.chat_postMessage(channel=user_id, text=text)


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


def create_app(store=None) -> App:
    if store is None:
        store = NotificationStore()
    app = App(token=require_environment_variable("SLACK_BOT_TOKEN"))
    app.event("app_home_opened")(show_home)
    app.action("show_gpu_status")(handle_home_button)
    app.action("open_gpu_notifications")(handle_toggle_notifications)
    app.action("close_gpu_notifications")(handle_toggle_notifications)

    def watch_action(ack, body, client, logger):
        handle_gpu_watch(ack, body, client, logger, store)

    app.action("watch_gpu")(watch_action)
    app.action("cancel_gpu_watch")(watch_action)
    app.message(STATUS_MESSAGE_PATTERN)(handle_status_message)
    app.event("app_mention")(handle_app_mention)
    return app


if __name__ == "__main__":
    app_token = require_environment_variable("SLACK_APP_TOKEN")
    store = NotificationStore()
    app = create_app(store)
    stop_notifications = start_notification_worker(
        store, app.client, configured_servers, read_gpu_status
    )
    print("GPU Botを起動します。終了するには Ctrl+C を押してください。")
    try:
        SocketModeHandler(app, app_token).start()
    finally:
        stop_notifications.set()
