-- Fixes the artifacts unique constraint so a raw CWI and its refined
-- (Discovery Agent) version can coexist as separate rows for the
-- same (source_id, source_ref) — distinguished by artifact_type.
--
-- Run this manually against the existing database (schema.sql only
-- auto-runs on a fresh volume, not on an already-initialized one):
--   docker exec -i adlc-postgres psql -U <POSTGRES_USER> -d <POSTGRES_DB> < 002_artifact_type_unique.sql

ALTER TABLE artifacts DROP CONSTRAINT IF EXISTS artifacts_source_id_source_ref_key;
ALTER TABLE artifacts ADD CONSTRAINT artifacts_source_id_source_ref_type_key
    UNIQUE (source_id, source_ref, artifact_type);