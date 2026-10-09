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
