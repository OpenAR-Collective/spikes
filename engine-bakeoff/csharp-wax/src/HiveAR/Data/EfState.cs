using Microsoft.EntityFrameworkCore;
using Wax.Commands;

namespace HiveAR.Data;

/// <summary>
/// The handlers' reads in EF Core mode. Each lock is an EF query rooted in raw
/// SQL that ends in FOR UPDATE, because LINQ has no locking operator; it takes
/// the row locks in key order before any rule runs (WAX-0013) and returns
/// tracked entities. Buckets come with their accounts through Include. The
/// stream heads are the event store's, read by Wax with plain SQL, so each
/// lock costs a second round trip that the plain SQL mode folds into one.
/// Foundation-written.
/// </summary>
static class EfState
{
    public static async Task<(List<AccountState> Accounts, Dictionary<Guid, Dictionary<string, BucketRow>> Rows)> LockAccountsAsync(
        CommandContext context, ArDbContext db, Guid[] accountPks, bool withBuckets, CancellationToken cancellation)
    {
        var wanted = accountPks.Distinct().ToArray();
        var accounts = await db.Accounts.FromSql(
            $"SELECT * FROM ar.account WHERE account_pk = ANY({wanted}) ORDER BY account_pk FOR UPDATE").ToListAsync(cancellation);
        if (withBuckets)
        {
            // A second statement, never an Include on the locking query. Under
            // READ COMMITTED a statement that waits for a row lock returns the
            // locked row's latest version but reads every joined row from the
            // snapshot it took before the wait, so an Include here reads stale
            // buckets and loses a concurrent payment; the concurrency tests
            // caught exactly that. A new statement takes a new snapshot after
            // the lock is granted, as the plain SQL mode's separate bucket read
            // does. EF fixes the buckets up onto the tracked accounts.
            var keys = wanted.ToList();
            await db.Buckets.Include(bucket => bucket.Definition).Where(bucket => keys.Contains(bucket.AccountFk)).LoadAsync(cancellation);
        }
        await context.LoadStreamHeadsAsync(wanted, cancellation);
        return (accounts, withBuckets ? Rows(accounts, wanted) : []);
    }

    /// <summary>Bucket rows by account, then by bucket code, as the plain SQL mode builds them.</summary>
    static Dictionary<Guid, Dictionary<string, BucketRow>> Rows(List<AccountState> accounts, IEnumerable<Guid> wanted)
    {
        var rows = wanted.ToDictionary(pk => pk, _ => new Dictionary<string, BucketRow>());
        foreach (var account in accounts)
        {
            foreach (var bucket in account.Buckets)
            {
                bucket.Code = bucket.Definition!.Code;
                bucket.Priority = bucket.Definition.Priority;
                rows[account.Pk][bucket.Code] = bucket;
            }
        }
        return rows;
    }

    public static async Task<(bool PaymentExists, List<AccountState> Accounts, Dictionary<Guid, Dictionary<string, BucketRow>> Rows)> LockForPaymentAsync(
        CommandContext context, ArDbContext db, Guid paymentPk, Guid[] accountPks, CancellationToken cancellation)
    {
        var exists = await db.Payments.AnyAsync(payment => payment.Pk == paymentPk, cancellation);
        var (accounts, rows) = await LockAccountsAsync(context, db, accountPks, withBuckets: true, cancellation);
        return (exists, accounts, rows);
    }

    public static async Task<PaymentState?> LockPaymentAsync(CommandContext context, ArDbContext db, Guid paymentPk, CancellationToken cancellation)
    {
        var payment = (await db.Payments.FromSql(
            $"SELECT * FROM ar.payment WHERE payment_pk = {paymentPk} FOR UPDATE").ToListAsync(cancellation)).SingleOrDefault();
        await context.LoadStreamHeadsAsync([paymentPk], cancellation);
        return payment;
    }

    /// <summary>The entry, claimed so that two pollers never fire it twice, or null.</summary>
    public static async Task<PendingEventRow?> ClaimPendingAsync(ArDbContext db, Guid pendingEventPk, CancellationToken cancellation) =>
        (await db.PendingEvents.FromSql(
            $"SELECT * FROM core.pending_event WHERE pending_event_pk = {pendingEventPk} FOR UPDATE SKIP LOCKED").ToListAsync(cancellation)).SingleOrDefault();
}
