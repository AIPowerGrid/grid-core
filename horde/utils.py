# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import hashlib
import json
import os
import random
import secrets
import uuid
from datetime import datetime

import bleach
import dateutil.relativedelta
import regex as re
from better_profanity import profanity
from profanity_check import predict

from horde import exceptions as e
from horde.flask import SQLITE_MODE

profanity.load_censor_words()

random.seed(random.SystemRandom().randint(0, 2**32 - 1))


def is_profane(text):
    if profanity.contains_profanity(text):
        return True
    if predict([text]) == [1]:
        return True
    return False


def count_digits(number):
    digits = 1
    while number > 10:
        number = number / 10
        digits += 1
    return digits


class ConvertAmount:
    def __init__(self, amount, decimals=1):
        self.digits = count_digits(amount)
        self.decimals = decimals
        if self.digits < 4:
            self.amount = round(amount, self.decimals)
            self.prefix = ""
            self.char = ""
        elif self.digits < 7:
            self.amount = round(amount / 1000, self.decimals)
            self.prefix = "kilo"
            self.char = "K"
        elif self.digits < 10:
            self.amount = round(amount / 1_000_000, self.decimals)
            self.prefix = "mega"
            self.char = "M"
        elif self.digits < 13:
            self.amount = round(amount / 1_000_000_000, self.decimals)
            self.prefix = "giga"
            self.char = "G"
        elif self.digits < 16:
            self.amount = round(amount / 1_000_000_000_000, self.decimals)
            self.prefix = "tera"
            self.char = "T"
        else:
            self.amount = round(amount / 1_000_000_000_000_000, self.decimals)
            self.prefix = "peta"
            self.char = "P"


def get_db_uuid():
    if SQLITE_MODE:
        return str(uuid.uuid4())
    return uuid.uuid4()


def generate_client_id():
    return secrets.token_urlsafe(16)


def sanitize_string(text):
    return bleach.clean(text).lstrip().rstrip()


_API_KEY_SALT = None


def _get_api_key_salt():
    """Read the API-key salt from GRID_SALT, exactly once, failing loudly.

    History: the original deployment hardcoded "s0m3s3cr3t" because the .env
    secret was never parsed, and the database filled with keys hashed against
    the known default. That deployment is being retired with a fresh database.
    This version refuses to run without a real secret, and refuses the
    known-compromised value, so the failure mode can never repeat silently.

    The same GRID_SALT env var is read by grid_api/auth.py and the dashboard
    (grid-frontend generate-api-key route) — all three must share it so keys
    hash identically everywhere.
    """
    global _API_KEY_SALT
    if _API_KEY_SALT is None:
        salt = os.getenv("GRID_SALT")
        if not salt:
            raise RuntimeError(
                "GRID_SALT is not set. Refusing to hash API keys without a real "
                "secret — set GRID_SALT in the environment (see deploy/env.template). "
                "It must match grid_api and the dashboard."
            )
        if salt == "s0m3s3cr3t":
            raise RuntimeError(
                "GRID_SALT is set to the known-compromised legacy value. "
                "Generate a fresh secret (e.g. `openssl rand -hex 32`)."
            )
        _API_KEY_SALT = salt
    return _API_KEY_SALT


def hash_api_key(unhashed_api_key):
    return hashlib.sha256(_get_api_key_salt().encode() + unhashed_api_key.encode()).hexdigest()


def hash_dictionary(dictionary):
    # Convert the dictionary to a JSON string
    json_string = json.dumps(dictionary, sort_keys=True)
    # Create a hash object
    hash_object = hashlib.sha256(json_string.encode())
    # Get the hexadecimal representation of the hash
    return hash_object.hexdigest()


def get_message_expiry_date():
    return datetime.utcnow() + dateutil.relativedelta.relativedelta(hours=+12)


def get_expiry_date():
    return datetime.utcnow() + dateutil.relativedelta.relativedelta(hours=+24)


def get_extra_slow_expiry_date():
    return datetime.utcnow() + dateutil.relativedelta.relativedelta(hours=+24)


def get_interrogation_form_expiry_date():
    return datetime.utcnow() + dateutil.relativedelta.relativedelta(minutes=+3)


def get_random_seed(start_point=0):
    """Generated a random seed, using a random number unique per node"""
    return random.randint(start_point, 2**32 - 1)


def count_parentheses(s):
    open_p = False
    count = 0
    for c in s:
        if c == "(":
            open_p = True
        elif c == ")" and open_p:
            open_p = False
            count += 1
    return count


def validate_regex(regex_string):
    try:
        re.compile(regex_string, re.IGNORECASE)
    except Exception:
        return False
    return True


def does_extra_text_reference_exist(extra_texts, reference):
    for et in extra_texts:
        if et["reference"] == reference:
            return True
    return False


def ensure_clean(string, key):
    if is_profane(string):
        raise e.BadRequest(f"{key} contains profanity")
    return sanitize_string(string)
