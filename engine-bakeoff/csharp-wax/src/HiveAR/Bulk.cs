using System.Text.Json.Nodes;
using Marten;
using Npgsql;
using NpgsqlTypes;
using Wax.Commands;
using Wax.Events;
using Wax.Hashing;
using Wax.Store;

namespace HiveAR;

/// <summary>What a bulk chunk did, account by account.</summary>
public readonly record struct BulkOutcome(int Succeeded, int Rejected, int Skipped);

/// <summary>
/// The bulk workloads of WORKLOADS.md: a chunk of aggregates in one transaction,
/// written with set-based statements. Every aggregate's state row is locked and
/// its version checked, every event is hashed in the application and chained to
/// its own aggregate's previous event, events and state are written in the same
/// transaction, and every stored event is what the single-command path would
/// write: the same event types and values, from the same shaping code.
/// Foundation-written.
/// </summary>
public sealed class BulkWriter(NpgsqlDataSource dataSource, IDocumentStore store, ReasonCodes reasons)
{
    readonly ISerializer serializer = store.Advanced.Serializer;

    /// <summary>The payment file for one chunk: one payment per account, as
    /// PostPayment would post it, all in one transaction.</summary>
    public async Task<BulkOutcome> PostPaymentsAsync(
        IReadOnlyList<Guid> accountPks, Guid runPk, long amount, DateTimeOffset receivedAt, Guid actor, CancellationToken cancellation)
    {
        var keys = accountPks.Distinct().ToArray();
        var paymentKeys = keys.ToDictionary(pk => pk, pk => DerivedUuid.From($"bakeoff:payment:{runPk}:{pk}"));
        var occurredAt = IsoFormat.UtcNowMicroseconds();
        receivedAt = IsoFormat.TruncateToMicroseconds(receivedAt);
        var reason = await reasons.ResolveAsync("payment_received", cancellation);

        await using var connection = await dataSource.OpenConnectionAsync(cancellation);
        await using var transaction = await connection.BeginTransactionAsync(cancellation);

        // One round trip: lock the chunk's state rows in key order, then read
        // their buckets, their stream heads, and which payments already exist.
        var accounts = new List<AccountState>();
        Dictionary<Guid, Dictionary<string, BucketRow>> rows;
        var heads = new Dictionary<Guid, StreamHead>();
        var existing = new HashSet<Guid>();
        await using (var batch = new NpgsqlBatch(connection, transaction))
        {
            batch.BatchCommands.Add(AccountState.LockCommand(keys));
            batch.BatchCommands.Add(BucketRow.ForAccountsCommand(keys));
            batch.BatchCommands.Add(StreamHeads.Command(keys));
            batch.BatchCommands.Add(new NpgsqlBatchCommand("SELECT payment_pk FROM ar.payment WHERE payment_pk = ANY($1)")
            {
                Parameters = { AccountState.UuidArray(paymentKeys.Values.ToArray()) },
            });
            await using var reader = await batch.ExecuteReaderAsync(cancellation);
            while (await reader.ReadAsync(cancellation))
            {
                accounts.Add(AccountState.Read(reader));
            }
            await reader.NextResultAsync(cancellation);
            rows = await BucketRow.ReadAsync(reader, keys, cancellation);
            await reader.NextResultAsync(cancellation);
            while (await reader.ReadAsync(cancellation))
            {
                heads[reader.GetGuid(0)] = new StreamHead(reader.GetInt64(1), reader.IsDBNull(2) ? null : reader.GetString(2));
            }
            await reader.NextResultAsync(cancellation);
            while (await reader.ReadAsync(cancellation))
            {
                existing.Add(reader.GetGuid(0));
            }
        }

        var writes = new SetWrites();
        int succeeded = 0, rejected = 0, skipped = 0;
        foreach (var account in accounts)
        {
            var paymentPk = paymentKeys[account.Pk];
            if (existing.Contains(paymentPk))
            {
                skipped++;
                continue;
            }
            var head = heads.GetValueOrDefault(account.Pk, StreamHead.Empty);
            if (head.Version != account.AggregateSequenceNumber)
            {
                throw new WaxConcurrencyException($"account {account.Pk}: expected version {account.AggregateSequenceNumber}, found {head.Version}");
            }
            // The refusals PostPayment would make for a one-account payment.
            if (account.Status != "active" || account.CurrencyCode != "USD")
            {
                rejected++;
                continue;
            }

            var command = new PostPayment
            {
                PaymentPk = paymentPk,
                AccountPks = [account.Pk],
                AmountMinorUnits = amount,
                CurrencyCode = "USD",
                ReceivedAt = receivedAt,
            };
            var buckets = rows[account.Pk];
            var position = new AccountPosition(account.Pk, account.ListDate, account.IsDisputed, buckets.Values.Sum(row => row.Balance));
            var (shares, unapplied) = Allocation.AllocateAcrossAccounts(
                amount, [position], command.AllocationMethod, new Dictionary<Guid, long>(), new HashSet<Guid>());
            var paydown = Allocation.RunWaterfall(shares[account.Pk], buckets.Values.Select(row => (row.Code, row.Priority, row.Balance))).Paydown;
            var paydowns = new Dictionary<Guid, List<(string Code, long Amount)>>();
            if (paydown.Count > 0)
            {
                paydowns[account.Pk] = paydown;
            }

            // The payment's own event: a new aggregate, so no prior hash.
            var posted = PaymentShapes.Posted(amount, "USD", receivedAt, paydowns, unapplied, PaymentShapes.Request(command, receivedAt));
            writes.Event<PaymentPosted>(
                this, "payment", paymentPk, 0, null, prior: null, @new: posted, metadata: null, occurredAt, actor, reason);
            writes.NewStreams.Add(paymentPk);
            writes.Payments.Add((paymentPk, amount, receivedAt, Json.Distribution(paydowns).ToJsonString(), unapplied));

            if (paydown.Count > 0)
            {
                var (prior, @new, metadata, moved) = Buckets.Move(buckets, paydown, -1, paymentPk, receivedAt);
                writes.Event<AccountPaymentApplied>(
                    this, "account", account.Pk, head.Version, head.EventHash, prior, @new, metadata, occurredAt, actor, reason);
                writes.MovedStreams.Add((account.Pk, head.Version + 1));
                writes.Versions.Add((account.Pk, (int)(head.Version + 1)));
                foreach (var row in moved)
                {
                    writes.Balances.Add((row.Pk, row.Balance));
                }
            }
            succeeded++;
        }

        await writes.FlushAsync(connection, transaction, cancellation);
        await transaction.CommitAsync(cancellation);
        return new BulkOutcome(succeeded, rejected, skipped);
    }

