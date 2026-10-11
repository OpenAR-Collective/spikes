using Microsoft.EntityFrameworkCore;
using Wax.Commands;

namespace HiveAR.Data;

/// <summary>A bucket, tracked so that its new balance is saved, with its
/// definition's code and priority read in the same query but not tracked.</summary>
sealed class BucketLoad
{
    public required BucketRow Bucket { get; init; }
    public required string Code { get; init; }
    public required int Priority { get; init; }
}

/// <summary>The two columns an existence check compares.</summary>
sealed class KeyAndText
{
    public Guid Key { get; init; }
    public string Text { get; init; } = "";
}

/// <summary>
/// The handlers' reads in EF Core mode. Each lock is an EF query rooted in raw
/// SQL that ends in FOR UPDATE, because LINQ has no locking operator; it takes
/// the row locks in key order before any rule runs (WAX-0013) and returns
/// tracked entities. The stream heads are the event store's, read by Wax with
/// plain SQL, so each lock costs a round trip of its own that the plain SQL
/// mode folds into one.
///
/// Every query here but the three locks is compiled once per process with
/// EF.CompileAsyncQuery, so a command skips EF's per-call query translation
/// and cache lookup. Reads of rows the command never writes are not tracked.
/// Foundation-written.
/// </summary>
static class EfState
{
    // The three locking queries cannot be compiled: EF Core does not compile a
    // query rooted in raw SQL. They run as ordinary queries, which EF still
    // translates once and keeps in its query cache.
    static IAsyncEnumerable<AccountState> LockAccounts(ArDbContext db, Guid[] keys) =>
        db.Accounts.FromSql($"SELECT * FROM ar.account WHERE account_pk = ANY({keys}) ORDER BY account_pk FOR UPDATE").AsAsyncEnumerable();

    // A second statement, never an Include on the locking query. Under READ
    // COMMITTED a statement that waits for a row lock returns the locked row's
    // latest version but reads every joined row from the snapshot it took
    // before the wait, so an Include on the lock reads stale buckets and loses
    // a concurrent payment; the concurrency tests caught exactly that. A new
    // statement takes a new snapshot after the lock is granted, as the plain
    // SQL mode's separate bucket read does. The definition's columns come by a
    // join in the projection, so definitions are never tracked.
    static readonly Func<ArDbContext, List<Guid>, IAsyncEnumerable<BucketLoad>> BucketsFor =
        EF.CompileAsyncQuery((ArDbContext db, List<Guid> keys) =>
            db.Buckets.Where(bucket => keys.Contains(bucket.AccountFk))
                .Select(bucket => new BucketLoad { Bucket = bucket, Code = bucket.Definition!.Code, Priority = bucket.Definition.Priority }));

    static IAsyncEnumerable<PaymentState> LockPayment(ArDbContext db, Guid key) =>
        db.Payments.FromSql($"SELECT * FROM ar.payment WHERE payment_pk = {key} FOR UPDATE").AsAsyncEnumerable();

    static IAsyncEnumerable<PendingEventRow> ClaimPending(ArDbContext db, Guid key) =>
        db.PendingEvents.FromSql($"SELECT * FROM core.pending_event WHERE pending_event_pk = {key} FOR UPDATE SKIP LOCKED").AsAsyncEnumerable();

    static readonly Func<ArDbContext, Guid, Task<bool>> PaymentExists =
        EF.CompileAsyncQuery((ArDbContext db, Guid key) => db.Payments.Any(payment => payment.Pk == key));

    public static readonly Func<ArDbContext, Guid, Task<bool>> AccountExists =
        EF.CompileAsyncQuery((ArDbContext db, Guid key) => db.Accounts.Any(account => account.Pk == key));

    public static readonly Func<ArDbContext, List<string>, IAsyncEnumerable<BucketDefinition>> DefinitionsByCode =
        EF.CompileAsyncQuery((ArDbContext db, List<string> codes) =>
            db.BucketDefinitions.AsNoTracking().Where(definition => codes.Contains(definition.Code)));

    public static readonly Func<ArDbContext, string, Task<Guid>> DefinitionPk =
        EF.CompileAsyncQuery((ArDbContext db, string code) =>
            db.BucketDefinitions.Where(definition => definition.Code == code).Select(definition => definition.Pk).Single());

    public static readonly Func<ArDbContext, Guid, Task<KeyAndText?>> TaskByPk =
        EF.CompileAsyncQuery((ArDbContext db, Guid key) =>
            db.Tasks.Where(task => task.Pk == key).Select(task => new KeyAndText { Key = task.AccountFk, Text = task.TaskType }).SingleOrDefault());

    public static readonly Func<ArDbContext, Guid, Task<KeyAndText?>> PendingByPk =
        EF.CompileAsyncQuery((ArDbContext db, Guid key) =>
            db.PendingEvents.Where(entry => entry.Pk == key)
                .Select(entry => new KeyAndText { Key = entry.AggregatePkRef, Text = entry.TargetEventType }).SingleOrDefault());

    public static async Task<List<T>> ListAsync<T>(IAsyncEnumerable<T> source, CancellationToken cancellation)
    {
        var list = new List<T>();
        await foreach (var item in source.WithCancellation(cancellation))
        {
            list.Add(item);
        }
        return list;
    }

    public static async Task<(List<AccountState> Accounts, Dictionary<Guid, Dictionary<string, BucketRow>> Rows)> LockAccountsAsync(
        CommandContext context, ArDbContext db, Guid[] accountPks, bool withBuckets, CancellationToken cancellation)
    {
        var wanted = accountPks.Distinct().ToArray();
        var accounts = await ListAsync(LockAccounts(db, wanted), cancellation);
        var rows = new Dictionary<Guid, Dictionary<string, BucketRow>>();
        if (withBuckets)
        {
            rows = wanted.ToDictionary(pk => pk, _ => new Dictionary<string, BucketRow>());
            foreach (var load in await ListAsync(BucketsFor(db, wanted.ToList()), cancellation))
            {
                load.Bucket.Code = load.Code;
                load.Bucket.Priority = load.Priority;
                rows[load.Bucket.AccountFk][load.Code] = load.Bucket;
            }
        }
        await context.LoadStreamHeadsAsync(wanted, cancellation);
        return (accounts, rows);
    }

    public static async Task<(bool PaymentExists, List<AccountState> Accounts, Dictionary<Guid, Dictionary<string, BucketRow>> Rows)> LockForPaymentAsync(
        CommandContext context, ArDbContext db, Guid paymentPk, Guid[] accountPks, CancellationToken cancellation)
    {
        var exists = await PaymentExists(db, paymentPk);
        var (accounts, rows) = await LockAccountsAsync(context, db, accountPks, withBuckets: true, cancellation);
        return (exists, accounts, rows);
    }

    public static async Task<PaymentState?> LockPaymentAsync(CommandContext context, ArDbContext db, Guid paymentPk, CancellationToken cancellation)
    {
        var payment = (await ListAsync(LockPayment(db, paymentPk), cancellation)).SingleOrDefault();
        await context.LoadStreamHeadsAsync([paymentPk], cancellation);
        return payment;
    }

    /// <summary>The entry, claimed so that two pollers never fire it twice, or null.</summary>
    public static async Task<PendingEventRow?> ClaimPendingAsync(ArDbContext db, Guid pendingEventPk, CancellationToken cancellation) =>
        (await ListAsync(ClaimPending(db, pendingEventPk), cancellation)).SingleOrDefault();
}
