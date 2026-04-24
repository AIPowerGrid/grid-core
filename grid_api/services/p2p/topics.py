# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Gossipsub topic management for AIPG P2P.

Topic structure:
    /aipg/1/jobs/{model}     - Job broadcasts for a specific model
    /aipg/1/claims           - All job claims (global)
    /aipg/1/results/{job_id} - Results for a specific job
    /aipg/1/workers          - Worker announcements (heartbeats)
"""

from .config import get_p2p_config


def job_topic(model: str) -> str:
    """Get the gossipsub topic for jobs targeting a specific model.

    Args:
        model: Model name (e.g., "llama3.2:3b", "flux")

    Returns:
        Topic string (e.g., "/aipg/1/jobs/llama3.2:3b")
    """
    config = get_p2p_config()
    # Normalize model name (replace special chars)
    safe_model = model.replace("/", "-").replace(":", "-")
    return f"{config.topic_prefix}/jobs/{safe_model}"


def claims_topic() -> str:
    """Get the global claims topic.

    All claims are broadcast here so all nodes know which jobs are taken.
    """
    config = get_p2p_config()
    return f"{config.topic_prefix}/claims"


def results_topic(job_id: str) -> str:
    """Get the topic for a specific job's results.

    Args:
        job_id: The job UUID

    Returns:
        Topic string (e.g., "/aipg/1/results/abc123")
    """
    config = get_p2p_config()
    return f"{config.topic_prefix}/results/{job_id}"


def workers_topic() -> str:
    """Get the topic for worker announcements.

    Workers publish heartbeats here to announce their presence and capabilities.
    """
    config = get_p2p_config()
    return f"{config.topic_prefix}/workers"


def all_job_topics(models: list[str]) -> list[str]:
    """Get job topics for a list of models.

    Args:
        models: List of model names this worker supports

    Returns:
        List of topic strings
    """
    return [job_topic(model) for model in models]
