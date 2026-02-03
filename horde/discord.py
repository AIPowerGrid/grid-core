# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import threading
from datetime import datetime
from queue import Queue

import requests
from loguru import logger

# Message queue for async sending
_message_queue = Queue()
_worker_thread = None


def _send_worker():
    """Background worker to send Discord messages without blocking."""
    while True:
        try:
            webhook_url, embed = _message_queue.get()
            if webhook_url and embed:
                data = {"embeds": [embed]}
                req = requests.post(webhook_url, json=data, timeout=5)
                if not req.ok:
                    # Don't log to avoid recursion
                    pass
            _message_queue.task_done()
        except Exception:
            pass


def _ensure_worker():
    """Ensure the background sender thread is running."""
    global _worker_thread
    if _worker_thread is None or not _worker_thread.is_alive():
        _worker_thread = threading.Thread(target=_send_worker, daemon=True)
        _worker_thread.start()


def send_webhook(webhook_url: str, message: str):
    """Legacy function for simple text messages."""
    data = {"content": message}
    try:
        req = requests.post(webhook_url, json=data, timeout=2)
        if not req.ok:
            logger.warning(f"Something went wrong when sending discord webhook: {req.status_code} - {req.text}")
            return
    except Exception as err:
        logger.warning(f"Exception when sending discord webhook: {err}")
        return


def send_embed(webhook_url: str, embed: dict):
    """Queue an embed message for async sending."""
    if not webhook_url:
        return
    _ensure_worker()
    _message_queue.put((webhook_url, embed))


def send_pause_notification(message: str):
    webhook_url = os.getenv("DISCORD_PAUSED_NOTICE_WEBHOOK")
    if not webhook_url:
        logger.warning("Cannot send Pause notification. No DISCORD_PAUSED_NOTICE_WEBHOOK set")
        return
    send_webhook(webhook_url, message)


def send_problem_user_notification(message: str):
    webhook_url = os.getenv("DISCORD_PROBLEM_USER_WEBHOOK")
    if not webhook_url:
        logger.warning("Cannot send Pause notification. No DISCORD_PROBLEM_USER_WEBHOOK set")
        return
    send_webhook(webhook_url, message)


# ============================================================================
# Core Log Notifications (Worker Events, Errors, etc.)
# ============================================================================


def _get_core_webhook():
    """Get the core log webhook URL."""
    return os.getenv("DISCORD_CORE_LOG_WEBHOOK")


def notify_worker_online(worker_name: str, worker_id: str, models: list = None, user_name: str = None):
    """Notify when a worker comes online (from stale state)."""
    webhook_url = _get_core_webhook()
    if not webhook_url:
        return

    models_str = ", ".join(models[:5]) if models else "None"
    if models and len(models) > 5:
        models_str += f" (+{len(models) - 5} more)"

    embed = {
        "title": "🟢 Worker Online",
        "color": 0x00FF00,  # Green
        "fields": [
            {"name": "Worker", "value": worker_name, "inline": True},
            {"name": "Owner", "value": user_name or "Unknown", "inline": True},
            {"name": "Models", "value": models_str or "None", "inline": False},
        ],
        "footer": {"text": f"ID: {worker_id}"},
        "timestamp": datetime.utcnow().isoformat(),
    }
    send_embed(webhook_url, embed)


def notify_worker_offline(worker_name: str, worker_id: str, user_name: str = None):
    """Notify when a worker goes offline (becomes stale)."""
    webhook_url = _get_core_webhook()
    if not webhook_url:
        return

    embed = {
        "title": "🔴 Worker Offline",
        "color": 0xFF0000,  # Red
        "fields": [
            {"name": "Worker", "value": worker_name, "inline": True},
            {"name": "Owner", "value": user_name or "Unknown", "inline": True},
        ],
        "footer": {"text": f"ID: {worker_id}"},
        "timestamp": datetime.utcnow().isoformat(),
    }
    send_embed(webhook_url, embed)


