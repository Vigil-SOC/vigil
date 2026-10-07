-- The index the case_findings association table declares on finding_id.
--
-- The table's primary key is (case_id, finding_id), so it cannot serve a
-- lookup by finding: Finding.cases, loaded with every Finding, the ON DELETE
-- CASCADE from findings, and any "which cases hold this finding" query all
-- scan the table without it.
--
-- case_findings is created by SQLAlchemy rather than declared here, and
-- create_all adds no index to a table that already exists. On a first install
-- the table is not there yet and this statement fails harmlessly; create_all
-- then builds the table with the index. scripts/migrate_schema.py covers a
-- database this file never reached, or one where this file's role does not own
-- the table.
CREATE INDEX IF NOT EXISTS idx_case_findings_finding
    ON case_findings (finding_id, case_id);
