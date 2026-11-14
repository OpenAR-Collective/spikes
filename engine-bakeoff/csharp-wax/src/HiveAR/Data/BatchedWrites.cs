using Npgsql;
using NpgsqlTypes;
using Wax.Commands;

namespace HiveAR.Data;

/// <summary>
/// The batched state mode's writes for one command. Account and bucket rows
/// the command changes are collected as it runs, the last value of each row
/// winning, and go out as one set-based UPDATE per table, after the command's
/// other writes and in the same batch. The values written are the values
/// plain SQL mode writes one row per statement, so the stored state is the
/// same. In the fair batched mode the updates are sized to what changed, as
/// the Python direct side sizes them: an account whose only change is its
/// version gets the version-only update, one row as a single-row statement and
/// several as one two-array UPDATE; an account with any other change gets plain
/// SQL mode's single-row statement; and a single bucket row gets a single-row
/// statement. Foundation-written.
/// </summary>
sealed class BatchedWrites(bool fair)
{
    const string AccountsSql =
        "UPDATE ar.account a SET aggregate_sequence_number = v.version, status = v.status, is_disputed = v.disputed, " +
        "interest_accrued_through = v.accrued_through, queue_code = v.queue_code " +
        "FROM unnest($1::uuid[], $2::integer[], $3::text[], $4::boolean[], $5::date[], $6::text[]) " +
        "AS v(pk, version, status, disputed, accrued_through, queue_code) WHERE a.account_pk = v.pk";

    const string BucketsSql =
        "UPDATE ar.account_balance_bucket b SET balance_minor_units = v.balance " +
        "FROM unnest($1::uuid[], $2::bigint[]) AS v(pk, balance) WHERE b.account_balance_bucket_pk = v.pk";

    const string VersionsSql =
        "UPDATE ar.account a SET aggregate_sequence_number = v.version " +
        "FROM unnest($1::uuid[], $2::integer[]) AS v(pk, version) WHERE a.account_pk = v.pk";

    const string BucketSql = "UPDATE ar.account_balance_bucket SET balance_minor_units = ? WHERE account_balance_bucket_pk = ?";

    readonly Dictionary<Guid, AccountState> accounts = [];
    readonly Dictionary<Guid, long> buckets = [];

    /// <summary>The command's collector in either batched mode, or null in any other mode.</summary>
    public static BatchedWrites? For(CommandContext context)
    {
        var access = context.Service<ArData>()?.Access;
        if (access is not (StateAccess.Batched or StateAccess.FairBatched))
        {
            return null;
        }
        return context.Scoped(() =>
        {
            var writes = new BatchedWrites(access == StateAccess.FairBatched);
            context.BeforeFlush(() => writes.Queue(context));
            return writes;
        });
    }

    public void Account(AccountState account) => accounts[account.Pk] = account;

    public void Bucket(Guid bucketPk, long balance) => buckets[bucketPk] = balance;

    void Queue(CommandContext context)
    {
        if (fair)
        {
            QueueFair(context);
            return;
        }
        if (accounts.Count > 0)
        {
            var rows = accounts.Values.ToArray();
            context.QueueCommand(new NpgsqlBatchCommand(AccountsSql)
            {
                Parameters =
                {
                    Array(rows.Select(a => a.Pk).ToArray(), NpgsqlDbType.Uuid),
                    Array(rows.Select(a => a.AggregateSequenceNumber).ToArray(), NpgsqlDbType.Integer),
                    Array(rows.Select(a => a.Status).ToArray(), NpgsqlDbType.Text),
                    Array(rows.Select(a => a.IsDisputed).ToArray(), NpgsqlDbType.Boolean),
                    Array(rows.Select(a => a.InterestAccruedThrough).ToArray(), NpgsqlDbType.Date),
                    Array(rows.Select(a => a.QueueCode).ToArray(), NpgsqlDbType.Text),
                },
            });
        }
        if (buckets.Count > 0)
        {
            context.QueueCommand(new NpgsqlBatchCommand(BucketsSql)
            {
                Parameters =
                {
                    Array(buckets.Keys.ToArray(), NpgsqlDbType.Uuid),
                    Array(buckets.Values.ToArray(), NpgsqlDbType.Bigint),
                },
            });
        }
    }

    void QueueFair(CommandContext context)
    {
        List<AccountState>? versionOnly = null;
        foreach (var account in accounts.Values)
        {
            if (account.OnlyVersionChanged)
            {
                (versionOnly ??= []).Add(account);
            }
            else
            {
                account.QueueUpdateSql(context);
            }
        }
        if (versionOnly is [var single])
        {
            single.QueueVersionUpdateSql(context);
        }
        else if (versionOnly is { Count: > 1 })
        {
            context.QueueCommand(new NpgsqlBatchCommand(VersionsSql)
            {
                Parameters =
                {
                    Array(versionOnly.Select(a => a.Pk).ToArray(), NpgsqlDbType.Uuid),
                    Array(versionOnly.Select(a => a.AggregateSequenceNumber).ToArray(), NpgsqlDbType.Integer),
                },
            });
        }
        if (buckets.Count == 1)
        {
            var (pk, balance) = buckets.First();
            context.QueueSql(BucketSql, balance, pk);
        }
        else if (buckets.Count > 1)
        {
            context.QueueCommand(new NpgsqlBatchCommand(BucketsSql)
            {
                Parameters =
                {
                    Array(buckets.Keys.ToArray(), NpgsqlDbType.Uuid),
                    Array(buckets.Values.ToArray(), NpgsqlDbType.Bigint),
                },
            });
        }
    }

    static NpgsqlParameter Array<T>(T[] values, NpgsqlDbType type) =>
        new() { Value = values, NpgsqlDbType = NpgsqlDbType.Array | type };
}