def notify_worker_created(worker_name: str, worker_id: str, user_name: str = None, models: list = None):
    """Notify when a new worker is created."""
    webhook_url = _get_core_webhook()
    if not webhook_url:
        return

    models_str = ", ".join(models[:5]) if models else "None"
    if models and len(models) > 5:
        models_str += f" (+{len(models) - 5} more)"

    embed = {
        "title": "✨ New Worker Created",
        "color": 0x00BFFF,  # Deep Sky Blue
        "fields": [
            {"name": "Worker", "value": worker_name, "inline": True},
            {"name": "Owner", "value": user_name or "Unknown", "inline": True},
            {"name": "Models", "value": models_str or "None", "inline": False},
        ],
        "footer": {"text": f"ID: {worker_id}"},
        "timestamp": datetime.utcnow().isoformat(),
    }
    send_embed(webhook_url, embed)


def notify_job_aborted(job_id: str, worker_name: str = None, reason: str = None):
    """Notify when a job is aborted."""
    webhook_url = _get_core_webhook()
    if not webhook_url:
        return

    embed = {
        "title": "⚠️ Job Aborted",
        "color": 0xFFA500,  # Orange
        "fields": [
            {"name": "Job ID", "value": job_id, "inline": False},
            {"name": "Worker", "value": worker_name or "Unknown", "inline": False},
            {"name": "Reason", "value": (reason or "Unknown")[:1000], "inline": False},
        ],
        "timestamp": datetime.utcnow().isoformat(),
    }
    send_embed(webhook_url, embed)


def notify_error(error_type: str, message: str, context: dict = None):
    """Notify on errors."""
    webhook_url = _get_core_webhook()
    if not webhook_url:
        return

    fields = [
        {"name": "Type", "value": error_type, "inline": True},
        {"name": "Message", "value": message[:1000], "inline": False},
    ]

    if context:
        for key, value in list(context.items())[:5]:
            fields.append({"name": key, "value": str(value)[:200], "inline": True})

    embed = {
        "title": "❌ Error",
        "color": 0xFF0000,  # Red
        "fields": fields,
        "timestamp": datetime.utcnow().isoformat(),
    }
    send_embed(webhook_url, embed)


def notify_blockchain_event(event_type: str, details: dict = None):
    """Notify on blockchain-related events."""
    webhook_url = _get_core_webhook()
    if not webhook_url:
        return

    fields = [{"name": "Event", "value": event_type, "inline": False}]

    if details:
        for key, value in list(details.items())[:6]:
            fields.append({"name": key, "value": str(value)[:200], "inline": True})

    embed = {
        "title": "⛓️ Blockchain Event",
        "color": 0x9B59B6,  # Purple
        "fields": fields,
        "timestamp": datetime.utcnow().isoformat(),
    }
    send_embed(webhook_url, embed)


def _get_jobs_webhook():
    """Get the jobs webhook URL (for job activity logging)."""
    return os.getenv("DISCORD_JOBS_WEBHOOK")


def notify_job_popped(job_id: str, model: str, worker_name: str, prompt: str = None):
    """Notify when a job is popped by a worker."""
    webhook_url = _get_jobs_webhook()
    if not webhook_url:
        return

    prompt_preview = (prompt[:200] + "...") if prompt and len(prompt) > 200 else (prompt or "N/A")

    embed = {
        "title": "📤 Job Popped",
        "color": 0x3498DB,  # Blue
        "fields": [
            {"name": "Job ID", "value": job_id, "inline": False},
            {"name": "Model", "value": model or "Unknown", "inline": False},
            {"name": "Worker", "value": worker_name, "inline": False},
            {"name": "Prompt", "value": prompt_preview, "inline": False},
        ],
        "timestamp": datetime.utcnow().isoformat(),
    }
    send_embed(webhook_url, embed)


