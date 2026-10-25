"""Starts Django the way a real server does (DEBUG off, https on) and asks Django's own
production checklist (`check --deploy`). Fails if somebody weakens the security settings."""

import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

PRODUCTION_ENV = {
    "DEBUG": "False",
    "HTTPS_ONLY": "True",
    "BEHIND_PROXY": "True",
    "HSTS_INCLUDE_SUBDOMAINS": "True",
    "SECRET_KEY": "a-long-random-production-style-secret-key-0123456789-abcdefghijklmnop",
    "ALLOWED_HOSTS": "example.com",
}


def run_manage(*args, **env):
    return subprocess.run(
        [sys.executable, "manage.py", *args],
        cwd=PROJECT,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_production_settings_pass_the_django_deploy_checklist():
    result = run_manage("check", "--deploy", "--fail-level", "WARNING", **PRODUCTION_ENV)
    assert result.returncode == 0, result.stdout + result.stderr


def test_without_the_https_switch_the_checklist_complains():
    """Proves the test above really checks something: weak settings must fail it."""
    weak = {**PRODUCTION_ENV, "HTTPS_ONLY": "False"}
    result = run_manage("check", "--deploy", "--fail-level", "WARNING", **weak)
    assert result.returncode != 0
    assert "SECURE_SSL_REDIRECT" in result.stderr + result.stdout
