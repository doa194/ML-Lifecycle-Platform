"""Bootstrap and reset helpers: secret generation must be safe, and reset must really delete."""

from __future__ import annotations

import os
import re
import stat

from churn_platform.cli.stack import PLACEHOLDER, ensure_env_file, remove_tree
from churn_platform.settings import read_dotenv
from tests.support.data import REPO_ROOT


def test_bootstrap_replaces_every_placeholder_with_a_distinct_random_secret(tmp_path):
    (tmp_path / ".env.example").write_text((REPO_ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")

    created = ensure_env_file(tmp_path)

    values = read_dotenv(tmp_path / ".env")
    secrets = [v for k, v in values.items() if k.endswith(("PASSWORD", "SECRET_KEY"))]
    assert created
    assert PLACEHOLDER not in values.values()
    assert secrets and len(set(secrets)) == len(secrets)
    # Hex only: safe in URLs, shells and CLIs (a leading "-" once broke the MinIO client).
    assert all(re.fullmatch(r"[0-9a-f]{48}", secret) for secret in secrets)


def test_existing_env_file_is_never_overwritten(tmp_path):
    (tmp_path / ".env.example").write_text("OPS_DB_PASSWORD=change-me\n", encoding="utf-8")
    (tmp_path / ".env").write_text("OPS_DB_PASSWORD=already-used-by-the-volumes\n", encoding="utf-8")

    created = ensure_env_file(tmp_path)

    assert not created
    assert read_dotenv(tmp_path / ".env")["OPS_DB_PASSWORD"] == "already-used-by-the-volumes"


def test_reset_removes_read_only_files_such_as_the_dvc_cache(tmp_path):
    cache = tmp_path / ".dvc" / "cache" / "files" / "md5" / "ab"
    cache.mkdir(parents=True)
    blob = cache / "cdef0123"
    blob.write_bytes(b"dataset")
    os.chmod(blob, stat.S_IREAD)

    remove_tree(tmp_path / ".dvc" / "cache")

    assert not (tmp_path / ".dvc" / "cache").exists()
