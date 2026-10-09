-- What the bake-off's workloads add to the prototype's schema (WORKLOADS.md).
-- Foundation-written.

ALTER TABLE ar.account ADD COLUMN interest_accrued_through date;
UPDATE ar.account SET interest_accrued_through = itemization_date;
ALTER TABLE ar.account ALTER COLUMN interest_accrued_through SET NOT NULL;
ALTER TABLE ar.account ADD COLUMN queue_code text;

CREATE TABLE ar.task (
    task_pk uuid PRIMARY KEY,
    account_fk uuid NOT NULL REFERENCES ar.account (account_pk) DEFERRABLE INITIALLY DEFERRED,
    task_type text NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE INDEX idx_task_account ON ar.task (account_fk);

INSERT INTO core.reference_value (reference_type_fk, code, label)
SELECT reference_type_pk, value.code, value.label
FROM core.reference_type,
     (VALUES ('interest_accrued', 'Interest accrued'),
             ('paid_in_full', 'Paid in full'),
             ('pending_event_scheduled', 'Future event scheduled'),
             ('pending_event_fired', 'Scheduled event fired'),
             ('task_created', 'Task created'),
             ('queue_assigned', 'Assigned to a queue')) AS value (code, label)
WHERE core.reference_type.code = 'reason_code';
