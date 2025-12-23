#!/usr/bin/env python3

import os
import hashlib
from dotenv import load_dotenv

# First try to load from .env file
print("Loading environment variables...")
load_dotenv()

# Get the secret key as it would be used in the actual code
secret_key_from_env = os.getenv("secret_key", "s0m3s3cr3t")
print(f"Secret key from environment: {secret_key_from_env}")

# Recreate the hash_api_key function from horde/utils.py
def hash_api_key(unhashed_api_key, salt=None):
    if salt is None:
        salt = os.getenv("secret_key", "s0m3s3cr3t")
    return hashlib.sha256(salt.encode() + unhashed_api_key.encode()).hexdigest()

# Test with a sample API key
test_api_key = "test_key_12345"
hashed_with_env = hash_api_key(test_api_key)
hashed_with_default = hash_api_key(test_api_key, "s0m3s3cr3t")

print("\nHashing results:")
print(f"Hash with environment key: {hashed_with_env}")
print(f"Hash with default key:     {hashed_with_default}")

print("\nAre the hashes the same?", hashed_with_env == hashed_with_default)

# If they're different, this means the secret_key in the environment is being used
# If they're the same, either the secret_key is not set or it's set to the default

print("\nTo see what's in your database, you can hash an actual API key with both methods")
print("and check which one matches what's stored in the database.") 