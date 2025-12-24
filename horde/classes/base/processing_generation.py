# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import random
from datetime import datetime

import requests
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import expression

from horde.flask import SQLITE_MODE, db
from horde.logger import logger
from horde.utils import get_db_uuid

uuid_column_type = lambda: UUID(as_uuid=True) if not SQLITE_MODE else db.String(36)  # FIXME # noqa E731
json_column_type = JSONB if not SQLITE_MODE else JSON


class ProcessingGeneration(db.Model):
    """For storing processing generations in the DB"""

    __tablename__ = "processing_gens"
    __mapper_args__ = {
        "polymorphic_identity": "template",
        "polymorphic_on": "procgen_type",
    }
    id = db.Column(uuid_column_type(), primary_key=True, default=get_db_uuid)
    procgen_type = db.Column(db.String(30), nullable=False, index=True)
    generation = db.Column(db.Text)
    gen_metadata = db.Column(json_column_type, nullable=True)

    model = db.Column(db.String(255), default="", nullable=False)
    seed = db.Column(db.BigInteger, default=0, nullable=False)
    start_time = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    cancelled = db.Column(db.Boolean, default=False, nullable=False)
    faulted = db.Column(db.Boolean, default=False, nullable=False)
    fake = db.Column(db.Boolean, default=False, nullable=False)
    censored = db.Column(
        db.Boolean,
        default=False,
        nullable=False,
        server_default=expression.literal(False),
    )
    job_ttl = db.Column(db.Integer, default=150, nullable=False, index=True)
    
    # Progress tracking fields for real-time updates
    progress_percent = db.Column(db.Integer, default=0, nullable=False)
    current_step = db.Column(db.Integer, default=0, nullable=False)
    total_steps = db.Column(db.Integer, default=0, nullable=False)
    progress_updated_at = db.Column(db.DateTime, nullable=True)

    wp_id = db.Column(
        uuid_column_type(),
        db.ForeignKey("waiting_prompts.id", ondelete="CASCADE"),
        nullable=False,
    )
    worker_id = db.Column(uuid_column_type(), db.ForeignKey("workers.id"), nullable=False)
    wallet_id = db.Column(db.String(42), nullable=True, index=True)  # EVM wallet for Web3 rewards
    media_type = db.Column(db.String(10), default="image", nullable=False, index=True)  # "image" or "video"
    created = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    
    # File storage and metadata
    tags = db.Column(json_column_type, nullable=True)  # JSON array of strings for categorization
    r2_download_url = db.Column(db.Text, nullable=True)  # Direct download URL for the generated file
    file_size = db.Column(db.BigInteger, nullable=True)  # File size in bytes

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # If there has been no explicit model requested by the user, we just choose the first available from the worker
        db.session.add(self)
        db.session.commit()
        if kwargs.get("model") is None:
            worker_models = self.worker.get_model_names()
            if len(worker_models):
                self.model = worker_models[0]
            else:
                self.model = ""
            # If we reached this point, it means there is at least 1 matching model between worker and client
            # so we pick the first one.
            wp_models = self.wp.get_model_names()
            logger.info(f"🔍 ProcessingGeneration: wp_id={self.wp.id}, wp_models={wp_models}, worker_models={worker_models[:5]}...")
            matching_models = worker_models
            if len(wp_models) != 0:
                matching_models = [model for model in self.wp.get_model_names() if model in worker_models]
                logger.info(f"🔍 ProcessingGeneration: Found {len(matching_models)} matching models: {matching_models[:5]}...")
            else:
                logger.warning(f"🔍 ProcessingGeneration: WP has NO models! Will use worker's models randomly.")
            if len(matching_models) == 0:
                logger.warning(
                    f"Unexpectedly No models matched between worker and request!: Worker Models: {worker_models}. "
                    f"Request Models: {wp_models}. Will use random worker model.",
                )
                matching_models = worker_models
            random.shuffle(matching_models)
            self.model = matching_models[0]
            logger.info(f"🔍 ProcessingGeneration: Selected model '{self.model}' from matching_models={matching_models[:5]} (worker_models={worker_models[:5]}, wp_models={wp_models[:5]})")
        else:
            self.model = kwargs["model"]
            logger.info(f"🔍 ProcessingGeneration: Using explicit model '{self.model}'")
        self.set_job_ttl()
        db.session.commit()

    def set_generation(self, generation, things_per_sec, **kwargs):
        if self.is_completed():
            return 0
        # We return -1 to know to send a different error
        if self.is_faulted():
            return -1
        # Sanitize NUL char away from string literal we store in the DB
        self.generation = generation.replace("\x00", "\uFFFD")
        # Support for two typical properties
        self.seed = kwargs.get("seed", None)
        self.gen_metadata = kwargs.get("gen_metadata", None)
        # File storage and metadata
        self.tags = kwargs.get("tags", None)
        self.r2_download_url = kwargs.get("r2_download_url", None)
        self.file_size = kwargs.get("file_size", None)
        kudos = self.get_gen_kudos()
        self.cancelled = False
        self.record(things_per_sec, kudos)
        self.send_webhook(kudos)
        db.session.commit()
        return kudos

    def cancel(self):
        """Cancelling requests in progress still rewards/burns the relevant amount of kudos"""
        if self.is_completed() or self.is_faulted():
            return None
        self.faulted = True
        # We  don't want cancelled requests to raise suspicion
        things_per_sec = self.worker.speed
        kudos = self.get_gen_kudos()
        self.cancelled = True
        self.record(things_per_sec, kudos)
        db.session.commit()
        return kudos * self.worker.get_bridge_kudos_multiplier()

    def record(self, things_per_sec, kudos):
        cancel_txt = ""
        if self.cancelled:
            cancel_txt = " Cancelled"
        if self.fake and self.worker.user == self.wp.user:
            # We do not record usage for paused workers, unless the requestor was the same owner as the worker
            self.worker.record_contribution(raw_things=self.wp.things, kudos=kudos, things_per_sec=things_per_sec)
            logger.info(
                f"Fake{cancel_txt} Generation {self.id} worth {self.kudos} kudos, delivered by worker: "
                f"{self.worker.name} for wp {self.wp.id}",
            )
        else:
            self.worker.record_contribution(raw_things=self.wp.things, kudos=kudos, things_per_sec=things_per_sec)
            self.wp.record_usage(raw_things=self.wp.things, kudos=self.adjust_user_kudos(kudos))
            log_string = (
                f"New{cancel_txt} Generation {self.id} worth {kudos} kudos, delivered by worker: {self.worker.name} for wp {self.wp.id} "
            )
            log_string += f" (requesting user {self.wp.user.get_unique_alias()} [{self.wp.ipaddr}])"
            logger.info(log_string)

    def adjust_user_kudos(self, kudos):
        if self.censored:
            return 0
        return kudos

    def abort(self):
        """Called when this request needs to be stopped without rewarding kudos. Say because it timed out due to a worker crash"""
        if self.is_completed() or self.is_faulted():
            return
        self.faulted = True
        self.worker.log_aborted_job()
        self.log_aborted_generation()
        db.session.commit()

    def log_aborted_generation(self):
        logger.info(f"Aborted Stale Generation {self.id} from by worker: {self.worker.name} ({self.worker.id})")

    # Overridable function
    def get_gen_kudos(self):
        return self.wp.kudos
        # return(database.convert_things_to_kudos(self.wp.things, seed = self.seed, model_name = self.model))

    def is_completed(self):
        if self.generation is not None:
            return True
        return False

    def is_faulted(self):
        return self.faulted

    def is_stale(self):
        if self.is_completed() or self.is_faulted():
            return False
        return (datetime.utcnow() - self.start_time).total_seconds() > self.job_ttl

    def delete(self):
        db.session.delete(self)
        db.session.commit()

    def get_seconds_needed(self):
        return self.wp.things / self.worker.speed

    def get_expected_time_left(self):
        if self.is_completed():
            return 0
        seconds_needed = self.get_seconds_needed()
        seconds_elapsed = (datetime.utcnow() - self.start_time).total_seconds()
        expected_time = seconds_needed - seconds_elapsed
        # In case we run into a slow request
        if expected_time < 0:
            expected_time = 0
        return expected_time

    # This should be extended by every horde type
    def get_details(self):
        """Returns a dictionary with details about this processing generation"""
        ret_dict = {
            "gen": self.generation,
            "worker_id": self.worker.id,
            "worker_name": self.worker.name,
            "model": self.model,
            "gen_metadata": self.gen_metadata if self.gen_metadata is not None else [],
            "progress": self.get_progress(),
            "tags": self.tags if self.tags is not None else [],
            "r2_download_url": self.r2_download_url,
            "file_size": self.file_size,
        }
        return ret_dict

    # Extendable function to be able to dynamically adjust the amount of things
    # based on what the worker actually returned.
    # Typically needed for LLMs using EOS tokens etc
    def get_things_count(self, generation):
        return self.wp.things

    def send_webhook(self, kudos):
        if not self.wp.webhook:
            return
        data = self.get_details()
        data["request"] = str(self.wp.id)
        data["id"] = str(self.id)
        data["kudos"] = kudos
        data["worker_id"] = str(data["worker_id"])
        for riter in range(3):
            try:
                req = requests.post(self.wp.webhook, json=data, timeout=3)
                if not req.ok:
                    logger.debug(
                        f"Something went wrong when sending generation webhook: {req.status_code} - {req.text}. "
                        f"Will retry {3-riter-1} more times...",
                    )
                    continue
                break
            except Exception as err:
                logger.debug(f"Exception when sending generation webhook: {err}. Will retry {3-riter-1} more times...")

    def set_job_ttl(self):
        """Returns how many seconds each job request should stay waiting before considering it stale and cancelling it
        This function should be overriden by the invididual hordes depending on how the calculating ttl
        """
        # No timeout - allow jobs to run as long as needed (24 hours)
        self.job_ttl = 86400
        db.session.commit()

    def update_progress(self, current_step: int, total_steps: int):
        """Update the progress of this generation job"""
        if self.is_completed() or self.is_faulted():
            return False
        self.current_step = current_step
        self.total_steps = total_steps
        self.progress_percent = int((current_step / max(total_steps, 1)) * 100)
        self.progress_updated_at = datetime.utcnow()
        db.session.commit()
        return True

    def get_progress(self):
        """Get current progress info"""
        return {
            "progress_percent": self.progress_percent,
            "current_step": self.current_step,
            "total_steps": self.total_steps,
            "progress_updated_at": self.progress_updated_at.isoformat() if self.progress_updated_at else None,
        }
