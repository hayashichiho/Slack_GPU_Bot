"""Persistent, one-shot GPU availability subscriptions."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

CHECK_INTERVAL_SECONDS = 300
DEFAULT_DATABASE = Path(__file__).with_name("gpu-notifications.sqlite3")


class NotificationStore:
    def __init__(self, path=DEFAULT_DATABASE):
        self.path = path
        with self.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    server_name TEXT NOT NULL,
                    ssh_target TEXT NOT NULL,
                    gpu_uuid TEXT NOT NULL,
                    gpu_index TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    PRIMARY KEY (server_name, ssh_target, gpu_uuid, user_id)
                )
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def subscribe(self, server, uuid, index, user_id):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO subscriptions VALUES (?, ?, ?, ?, ?)",
                (server.name, server.ssh_target or "", uuid, index, user_id),
            )

    def cancel(self, server, uuid, user_id):
        with self.connect() as db:
            db.execute(
                "DELETE FROM subscriptions WHERE server_name=? AND ssh_target=? "
                "AND gpu_uuid=? AND user_id=?",
                (server.name, server.ssh_target or "", uuid, user_id),
            )

    def pending(self):
        with self.connect() as db:
            return db.execute("SELECT * FROM subscriptions").fetchall()


def check_notifications(store, client, servers, read_status, logger):
    """Query each subscribed server once; failed/unknown readings never notify."""
    pending = store.pending()
    for server in servers:
        subscriptions = [
            row
            for row in pending
            if (row["server_name"], row["ssh_target"]) == (server.name, server.ssh_target or "")
        ]
        if not subscriptions:
            continue
        try:
            gpus = {gpu.uuid: gpu for gpu in read_status(server)}
        except Exception:
            logger.exception("Failed to check GPU notifications for %s", server.name)
            continue
        for row in subscriptions:
            gpu = gpus.get(row["gpu_uuid"])
            if gpu is None or not gpu.users_complete or gpu.users:
                continue
            try:
                # Keep cancellation and successful delivery atomic within this process.
                with store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    exists = db.execute(
                        "SELECT 1 FROM subscriptions WHERE server_name=? AND ssh_target=? "
                        "AND gpu_uuid=? AND user_id=?",
                        (server.name, server.ssh_target or "", gpu.uuid, row["user_id"]),
                    ).fetchone()
                    if not exists:
                        continue
                    client.chat_postMessage(
                        channel=row["user_id"],
                        text=(
                            f"🔔 {server.name} の GPU {gpu.index}（{gpu.name}）の"
                            "利用者が0人になりました。\n"
                        ),
                    )
                    db.execute(
                        "DELETE FROM subscriptions WHERE server_name=? AND ssh_target=? "
                        "AND gpu_uuid=? AND user_id=?",
                        (server.name, server.ssh_target or "", gpu.uuid, row["user_id"]),
                    )
            except Exception:
                logger.exception("GPU notification failed; will retry next interval")


def start_notification_worker(store, client, servers, read_status):
    stop = threading.Event()
    logger = logging.getLogger(__name__)

    def run():
        while not stop.wait(CHECK_INTERVAL_SECONDS):
            try:
                check_notifications(store, client, servers(), read_status, logger)
            except Exception:
                logger.exception("GPU notification check failed")

    threading.Thread(target=run, name="gpu-notifications", daemon=True).start()
    return stop
