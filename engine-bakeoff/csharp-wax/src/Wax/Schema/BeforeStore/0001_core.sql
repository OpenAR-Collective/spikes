-- The core schema's own tables: reference values, which hold the reason code
-- registry (WAX-0020). Marten creates the event store tables in the same
-- schema next.

-- The application connects as a member of wax_runtime, never as the owner
-- that runs migrations. The reference deployment creates the role before
-- the first migration; this covers every other path.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'wax_runtime') THEN
        CREATE ROLE wax_runtime NOLOGIN;
    END IF;
END $$;

CREATE TABLE core.reference_type (
    reference_type_pk uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code text NOT NULL UNIQUE,
    label text NOT NULL
);

CREATE TABLE core.reference_value (
    reference_value_pk uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reference_type_fk uuid NOT NULL REFERENCES core.reference_type (reference_type_pk),
    code text NOT NULL,
    label text NOT NULL,
    UNIQUE (reference_type_fk, code)
);
