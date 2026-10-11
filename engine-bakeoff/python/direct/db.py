"""A psycopg connection configured from the same environment as Django's
settings. Foundation-written for the python-direct side."""

import json
import os

import psycopg


def connect(**overrides):
    """One connection, in autocommit mode, so every transaction is an explicit
    `with conn.transaction()`. Prepared statements are on unless the
    connection goes through pgBouncer in transaction pooling mode."""
    pooled = os.environ.get("DATABASE_POOLED") == "1"
    params = {
        "dbname": os.environ.get("DATABASE_NAME", "hivear"),
        "user": os.environ.get("DATABASE_USER", "hivear"),
        "password": os.environ.get("DATABASE_PASSWORD", "local-evaluation-only"),
        "host": os.environ.get("DATABASE_HOST", "localhost"),
        "port": os.environ.get("DATABASE_PORT", "5432"),
    }
    params.update(overrides)
    conn = psycopg.connect(**params, autocommit=True, prepare_threshold=None if pooled else 5)
    conn.execute("SET TIME ZONE 'UTC'")
    return conn


def as_json(value):
    """jsonb comes back decoded on a plain connection, and as text where a
    text loader is registered; accept both."""
    return json.loads(value) if isinstance(value, str) else value
