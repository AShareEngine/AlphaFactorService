from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from factor_service.control_database import get_control_database
from factor_service.model_artifacts import ModelArtifactStore, ArtifactError


class DatasetArchiveRepository:
    """One immutable, complete object-store identity per frozen dataset."""

    def __init__(self, database: Any = None) -> None:
        self.database = database or get_control_database()

    def get(self, dataset_hash: str) -> dict[str, Any] | None:
        clean = ModelArtifactStore._dataset_hash(dataset_hash)
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT * FROM model_dataset_archives WHERE dataset_hash = %s",
                (clean,),
            ).fetchone()
        return dict(row) if row else None

    def register(self, dataset_hash: str, *, spec: dict, manifest: dict, files: dict) -> dict:
        clean = ModelArtifactStore._dataset_hash(dataset_hash)
        with self.database.connection() as conn:
            with conn.transaction():
                conn.execute(
                    """INSERT INTO model_dataset_archives
                       (dataset_hash, spec_json, manifest_json, files_json)
                       VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                    (clean, Jsonb(spec), Jsonb(manifest), Jsonb(files)),
                )
                row = dict(conn.execute(
                    "SELECT * FROM model_dataset_archives WHERE dataset_hash = %s",
                    (clean,),
                ).fetchone())
                # Concurrent publishers may upload the same bytes under different
                # bucket versions. Keep the first committed identity, never replace it.
                expected = {k: (v['sha256'], v['size_bytes']) for k, v in files.items()}
                actual = {k: (v['sha256'], v['size_bytes']) for k, v in row['files_json'].items()}
                if actual != expected or row['manifest_json'] != manifest:
                    raise ArtifactError("相同Dataset Hash已登记不同内容，拒绝覆盖归档")
                # Backfill existing job artifact indexes without changing job or
                # model identity. New jobs attach the same identities on publication.
                for name, identity in row['files_json'].items():
                    conflict = conn.execute(
                        """SELECT artifact_id FROM model_artifacts
                           WHERE dataset_hash = %s AND file_name = %s
                           AND artifact_kind IN ('dataset', 'dataset_raw', 'dataset_manifest')
                           AND (sha256 <> %s OR size_bytes <> %s) LIMIT 1""",
                        (clean, name, identity['sha256'], identity['size_bytes']),
                    ).fetchone()
                    if conflict:
                        raise ArtifactError("历史任务的数据集文件摘要不一致，拒绝登记归档")
                    conn.execute(
                        """UPDATE model_artifacts SET object_store_uri = %s,
                           object_store_version_id = %s, object_store_sha256 = %s
                           WHERE dataset_hash = %s AND file_name = %s
                           AND artifact_kind IN ('dataset', 'dataset_raw', 'dataset_manifest')""",
                        (identity['object_uri'], identity.get('version_id', ''),
                         identity['sha256'], clean, name),
                    )
        return row
