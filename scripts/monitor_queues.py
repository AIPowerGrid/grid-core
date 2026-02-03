#!/usr/bin/env python3
"""
Queue Monitor for AI Power Grid
================================
Monitors processing_gens and waiting_prompts for stuck jobs.
Sends Discord alerts when issues are detected.

Run manually:
    python scripts/monitor_queues.py

Run with auto-cleanup:
    python scripts/monitor_queues.py --cleanup

Run as cron (every 5 minutes):
    */5 * * * * cd /home/aipg/aipg && python scripts/monitor_queues.py >> /var/log/queue_monitor.log 2>&1
"""

import argparse
import os
import sys
from datetime import datetime

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
STUCK_THRESHOLD_MINUTES = 10  # Jobs older than this are considered stuck
ALERT_THRESHOLD = 5  # Only alert if more than this many stuck jobs
AUTO_CLEANUP_THRESHOLD_MINUTES = 30  # Auto-cleanup jobs older than this


def get_db_connection():
    """Get database connection from environment."""
    postgres_url = os.getenv("POSTGRES_URL", "localhost/postgres")
    postgres_pass = os.getenv("POSTGRES_PASS", "")

    # Parse URL (format: host/database)
    if "/" in postgres_url:
        host, database = postgres_url.split("/", 1)
    else:
        host = postgres_url
        database = "postgres"

    return psycopg2.connect(host=host, database=database, user="postgres", password=postgres_pass)


def check_stuck_jobs(conn):
    """Check for stuck processing_gens."""
    cur = conn.cursor()

    # Get stuck processing_gens
    cur.execute(
        f"""
        SELECT
            pg.model,
            COUNT(*) as count,
            MIN(EXTRACT(EPOCH FROM (NOW() - pg.created))/60) as oldest_minutes,
            MAX(EXTRACT(EPOCH FROM (NOW() - pg.created))/60) as newest_minutes
        FROM processing_gens pg
        WHERE pg.created < NOW() - INTERVAL '{STUCK_THRESHOLD_MINUTES} minutes'
        GROUP BY pg.model
        ORDER BY count DESC
    """,
    )

    stuck_by_model = []
    total_stuck = 0
    oldest_age = 0

    for row in cur.fetchall():
        model, count, oldest, newest = row
        stuck_by_model.append(
            {
                "model": model,
                "count": count,
                "oldest_minutes": float(oldest) if oldest else 0,
            },
        )
        total_stuck += count
        if oldest and float(oldest) > oldest_age:
            oldest_age = float(oldest)

    return total_stuck, oldest_age, stuck_by_model


def check_queue_health(conn):
    """Get overall queue health metrics."""
    cur = conn.cursor()

    # Processing count
    cur.execute("SELECT COUNT(*) FROM processing_gens")
    processing_count = cur.fetchone()[0]

    # Waiting prompts count
    cur.execute("SELECT COUNT(*), COALESCE(SUM(n), 0) FROM waiting_prompts WHERE n > 0")
    row = cur.fetchone()
    waiting_count = row[0]
    waiting_images = row[1]

    # Active workers (checked in last 5 minutes)
    cur.execute(
        """
        SELECT COUNT(*) FROM workers
        WHERE last_check_in > NOW() - INTERVAL '5 minutes'
    """,
    )
    active_workers = cur.fetchone()[0]

    return {
        "processing_count": processing_count,
        "waiting_count": waiting_count,
        "waiting_images": int(waiting_images),
        "active_workers": active_workers,
    }


def cleanup_stuck_jobs(conn, max_age_minutes=None):
    """Clean up stuck processing_gens and orphaned waiting_prompts."""
    if max_age_minutes is None:
        max_age_minutes = AUTO_CLEANUP_THRESHOLD_MINUTES

    cur = conn.cursor()

    # Delete stuck processing_gens
    cur.execute(
        f"""
        DELETE FROM processing_gens
        WHERE created < NOW() - INTERVAL '{max_age_minutes} minutes'
        RETURNING id
    """,
    )
    deleted_pgs = len(cur.fetchall())

    # Clean orphaned wp_models
    cur.execute(
        """
        DELETE FROM wp_models
        WHERE wp_id IN (
            SELECT wp.id FROM waiting_prompts wp
            LEFT JOIN processing_gens pg ON pg.wp_id = wp.id
            WHERE pg.id IS NULL AND wp.n = 0
        )
    """,
    )

    # Clean orphaned waiting_prompts
    cur.execute(
        """
        DELETE FROM waiting_prompts wp
        WHERE NOT EXISTS (SELECT 1 FROM processing_gens pg WHERE pg.wp_id = wp.id)
        AND wp.n = 0
        RETURNING id
    """,
    )
    deleted_wps = len(cur.fetchall())

    conn.commit()

    return deleted_pgs, deleted_wps