    /// <summary>Place new accounts in one transaction, as PlaceAccount would place
    /// each, passing over any key that already exists.</summary>
    public async Task<BulkOutcome> PlaceAccountsAsync(
        IReadOnlyList<SeedAccount> accounts, Guid actor, CancellationToken cancellation)
    {
        var occurredAt = IsoFormat.UtcNowMicroseconds();
        var reason = await reasons.ResolveAsync("account_placed", cancellation);
        await using var connection = await dataSource.OpenConnectionAsync(cancellation);
        await using var transaction = await connection.BeginTransactionAsync(cancellation);

        var existing = new HashSet<Guid>();
        var definitions = new Dictionary<string, Guid>();
        await using (var batch = new NpgsqlBatch(connection, transaction))
        {
            batch.BatchCommands.Add(new NpgsqlBatchCommand("SELECT account_pk FROM ar.account WHERE account_pk = ANY($1)")
            {
                Parameters = { AccountState.UuidArray(accounts.Select(account => account.Pk).ToArray()) },
            });
            batch.BatchCommands.Add(new NpgsqlBatchCommand("SELECT code, balance_bucket_definition_pk FROM ar.balance_bucket_definition"));
            await using var reader = await batch.ExecuteReaderAsync(cancellation);
            while (await reader.ReadAsync(cancellation))
            {
                existing.Add(reader.GetGuid(0));
            }
            await reader.NextResultAsync(cancellation);
            while (await reader.ReadAsync(cancellation))
            {
                definitions[reader.GetString(0)] = reader.GetGuid(1);
            }
        }

        var writes = new SetWrites();
        int succeeded = 0, skipped = 0;
        foreach (var account in accounts)
        {
            if (existing.Contains(account.Pk))
            {
                skipped++;
                continue;
            }
            writes.Event<AccountPlaced>(
                this, "account", account.Pk, 0, null, prior: null, @new: Seeder.Placed(account), metadata: null, occurredAt, actor, reason);
            writes.NewStreams.Add(account.Pk);
            writes.NewAccounts.Add(account);
            foreach (var (code, amount) in account.Balances)
            {
                writes.NewBuckets.Add((Guid.NewGuid(), amount, account.Pk, definitions[code]));
            }
            succeeded++;
        }

        await writes.FlushAsync(connection, transaction, cancellation);
        await transaction.CommitAsync(cancellation);
        return new BulkOutcome(succeeded, 0, skipped);
    }

