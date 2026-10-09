using Wax.Events;

namespace HiveAR;

// The event types this slice writes, under the same event_type names as the
// Python side. Each carries the enriched body of WAX-0001; what changed is in
// its prior_value and new_value, keyed JSON objects.

[WaxEventType("account.placed")]
public sealed record AccountPlaced : WaxEvent;

[WaxEventType("dispute.opened")]
public sealed record DisputeOpened : WaxEvent;

[WaxEventType("account.payment_applied")]
public sealed record AccountPaymentApplied : WaxEvent;

[WaxEventType("account.payment_reversed")]
public sealed record AccountPaymentReversed : WaxEvent;

[WaxEventType("payment.posted")]
public sealed record PaymentPosted : WaxEvent;

[WaxEventType("payment.reversed")]
public sealed record PaymentReversed : WaxEvent;

// Added for the bake-off's workloads (WORKLOADS.md). Foundation-written.

[WaxEventType("account.interest_accrued")]
public sealed record AccountInterestAccrued : WaxEvent;

[WaxEventType("account.status_changed")]
public sealed record AccountStatusChanged : WaxEvent;

[WaxEventType("account.queue_assigned")]
public sealed record AccountQueueAssigned : WaxEvent;

[WaxEventType("task.created")]
public sealed record TaskCreated : WaxEvent;

[WaxEventType("pending_event.scheduled")]
public sealed record PendingEventScheduled : WaxEvent;

[WaxEventType("account.dispute_review_due")]
public sealed record AccountDisputeReviewDue : WaxEvent;

[WaxEventType("account.first_contact_due")]
public sealed record AccountFirstContactDue : WaxEvent;
