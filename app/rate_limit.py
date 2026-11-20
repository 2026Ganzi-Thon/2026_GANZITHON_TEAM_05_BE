"""Shared IP-based rate limiter for public authentication endpoints."""

from slowapi import Limiter
from slowapi.util import get_remote_address


# In-memory storage is suitable for the current single-process app/demo.
# Use a shared Redis storage URI when deploying multiple workers or instances.
limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")