def notify_job_submitted(job_id: str, model: str, worker_name: str, kudos: float = None):
    """Notify when a job is submitted/completed by a worker."""
    webhook_url = _get_jobs_webhook()
    if not webhook_url:
        return

    embed = {
        "title": "✅ Job Complete",
        "color": 0x00FF00,  # Green
        "fields": [
            {"name": "Job ID", "value": job_id, "inline": False},
            {"name": "Model", "value": model or "Unknown", "inline": False},
            {"name": "Worker", "value": worker_name, "inline": False},
        ],
        "timestamp": datetime.utcnow().isoformat(),
    }
    if kudos is not None:
        embed["fields"].append({"name": "Kudos", "value": f"{kudos:.1f}", "inline": False})

    send_embed(webhook_url, embed)


def notify_generation_complete(job_id: str, model: str, worker_name: str = None, time_seconds: float = None):
    """Notify when a generation completes (optional, can be noisy)."""
    webhook_url = os.getenv("DISCORD_GENERATION_WEBHOOK")  # Separate webhook for generations
    if not webhook_url:
        return

    embed = {
        "title": "✅ Generation Complete",
        "color": 0x00FF00,  # Green
        "fields": [
            {"name": "Job ID", "value": job_id, "inline": True},
            {"name": "Model", "value": model, "inline": True},
            {"name": "Worker", "value": worker_name or "Unknown", "inline": True},
        ],
        "timestamp": datetime.utcnow().isoformat(),
    }
    if time_seconds:
        embed["fields"].append({"name": "Time", "value": f"{time_seconds:.1f}s", "inline": True})

    send_embed(webhook_url, embed)


# ============================================================================
# Queue Monitoring Alerts (HIGH PRIORITY)
# ============================================================================


def notify_stuck_jobs_alert(stuck_count: int, oldest_age_minutes: float, details: list = None):
    """
    CRITICAL ALERT: Stuck processing_gens detected.
    These block workers from receiving new jobs.
    """
    webhook_url = _get_core_webhook()
    if not webhook_url:
        return

    # Build details string
    details_str = ""
    if details:
        for d in details[:5]:
            details_str += f"• {d['model']}: {d['count']} jobs ({d['oldest_minutes']:.0f}m old)\n"
        if len(details) > 5:
            details_str += f"• ... and {len(details) - 5} more models"

    embed = {
        "title": "🚨 STUCK JOBS DETECTED",
        "color": 0xFF0000,  # Red - critical
        "description": (
            f"**{stuck_count} processing jobs are stuck!**\n"
            f"Oldest job: **{oldest_age_minutes:.0f} minutes** old\n\n"
            "Workers cannot receive new jobs until these are cleared."
        ),
        "fields": [
            {"name": "Stuck Jobs", "value": str(stuck_count), "inline": True},
            {"name": "Oldest Age", "value": f"{oldest_age_minutes:.0f} min", "inline": True},
        ],
        "timestamp": datetime.utcnow().isoformat(),
    }

    if details_str:
        embed["fields"].append({"name": "By Model", "value": details_str, "inline": False})

    embed["fields"].append({
        "name": "Action Required",
        "value": "Run queue cleanup or check worker connectivity",
        "inline": False,
    })

    send_embed(webhook_url, embed)


def notify_queue_health(
    processing_count: int,
    waiting_count: int,
    waiting_images: int,
    active_workers: int,
    stuck_count: int = 0,
):
    """
    Queue health status update (can be periodic).
    Only sends if there are issues or on request.
    """
    webhook_url = _get_core_webhook()
    if not webhook_url:
        return

    # Determine status color
    if stuck_count > 0:
        color = 0xFF0000  # Red
        status = "🔴 UNHEALTHY"
    elif processing_count > waiting_count * 2:
        color = 0xFFA500  # Orange
        status = "🟡 WARNING"
    else:
        color = 0x00FF00  # Green
        status = "🟢 HEALTHY"

    embed = {
        "title": f"Queue Status: {status}",
        "color": color,
        "fields": [
            {"name": "Active Workers", "value": str(active_workers), "inline": True},
            {"name": "Processing", "value": str(processing_count), "inline": True},
            {"name": "Queued Jobs", "value": str(waiting_count), "inline": True},
            {"name": "Queued Images", "value": str(waiting_images), "inline": True},
        ],
        "timestamp": datetime.utcnow().isoformat(),
    }

    if stuck_count > 0:
        embed["fields"].append({
            "name": "⚠️ Stuck Jobs",
            "value": f"**{stuck_count}** (blocking new jobs!)",
            "inline": False,
        })

    send_embed(webhook_url, embed)