    /// <summary>A chunk's writes, gathered in memory and sent as set-based statements.</summary>
    sealed class SetWrites
    {
        public List<Guid> NewStreams { get; } = [];
        public List<(Guid Stream, long Version)> MovedStreams { get; } = [];
        public List<(Guid Id, Guid Stream, long Version, string Data, string Type, string DotnetType, string Headers)> Events { get; } = [];
        public List<(Guid Pk, long Amount, DateTimeOffset ReceivedAt, string Distribution, long Unapplied)> Payments { get; } = [];
        public List<(Guid Pk, long Balance)> Balances { get; } = [];
        public List<(Guid Pk, int Version)> Versions { get; } = [];
        public List<SeedAccount> NewAccounts { get; } = [];
        public List<(Guid Pk, long Balance, Guid Account, Guid Definition)> NewBuckets { get; } = [];

        /// <summary>Shape, hash, and queue one event, exactly as the single-command path records it.</summary>
        public string Event<TEvent>(
            BulkWriter writer, string aggregateType, Guid aggregatePk, long priorVersion, string? priorHash,
            JsonObject? prior, JsonObject? @new, JsonObject? metadata, DateTimeOffset occurredAt, Guid actor, Guid reason)
            where TEvent : WaxEvent, new()
        {
            var body = new TEvent
            {
                AggregateType = aggregateType,
                PriorValue = prior,
                NewValue = @new,
                OccurredAt = occurredAt,
                ActorPkRef = actor,
                ActorType = "batch",
                ReasonCodeFk = reason,
                Source = "batch",
                Metadata = metadata,
            };
            var eventType = WaxEventTypes.NameOf<TEvent>();
            var hash = EventHasher.Compute(new EventHashInput(
                aggregateType, aggregatePk, eventType, body.SchemaVersion, prior, @new, occurredAt, actor, "batch", reason, priorHash));
            var headers = new JsonObject { [StreamHeads.EventHashHeader] = hash };
            if (priorHash is not null)
            {
                headers[StreamHeads.PriorEventHashHeader] = priorHash;
            }
            Events.Add((Guid.NewGuid(), aggregatePk, priorVersion + 1, writer.serializer.ToJson(body), eventType,
                $"{typeof(TEvent).FullName}, {typeof(TEvent).Assembly.GetName().Name}", headers.ToJsonString()));
            return hash;
        }

