"""S3 object storage: the single source of truth shared by pipeline steps.

Layout inside the bucket (default ``aap-migration-data``)::

    migrations/<migration-id>/workspace/<exports|xformed|reports|logs|database|schemas>/...
    migrations/<migration-id>/agent/        plan, agent log, summary
    migrations/<migration-id>/dataset/      dataset.csv, datacard.json
    migrations/<migration-id>/ai/           AI readiness report
    migrations/<migration-id>/casc/         generated Configuration as Code
    models/aap-migration-risk/<version>/    trained model + model card
    models/aap-migration-risk/latest.json   pointer to the current version

Every pipeline step pulls what it needs, works in a local directory and
pushes its results back, so steps can run on any node and any storage class.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable

from migration_ai.config import WORKSPACE_FOLDERS, S3Settings

log = logging.getLogger(__name__)


class Store:
    def __init__(self, settings: S3Settings):
        import boto3
        from botocore.config import Config

        if not settings.configured:
            raise RuntimeError(
                "S3 is not configured: set AWS_S3_BUCKET, AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY "
                "(the 'aap-migration-data' connection)."
            )
        self.bucket = settings.bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.endpoint or None,
            aws_access_key_id=settings.access_key,
            aws_secret_access_key=settings.secret_key,
            region_name=settings.region or None,
            verify=settings.verify_tls,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def ensure_bucket(self, name: str | None = None) -> None:
        name = name or self.bucket
        existing = {b["Name"] for b in self.client.list_buckets().get("Buckets", [])}
        if name not in existing:
            self.client.create_bucket(Bucket=name)
            log.info("created bucket %s", name)

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix.rstrip("/") + "/"):
            keys.extend(o["Key"] for o in page.get("Contents", []))
        return keys

    def list_children(self, prefix: str) -> list[str]:
        """Immediate 'sub-folders' under a prefix."""
        out: list[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix.rstrip("/") + "/", Delimiter="/"):
            for p in page.get("CommonPrefixes", []):
                out.append(p["Prefix"].rstrip("/").rsplit("/", 1)[-1])
        return sorted(out)

    def put_file(self, local: Path, key: str) -> None:
        self.client.upload_file(str(local), self.bucket, key)

    def get_file(self, key: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, key, str(local))

    def put_json(self, key: str, data: Any) -> None:
        self.client.put_object(
            Bucket=self.bucket, Key=key, Body=json.dumps(data, indent=1, default=str).encode(),
            ContentType="application/json",
        )

    def get_json(self, key: str, default: Any = None) -> Any:
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            return json.loads(body)
        except Exception:
            return default

    def put_text(self, key: str, text: str, content_type: str = "text/plain") -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=text.encode(), ContentType=content_type)

    def upload_dir(self, local: Path, prefix: str) -> int:
        n = 0
        if not local.is_dir():
            return 0
        for path in sorted(local.rglob("*")):
            if path.is_file():
                rel = path.relative_to(local).as_posix()
                self.put_file(path, f"{prefix.rstrip('/')}/{rel}")
                n += 1
        return n

    def list_objects(self, prefix: str) -> list[dict]:
        out: list[dict] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix.rstrip("/") + "/"):
            out.extend(page.get("Contents", []))
        return out

    def last_modified(self, key: str):
        try:
            return self.client.head_object(Bucket=self.bucket, Key=key)["LastModified"]
        except Exception:
            return None

    def download_dir(self, prefix: str, local: Path, sync: bool = False) -> int:
        """Download a 'folder'. Unchanged files (same size and date) are skipped; the local copy
        keeps the object's date. ``sync`` also removes local files that are no longer in S3."""
        n = 0
        prefix = prefix.rstrip("/")
        remote: set[Path] = set()
        for obj in self.list_objects(prefix):
            rel = obj["Key"][len(prefix) + 1:]
            if not rel or rel.endswith("/"):
                continue
            target = local / rel
            remote.add(target)
            ts = obj["LastModified"].timestamp()
            if target.is_file() and target.stat().st_size == obj["Size"] and abs(target.stat().st_mtime - ts) < 1:
                continue
            self.get_file(obj["Key"], target)
            os.utime(target, (ts, ts))
            n += 1
        if sync and local.is_dir():
            for path in sorted(local.rglob("*"), reverse=True):
                if path.is_file() and path not in remote:
                    path.unlink()
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
        return n

    def delete_prefix(self, prefix: str) -> int:
        keys = self.list_keys(prefix)
        for i in range(0, len(keys), 1000):
            chunk = [{"Key": k} for k in keys[i:i + 1000]]
            self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": chunk})
        return len(keys)


def workspace_prefix(migrations_prefix: str, migration_id: str) -> str:
    return f"{migrations_prefix}/{migration_id}/workspace"


def pull_workspace(store: Store, prefix: str, work_dir: Path,
                   folders: Iterable[str] = WORKSPACE_FOLDERS, sync: bool = False) -> dict[str, int]:
    """Download the toolkit folders of a migration into ``work_dir`` (``sync``: mirror, see download_dir)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for folder in folders:
        counts[folder] = store.download_dir(f"{prefix}/{folder}", work_dir / folder, sync=sync)
    return counts


def push_workspace(store: Store, prefix: str, work_dir: Path,
                   folders: Iterable[str] = WORKSPACE_FOLDERS, replace: bool = True) -> dict[str, int]:
    """Upload the toolkit folders of ``work_dir``; ``replace`` removes stale remote files first."""
    counts = {}
    for folder in folders:
        local = work_dir / folder
        if not local.is_dir():
            continue
        if replace:
            store.delete_prefix(f"{prefix}/{folder}")
        counts[folder] = store.upload_dir(local, f"{prefix}/{folder}")
    return counts


def local_workspace_summary(work_dir: Path) -> dict[str, int]:
    return {
        f: sum(1 for p in (work_dir / f).rglob("*") if p.is_file()) if (work_dir / f).is_dir() else 0
        for f in WORKSPACE_FOLDERS
    }


def env_s3_ok() -> bool:
    return bool(os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"))
