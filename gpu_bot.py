#!/usr/bin/env python3
"""Reply to Slack mentions with the current NVIDIA GPU status."""

from __future__ import annotations

import csv
import io
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler


COMMAND_TIMEOUT_SECONDS = 10


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


def parse_csv(text: str) -> list[list[str]]:
    if not text:
        return []
    return [
        [value.strip() for value in row]
        for row in csv.reader(io.StringIO(text))
        if row
    ]


def linux_user_for_pid(pid: str) -> str | None:
    """Resolve a process ID to its Linux user without exposing command lines."""
    try:
        user = run_command(["ps", "-o", "user=", "-p", pid]).strip()
        return user or None
    except (subprocess.SubprocessError, OSError):
        return None


def read_gpu_status() -> list[GPU]:
    gpu_rows = parse_csv(
        run_command(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ]
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
            run_command(
                [
                    "nvidia-smi",
                    "--query-compute-apps=gpu_uuid,pid",
                    "--format=csv,noheader,nounits",
                ]
            )
        )
    except subprocess.CalledProcessError:
        process_rows = []

    for row in process_rows:
        if len(row) != 2:
            continue
        gpu_uuid, pid = row
        gpu = by_uuid.get(gpu_uuid)
        user = linux_user_for_pid(pid)
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
    gpus = read_gpu_status()
    if not gpus:
        return "⚠️ GPU情報を取得できませんでした。"

    lines = [
        f"*🖥 GPUサーバー状況*　{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
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