        public async Task FlushAsync(NpgsqlConnection connection, NpgsqlTransaction transaction, CancellationToken cancellation)
        {
            await using var batch = new NpgsqlBatch(connection, transaction);
            void Add(string sql, params NpgsqlParameter[] parameters)
            {
                var command = new NpgsqlBatchCommand(sql);
                command.Parameters.AddRange(parameters);
                batch.BatchCommands.Add(command);
            }

            if (NewStreams.Count > 0)
            {
                Add("INSERT INTO core.mt_streams (id, type, version, timestamp, tenant_id) " +
                    "SELECT id, NULL, 1, now(), '*DEFAULT*' FROM unnest($1) AS s(id)",
                    Uuids(NewStreams));
            }
            if (MovedStreams.Count > 0)
            {
                Add("UPDATE core.mt_streams s SET version = v.version, timestamp = now() " +
                    "FROM unnest($1, $2) AS v(id, version) WHERE s.id = v.id",
                    Uuids(MovedStreams.Select(m => m.Stream)), Longs(MovedStreams.Select(m => m.Version)));
            }
            if (Events.Count > 0)
            {
                // Sequence numbers are taken in the order the events were recorded.
                Add("INSERT INTO core.mt_events (seq_id, id, stream_id, version, data, type, tenant_id, timestamp, mt_dotnet_type, is_archived, headers) " +
                    "SELECT nextval('core.mt_events_sequence'), e.id, e.stream_id, e.version, e.data, e.type, '*DEFAULT*', now(), e.dotnet_type, false, e.headers " +
                    "FROM (SELECT * FROM unnest($1, $2, $3, $4, $5, $6, $7) WITH ORDINALITY " +
                    "AS u(id, stream_id, version, data, type, dotnet_type, headers, n) ORDER BY n) e",
                    Uuids(Events.Select(e => e.Id)),
                    Uuids(Events.Select(e => e.Stream)),
                    Longs(Events.Select(e => e.Version)),
                    Texts(Events.Select(e => e.Data), NpgsqlDbType.Jsonb),
                    Texts(Events.Select(e => e.Type), NpgsqlDbType.Varchar),
                    Texts(Events.Select(e => e.DotnetType), NpgsqlDbType.Varchar),
                    Texts(Events.Select(e => e.Headers), NpgsqlDbType.Jsonb));
            }
            if (Payments.Count > 0)
            {
                Add("INSERT INTO ar.payment (aggregate_sequence_number, payment_pk, amount_minor_units, currency_code, status, " +
                    "received_at, distribution, unapplied_minor_units) " +
                    "SELECT 1, pk, amount, 'USD', 'posted', received_at, distribution, unapplied FROM unnest($1, $2, $3, $4, $5) " +
                    "AS p(pk, amount, received_at, distribution, unapplied)",
                    Uuids(Payments.Select(p => p.Pk)),
                    Longs(Payments.Select(p => p.Amount)),
                    new NpgsqlParameter { Value = Payments.Select(p => p.ReceivedAt).ToArray(), NpgsqlDbType = NpgsqlDbType.Array | NpgsqlDbType.TimestampTz },
                    Texts(Payments.Select(p => p.Distribution), NpgsqlDbType.Jsonb),
                    Longs(Payments.Select(p => p.Unapplied)));
            }
            if (Balances.Count > 0)
            {
                Add("UPDATE ar.account_balance_bucket b SET balance_minor_units = v.balance " +
                    "FROM unnest($1, $2) AS v(pk, balance) WHERE b.account_balance_bucket_pk = v.pk",
                    Uuids(Balances.Select(b => b.Pk)), Longs(Balances.Select(b => b.Balance)));
            }
            if (Versions.Count > 0)
            {
                Add("UPDATE ar.account a SET aggregate_sequence_number = v.version " +
                    "FROM unnest($1, $2) AS v(pk, version) WHERE a.account_pk = v.pk",
                    Uuids(Versions.Select(v => v.Pk)),
                    new NpgsqlParameter { Value = Versions.Select(v => v.Version).ToArray(), NpgsqlDbType = NpgsqlDbType.Array | NpgsqlDbType.Integer });
            }
            if (NewAccounts.Count > 0)
            {
                Add("INSERT INTO ar.account (aggregate_sequence_number, account_pk, client_account_number, debt_type, contract_type, " +
                    "currency_code, status, is_disputed, list_date, itemization_date, interest_accrued_through) " +
                    "SELECT 1, pk, client, 'personal_loan', 'third_party_contingency', 'USD', 'active', false, listed, itemized, $5 " +
                    "FROM unnest($1, $2, $3, $4) AS a(pk, client, listed, itemized)",
                    Uuids(NewAccounts.Select(a => a.Pk)),
                    Texts(NewAccounts.Select(a => a.ClientAccountNumber), NpgsqlDbType.Text),
                    Dates(NewAccounts.Select(a => a.ListDate)),
                    Dates(NewAccounts.Select(a => a.ItemizationDate)),
                    new NpgsqlParameter { Value = SeedAccount.AccruedThrough, NpgsqlDbType = NpgsqlDbType.Date });
            }
            if (NewBuckets.Count > 0)
            {
                Add("INSERT INTO ar.account_balance_bucket (account_balance_bucket_pk, balance_minor_units, account_fk, balance_bucket_definition_fk) " +
                    "SELECT * FROM unnest($1, $2, $3, $4)",
                    Uuids(NewBuckets.Select(b => b.Pk)),
                    Longs(NewBuckets.Select(b => b.Balance)),
                    Uuids(NewBuckets.Select(b => b.Account)),
                    Uuids(NewBuckets.Select(b => b.Definition)));
            }
            if (batch.BatchCommands.Count > 0)
            {
                await batch.ExecuteNonQueryAsync(cancellation);
            }
        }

        static NpgsqlParameter Uuids(IEnumerable<Guid> values) =>
            new() { Value = values.ToArray(), NpgsqlDbType = NpgsqlDbType.Array | NpgsqlDbType.Uuid };

        static NpgsqlParameter Longs(IEnumerable<long> values) =>
            new() { Value = values.ToArray(), NpgsqlDbType = NpgsqlDbType.Array | NpgsqlDbType.Bigint };

        static NpgsqlParameter Texts(IEnumerable<string> values, NpgsqlDbType type) =>
            new() { Value = values.ToArray(), NpgsqlDbType = NpgsqlDbType.Array | type };

        static NpgsqlParameter Dates(IEnumerable<DateOnly> values) =>
            new() { Value = values.ToArray(), NpgsqlDbType = NpgsqlDbType.Array | NpgsqlDbType.Date };
    }
}
