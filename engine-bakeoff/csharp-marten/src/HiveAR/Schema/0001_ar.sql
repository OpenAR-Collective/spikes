-- The ar schema (WAX-0004), the same tables and columns as the Python side's
-- migrations, so the two engines keep identical state. Money is an integer
-- count of minor currency units, as in the prototype.

CREATE SCHEMA IF NOT EXISTS ar;

-- Every table this owner creates in ar is usable by the runtime role. No
-- DELETE: state is never deleted.
GRANT USAGE ON SCHEMA ar TO wax_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA ar GRANT SELECT, INSERT, UPDATE ON TABLES TO wax_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA ar GRANT USAGE, SELECT ON SEQUENCES TO wax_runtime;

-- Agency-level bucket registry (HIVE-0005), seeded by a debt-type module.
CREATE TABLE ar.balance_bucket_definition (
    balance_bucket_definition_pk uuid PRIMARY KEY,
    code text NOT NULL UNIQUE,
    label text NOT NULL,
    waterfall_priority integer NOT NULL CHECK (waterfall_priority >= 0),
    bears_interest boolean NOT NULL,
    origin text NOT NULL
);

CREATE TABLE ar.account (
    aggregate_sequence_number integer NOT NULL CHECK (aggregate_sequence_number >= 0),
    account_pk uuid PRIMARY KEY,
    client_account_number text NOT NULL,
    debt_type text NOT NULL,
    contract_type text NOT NULL,
    currency_code varchar(3) NOT NULL,
    status text NOT NULL,
    is_disputed boolean NOT NULL,
    list_date date NOT NULL,
    itemization_date date NOT NULL
);

-- A bucket instance: the running sum of postings to one bucket on one
-- account. The account balance is the sum of these rows.
CREATE TABLE ar.account_balance_bucket (
    account_balance_bucket_pk uuid PRIMARY KEY,
    balance_minor_units bigint NOT NULL CONSTRAINT account_balance_bucket_not_negative CHECK (balance_minor_units >= 0),
    account_fk uuid NOT NULL REFERENCES ar.account (account_pk) DEFERRABLE INITIALLY DEFERRED,
    balance_bucket_definition_fk uuid NOT NULL
        REFERENCES ar.balance_bucket_definition (balance_bucket_definition_pk) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT account_balance_bucket_unique_definition UNIQUE (account_fk, balance_bucket_definition_fk)
);
CREATE INDEX idx_account_balance_bucket_account ON ar.account_balance_bucket (account_fk);
CREATE INDEX idx_account_balance_bucket_definition ON ar.account_balance_bucket (balance_bucket_definition_fk);

-- A payment is its own aggregate. distribution records exactly where the
-- money landed so that a reversal can unwind it (HIVE-0007).
CREATE TABLE ar.payment (
    aggregate_sequence_number integer NOT NULL CHECK (aggregate_sequence_number >= 0),
    payment_pk uuid PRIMARY KEY,
    amount_minor_units bigint NOT NULL,
    currency_code varchar(3) NOT NULL,
    status text NOT NULL,
    received_at timestamptz NOT NULL,
    distribution jsonb NOT NULL,
    unapplied_minor_units bigint NOT NULL
);

-- Read models, maintained by the projections. Never written by commands.
CREATE TABLE ar.account_summary_read_model (
    account_summary_read_model_pk uuid PRIMARY KEY,
    client_account_number text NOT NULL,
    status text NOT NULL,
    is_disputed boolean NOT NULL,
    currency_code varchar(3) NOT NULL,
    balance_minor_units bigint NOT NULL,
    balance_buckets jsonb NOT NULL,
    last_payment_at timestamptz,
    last_payment_minor_units bigint,
    last_global_sequence_number bigint NOT NULL,
    contract_type text,
    debt_type text,
    list_date date
);

CREATE TABLE ar.payment_read_model (
    payment_read_model_pk uuid PRIMARY KEY,
    amount_minor_units bigint NOT NULL,
    currency_code varchar(3) NOT NULL,
    status text NOT NULL,
    received_at timestamptz NOT NULL,
    distribution jsonb NOT NULL,
    unapplied_minor_units bigint NOT NULL,
    last_global_sequence_number bigint NOT NULL
);

-- One payment as one account saw it. The Python side's table carries a _v2
-- suffix from its versioning drill; the name is kept so one harness can read both.
CREATE TABLE ar.account_payment_read_model_v2 (
    account_payment_line_pk uuid PRIMARY KEY,
    payment_pk uuid NOT NULL,
    account_pk uuid NOT NULL,
    applied_minor_units bigint NOT NULL,
    bucket_amounts jsonb NOT NULL,
    payment_amount_minor_units bigint NOT NULL,
    payment_unapplied_minor_units bigint NOT NULL,
    currency_code varchar(3) NOT NULL,
    received_at timestamptz NOT NULL,
    status text NOT NULL,
    last_global_sequence_number bigint NOT NULL,
    CONSTRAINT account_payment_line_unique_pair UNIQUE (payment_pk, account_pk)
);
CREATE INDEX idx_account_payment_line_acct ON ar.account_payment_read_model_v2 (account_pk, received_at DESC);

-- Reason codes this slice's commands use (WAX-0020).
INSERT INTO core.reference_type (code, label) VALUES ('reason_code', 'Reason code');
INSERT INTO core.reference_value (reference_type_fk, code, label)
SELECT reference_type_pk, value.code, value.label
FROM core.reference_type,
     (VALUES ('account_placed', 'Account placed'),
             ('dispute_raised', 'Dispute raised'),
             ('payment_received', 'Payment received'),
             ('payment_returned', 'Payment returned')) AS value (code, label)
WHERE core.reference_type.code = 'reason_code';