def send_discord_alert(stuck_count, oldest_age, details):
    """Send Discord alert for stuck jobs."""
    try:
        from horde.discord import notify_stuck_jobs_alert

        notify_stuck_jobs_alert(stuck_count, oldest_age, details)
        print(f"[{datetime.now()}] Discord alert sent: {stuck_count} stuck jobs")
    except Exception as e:
        print(f"[{datetime.now()}] Failed to send Discord alert: {e}")


def send_cleanup_notification(cleared_pg, cleared_wp):
    """Send Discord notification for cleanup."""
    try:
        from horde.discord import notify_jobs_cleared

        notify_jobs_cleared(cleared_pg, cleared_wp, "automatic (monitor)")
        print(f"[{datetime.now()}] Discord cleanup notification sent")
    except Exception as e:
        print(f"[{datetime.now()}] Failed to send cleanup notification: {e}")


def main():
    parser = argparse.ArgumentParser(description="Monitor AI Power Grid job queues")
    parser.add_argument("--cleanup", action="store_true", help="Auto-cleanup stuck jobs")
    parser.add_argument(
        "--cleanup-age",
        type=int,
        default=AUTO_CLEANUP_THRESHOLD_MINUTES,
        help=f"Age in minutes for auto-cleanup (default: {AUTO_CLEANUP_THRESHOLD_MINUTES})",
    )
    parser.add_argument("--quiet", action="store_true", help="Only output on issues")
    parser.add_argument("--no-alert", action="store_true", help="Don't send Discord alerts")
    args = parser.parse_args()

    try:
        conn = get_db_connection()
    except Exception as e:
        print(f"[{datetime.now()}] ERROR: Cannot connect to database: {e}")
        sys.exit(1)

    try:
        # Check for stuck jobs
        stuck_count, oldest_age, stuck_by_model = check_stuck_jobs(conn)

        # Get queue health
        health = check_queue_health(conn)

        # Output status
        if not args.quiet or stuck_count > 0:
            print(f"[{datetime.now()}] Queue Status:")
            print(f"  Active Workers: {health['active_workers']}")
            print(f"  Processing: {health['processing_count']}")
            print(f"  Waiting Jobs: {health['waiting_count']} ({health['waiting_images']} images)")
            print(f"  Stuck Jobs: {stuck_count} (oldest: {oldest_age:.0f} min)")

        # Alert if stuck jobs exceed threshold
        if stuck_count >= ALERT_THRESHOLD and not args.no_alert:
            print(f"[{datetime.now()}] WARNING: {stuck_count} stuck jobs detected!")
            for detail in stuck_by_model:
                print(f"    - {detail['model']}: {detail['count']} jobs ({detail['oldest_minutes']:.0f}m old)")

            send_discord_alert(stuck_count, oldest_age, stuck_by_model)

        # Auto-cleanup if requested
        if args.cleanup and stuck_count > 0:
            print(f"[{datetime.now()}] Cleaning up stuck jobs older than {args.cleanup_age} minutes...")
            cleared_pg, cleared_wp = cleanup_stuck_jobs(conn, args.cleanup_age)
            print(f"[{datetime.now()}] Cleaned: {cleared_pg} processing_gens, {cleared_wp} waiting_prompts")

            if cleared_pg > 0 or cleared_wp > 0:
                if not args.no_alert:
                    send_cleanup_notification(cleared_pg, cleared_wp)

        conn.close()

        # Exit with error code if stuck jobs found (useful for alerting)
        if stuck_count >= ALERT_THRESHOLD:
            sys.exit(1)

    except Exception as e:
        print(f"[{datetime.now()}] ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
