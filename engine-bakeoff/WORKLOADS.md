# Workloads

This is the specification both sides implement, so that they do the same work and a result means the same thing on each. The [README](README.md) says why the bake-off exists and how runs are judged; this file says exactly what runs. Where this file and a side's code disagree, this file is right and the code is a bug.

Code written for these workloads beyond Andy Troiano's prototype, on either side, is Foundation-written. Each side's README marks which parts those are.

## Deterministic keys

Every key the workloads need is derived, so both sides seed the same accounts and a retried command reuses its key. A derived UUID is the first 16 bytes of the SHA-256 of a UTF-8 name, with the version set to 4 and the variant set to RFC 4122:

```
derived_uuid(name) = UUID(bytes = sha256(name)[0:16], version = 4)
```

In Python that is `uuid.UUID(bytes=hashlib.sha256(name.encode()).digest()[:16], version=4)`. In C# it is the same bytes with byte 6 set to `(b & 0x0F) | 0x40` and byte 8 set to `(b & 0x3F) | 0x80`, read big-endian. The vectors in [shared/derived-uuids.json](shared/derived-uuids.json) pin it.

## Schema additions

Both sides add the same columns and tables to what the prototype had:

- `ar.account.interest_accrued_through` (date, not null): the last day interest has been accrued for. PlaceAccount takes it as an optional field that defaults to the itemization date, and the `account.placed` event's `new_value` carries it.
- `ar.account.queue_code` (text, null): the work queue the account is assigned to.
- `ar.task`: `task_pk` (uuid key), `account_fk` (uuid), `task_type` (text), `status` (text), `created_at` (timestamptz).
- `core.pending_event`, the Pending Event Register of WAX-0027: `pending_event_pk` (uuid key), `aggregate_pk_ref` (uuid), `aggregate_type` (text), `target_event_type` (text), `due_at` (timestamptz), `status` (`pending`, `fired`, or `cancelled`), `scheduled_by_event` (the key of the event whose rule scheduled it), `fired_at` (timestamptz, null). Index on `(status, due_at)`.
- `core.batch_job_run` and `core.batch_job_chunk`, as WAX-0024 describes them. A run has a key, a job name, and created and finished times. A chunk has a key, its run, a `chunk_sequence_number`, `range_start` and `range_end` (inclusive account keys), a `status` (`pending`, `claimed`, `complete`, or `failed`), `claimed_by_instance`, `claim_expires_at`, `completed_at`, and `error_detail`.
- Reason codes: `interest_accrued`, `paid_in_full`, `pending_event_scheduled`, `pending_event_fired`, `task_created`, and `queue_assigned`, alongside the prototype's four.

## New commands

Each command keeps every invariant in the README: the row lock, the version check, the hash in the same transaction, and the event and state written together. Each is idempotent: a retry of one that took effect is answered as success and writes nothing.

| Command | Fields | What it does |
| --- | --- | --- |
| AccrueInterest | `account_pk`, `accrual_date`, `annual_rate_basis_points` | If the account is not active, or `accrual_date` is not after `interest_accrued_through`, nothing happens. Otherwise `days` is the difference, `amount = principal * basis_points * days // (10000 * 365)` in whole minor units, the interest bucket grows by `amount`, `interest_accrued_through` becomes `accrual_date`, and the event is `account.interest_accrued`, with the interest bucket and the date in `prior_value` and `new_value`. An amount of zero still advances the date and writes the event. |
| ChangeAccountStatus | `account_pk`, `status`, `reason_code` | Sets the status. Already at that status: answered as success, nothing written. Event: `account.status_changed`. |
| ScheduleFutureEvent | `pending_event_pk`, `account_pk`, `target_event_type`, `due_at`, `scheduled_by_event` | Writes `pending_event.scheduled` on the account, with the target, the due time, and the scheduling event in `new_value`, and inserts the register row in the same transaction. |
| FirePendingEvent | `pending_event_pk` | Locks the register row and the account. If the row is still pending, writes the target event type on the account, with the register key in `metadata`, and marks the row fired with `fired_at`. Otherwise, nothing. |
| CreateTask | `task_pk`, `account_pk`, `task_type` | Inserts the task row and writes `task.created` on the account. |
| AssignToQueue | `account_pk`, `queue_code` | Sets the queue. Already there: answered as success. Event: `account.queue_assigned`. |

## The seed

The seed is the starting database for every run. It is loaded by each side's own bulk loader, in that side's event store format, and is not measured. The loader writes, for each account, exactly what PlaceAccount would have written: the state row, three bucket rows, one `account.placed` event with a valid hash, and the account summary read model row. It then sets every projection's and the rule runner's position to the head of the stream, so nothing reprocesses the seed. Every run starts from a fresh copy of the seed, which the harness makes cheaply by creating the run's database from the seeded one as a template.

Account `i`, for `i` from 0 to N minus 1, is:

