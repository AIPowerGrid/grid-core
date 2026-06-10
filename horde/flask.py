# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import os

from flask import Flask
from flask_caching import Cache
from flask_sqlalchemy import SQLAlchemy
from werkzeug.middleware.proxy_fix import ProxyFix

from horde.logger import logger
from horde.redis_ctrl import ger_cache_url, is_redis_up

cache = None
SQLITE_MODE = os.getenv("USE_SQLITE", "0") == "1"


def create_app():
    HORDE = Flask(__name__)
    HORDE.config.SWAGGER_UI_DOC_EXPANSION = "list"
    HORDE.wsgi_app = ProxyFix(HORDE.wsgi_app, x_for=1)

    if SQLITE_MODE:
        logger.warning("Using SQLite for database")
        HORDE.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///horde.db"
    else:
        HORDE.config["SQLALCHEMY_DATABASE_URI"] = (
            f"postgresql://{os.getenv('POSTGRES_USER', 'postgres')}:" f"{os.getenv('POSTGRES_PASS')}@{os.getenv('POSTGRES_URL')}"
        )
        # Connection-pool sizing. The previous config (pool_size 50, unlimited
        # overflow) let a single process open an unbounded number of
        # connections — under load, 8 processes blew past Postgres's
        # max_connections and every new request got "FATAL: too many
        # connections". Now finite and env-tunable.
        #
        # Budget the total: (DB_POOL_SIZE + DB_MAX_OVERFLOW) * num_processes
        # must stay under Postgres max_connections (minus grid_api's pools and
        # headroom). With the default 8 Flask procs and these defaults that's
        # 8 * (10 + 15) = 200 worst-case — raise Postgres max_connections to
        # ~300 and/or front it with pgbouncer before scaling processes.
        HORDE.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "pool_size": int(os.getenv("DB_POOL_SIZE", "10")),
            "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "15")),
            "pool_pre_ping": True,        # drop dead connections instead of erroring mid-request
            "pool_recycle": int(os.getenv("DB_POOL_RECYCLE", "1800")),  # recycle every 30 min
        }
    HORDE.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(HORDE)

    if not SQLITE_MODE:
        with HORDE.app_context():
            logger.warning(f"pool size = {db.engine.pool.size()}")
    logger.init_ok("Horde Database", status="Started")

    return HORDE


db = SQLAlchemy()
HORDE = create_app()

if is_redis_up():
    try:
        cache_config = {
            "CACHE_REDIS_URL": ger_cache_url(),
            "CACHE_TYPE": "RedisCache",
            "CACHE_DEFAULT_TIMEOUT": 300,
        }
        cache = Cache(config=cache_config)
        cache.init_app(HORDE)
        logger.init_ok("Flask Cache", status="Connected")
    except Exception as e:
        logger.error(f"Flask Cache Failed: {e}")

# Allow local workstation run
if cache is None:
    cache_config = {"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 300}
    cache = Cache(config=cache_config)
    cache.init_app(HORDE)
    logger.init_warn("Flask Cache", status="SimpleCache")