def notify_worker_not_receiving_jobs(
    worker_name: str,
    worker_id: str,
    models: list,
    stuck_jobs: int = 0,
    queued_jobs: int = 0,
):
    """
    Alert when a worker appears online but isn't receiving jobs.
    """
    webhook_url = _get_core_webhook()
    if not webhook_url:
        return

    models_str = ", ".join(models[:3]) if models else "None"
    if models and len(models) > 3:
        models_str += f" (+{len(models) - 3} more)"

    embed = {
        "title": "⚠️ Worker Not Receiving Jobs",
        "color": 0xFFA500,  # Orange
        "description": (
            f"Worker **{worker_name}** is online but not getting jobs.\n"
            "This may indicate stuck processing_gens or model mismatch."
        ),
        "fields": [
            {"name": "Worker", "value": worker_name, "inline": True},
            {"name": "Models", "value": models_str, "inline": True},
            {"name": "Stuck Jobs", "value": str(stuck_jobs), "inline": True},
            {"name": "Queued Jobs", "value": str(queued_jobs), "inline": True},
        ],
        "footer": {"text": f"ID: {worker_id}"},
        "timestamp": datetime.utcnow().isoformat(),
    }

    send_embed(webhook_url, embed)


def notify_jobs_cleared(cleared_processing: int, cleared_waiting: int, reason: str = "manual"):
    """
    Notification that stuck jobs were cleared.
    """
    webhook_url = _get_core_webhook()
    if not webhook_url:
        return

    embed = {
        "title": "🧹 Queue Cleaned",
        "color": 0x00FF00,  # Green
        "fields": [
            {"name": "Processing Cleared", "value": str(cleared_processing), "inline": True},
            {"name": "Waiting Cleared", "value": str(cleared_waiting), "inline": True},
            {"name": "Reason", "value": reason, "inline": False},
        ],
        "timestamp": datetime.utcnow().isoformat(),
    }

    send_embed(webhook_url, embed)


# ============================================================================
# Loguru Integration - Add as a log sink
# ============================================================================


def discord_log_sink(message):
    """
    Loguru sink that sends ERROR and CRITICAL logs to Discord.
    Add this to logger with: logger.add(discord_log_sink, level="ERROR")
    """
    webhook_url = _get_core_webhook()
    if not webhook_url:
        return

    record = message.record
    level = record["level"].name

    # Only send ERROR and CRITICAL
    if level not in ("ERROR", "CRITICAL"):
        return

    msg_text = str(record["message"])

    # Filter out expected messages that happen on every restart
    ignore_patterns = [
        "Quorum changed to port",  # Normal on restart
    ]
    for pattern in ignore_patterns:
        if pattern in msg_text:
            return

    color = 0xFF0000 if level == "CRITICAL" else 0xFFA500  # Red for critical, orange for error

    # Truncate message if too long
    msg_text = msg_text[:1500]

    embed = {
        "title": f"{'🔥' if level == 'CRITICAL' else '❌'} {level}",
        "color": color,
        "description": f"```\n{msg_text}\n```",
        "fields": [
            {"name": "Location", "value": f"{record['name']}:{record['function']}:{record['line']}", "inline": True},
        ],
        "timestamp": datetime.utcnow().isoformat(),
    }

    # Add exception info if present
    if record["exception"]:
        exc_text = str(record["exception"])[:500]
        embed["fields"].append({"name": "Exception", "value": f"```\n{exc_text}\n```", "inline": False})

    send_embed(webhook_url, embed)
