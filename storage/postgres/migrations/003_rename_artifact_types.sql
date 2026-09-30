-- Renames existing artifact_type values to match the corrected
-- architecture: raw connector data is "source_artifact", and the
-- true CWI only exists after Discovery Agent refinement.
--
-- IMPORTANT: order matters. Step 1 frees up "candidate_work_item"
-- before step 2 claims it — running these out of order will corrupt
-- data (both would end up merged onto one label).
--
-- Run: docker exec -i adlc-postgres psql -U <user> -d <db> < storage/postgres/migrations/003_rename_artifact_types.sql

-- Step 1: old raw type → source_artifact
UPDATE artifacts SET artifact_type = 'source_artifact'
WHERE artifact_type = 'candidate_work_item';

-- Step 2: old refined type → candidate_work_item (the real CWI now)
UPDATE artifacts SET artifact_type = 'candidate_work_item'
WHERE artifact_type = 'refined_candidate_work_item';