| Field | Value |
| --- | --- |
| `account_pk` | `derived_uuid("bakeoff:account:{i}")` |
| `client_account_number` | `BK-` and `i` as nine digits, for example `BK-000000042` |
| `debt_type`, `contract_type`, `currency_code` | `personal_loan`, `third_party_contingency`, `USD` |
| `list_date` | 2020-01-01 plus `i % 2190` days |
| `itemization_date` | `list_date` minus 30 days |
| `interest_accrued_through` | 2026-09-30 |
| principal | `50000 + (i * 7919) % 950000` |
| interest | `100 + (i * 104729) % 20000` |
| fee | `500 + (i % 3) * 1000` |

The seed event's `occurred_at` is 2026-09-30T00:00:00+00:00, its actor is `derived_uuid("bakeoff:seeder")` with actor type `batch`, its source is `seed`, and its reason is `account_placed`. The first measured size is N = 500,000.

## Batch workloads

A batch runs as WAX-0024 describes. A coordinator creates the run and divides the accounts, in key order, into chunks of 1,000. Each worker then loops: it claims the next pending chunk with `SELECT ... FOR UPDATE SKIP LOCKED` in a short transaction of its own, runs one command per account in the chunk through the normal command path, each in its own transaction, and marks the chunk complete. The batch ends when every chunk is complete.

**Payment file.** One PostPayment per account: amount 2,500, currency USD, `oldest_first`, `received_at` 2026-10-01T06:00:00+00:00, and key `derived_uuid("bakeoff:payment:{run_pk}:{account_pk}")`.

**Interest accrual.** One AccrueInterest per account: `accrual_date` 2026-10-01 and 1,800 basis points.

## Real-time workload

Many workers at once send inbound commands, back to back, for a fixed time after a warm-up, with each worker's choices drawn from its own seeded random sequence. Each inbound command is one of these:

- **85 percent** are a PostPayment against one account chosen uniformly from the seed, for a uniform amount from 500 to 5,000. One in 30 of these is a payoff of 2,000,000, which pays the account to zero and leaves the rest unapplied.
- **10 percent** are an OpenDispute against one account chosen the same way.
- **5 percent** are a PlaceAccount for a new account, keyed `derived_uuid("bakeoff:live-account:{run_pk}:{worker}:{n}")`, with the seed's balances for `i = n`.

A refusal, such as a payment to an account that is already paid in full or a dispute on a disputed account, is a normal outcome. It is counted as rejected, not as an error.

### Rules

Rules are the "when X happens, do Y" post-event workflows of WAX-0023. They run after the triggering write has committed, from the event stream, in a rule runner that delivers each event at least once and each aggregate's events in order. Each rule's action is a command, keyed so that a second delivery is answered as success. One simulated day is `seconds_per_day` real seconds, 2 by default.

| When | And | Do |
| --- | --- | --- |
| `account.payment_applied` | the account's total balance, read from current state, is zero | ChangeAccountStatus to `paid_in_full`, reason `paid_in_full` |
| `dispute.opened` | | ScheduleFutureEvent `account.dispute_review_due`, due 30 simulated days after the event's `occurred_at`, keyed `derived_uuid("pending:" + triggering event key)` |
| `account.dispute_review_due` | | AssignToQueue `dispute_review` |
| `account.placed` | | ScheduleFutureEvent `account.first_contact_due`, due 1 simulated day after `occurred_at`, keyed the same way |
| `account.first_contact_due` | | CreateTask `first_contact`, keyed `derived_uuid("task:" + pending_event_pk)` |

The register's poller claims due entries with `SELECT ... FOR UPDATE SKIP LOCKED` on a polling interval and sends a FirePendingEvent for each, as WAX-0027 describes.

## What each side provides

Each side's runtime image answers these commands, which the harness calls. Each prints one line of JSON when it ends.

| Command | What it does |
| --- | --- |
| `seed --accounts N` | Loads the seed. Not measured. |
| `bench batch-payments --workers W` | Runs the payment file to the end. |
| `bench batch-accrual --workers W` | Runs the interest accrual to the end. |
| `bench realtime --workers W --warmup S --duration S --seconds-per-day D --random-seed R` | Runs the inbound mix. |
| `run-engine` | The long-running services: the projection worker, the rule runner, and the register poller. It runs in its own container for the whole run. |
| `verify-chain` | Recomputes every chain. |

The JSON line reports the workload, the worker count, the commands sent, how many succeeded, were rejected, failed, or were retried after a conflict, the elapsed seconds, and the command latency at the 50th, 95th, and 99th percentiles and the maximum. Everything else is measured by the harness from outside: CPU from each container's cgroup counters, statements and commits from PostgreSQL's statistics, read-model lag from the projection positions, rule latency as the time between a triggering event's recorded time and its action's recorded time, and register lateness as `fired_at` minus `due_at`.

## Correctness gates

A run counts only if all of these hold once the engine has caught up:

- **Batch payment file:** exactly one payment per account exists for the run, and the total of all balances fell by exactly 2,500 times N.
- **Interest accrual:** for every account, the interest bucket grew by exactly `principal * 1800 // 3650000`, and `interest_accrued_through` is 2026-10-01.
- **Real time:** for every account, its opening balance minus its current balance equals the amount applied to it across all payment rows. Every register entry due more than five seconds before the end has fired. Every `account.first_contact_due` event has exactly one task.
- **Every run:** every hash chain verifies, and every read model agrees with the authoritative state.
