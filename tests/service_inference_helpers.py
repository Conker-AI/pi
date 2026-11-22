"""Shared synthetic keys and the key digest for the service-inference tests."""

import hashlib

MEMORYGATE, TOOLGATE, OWNER = "memorygate-key-synthetic", "toolgate-key-synthetic", "owner-key"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()
