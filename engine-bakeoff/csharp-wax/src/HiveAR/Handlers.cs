using System.Text.Json.Nodes;
using Npgsql;
using Wax.Commands;
using Wax.Hashing;

namespace HiveAR;

// The handlers, ported rule for rule from the prototype's hivear/handlers.py:
// the same checks in the same order, the same refusal codes, and the same
// event contents. Reads that the Python side makes one at a time are sent here
// as one batch per round trip, and the writes go out in one batch at save.

public sealed class PlaceAccountHandler : ICommandHandler<PlaceAccount>
{
    /// <summary>HIVE-0005: an account needs an itemization date and a balance
    /// made of registered buckets.</summary>
    public async Task HandleAsync(PlaceAccount command, CommandContext context, CancellationToken cancellation)
    {
        var placed = new JsonObject
        {
            ["client_account_number"] = command.ClientAccountNumber,
            ["debt_type"] = command.DebtType,
            ["contract_type"] = command.ContractType,
            ["currency_code"] = command.CurrencyCode,
            ["status"] = "active",
            ["is_disputed"] = false,
            ["list_date"] = IsoFormat.Date(command.ListDate),
            ["itemization_date"] = IsoFormat.Date(command.ItemizationDate),
            ["balance_buckets"] = Json.Amounts(command.OpeningBalances),
            ["interest_accrued_through"] = IsoFormat.Date(command.InterestAccruedThrough ?? command.ItemizationDate),
        };

        bool exists;
        var definitions = new Dictionary<string, Guid>();
        await using (var batch = context.CreateBatch())
        {
            batch.BatchCommands.Add(new NpgsqlBatchCommand("SELECT EXISTS (SELECT 1 FROM ar.account WHERE account_pk = $1)")
            {
                Parameters = { new NpgsqlParameter { Value = command.AccountPk } },
            });
            batch.BatchCommands.Add(new NpgsqlBatchCommand(
                "SELECT code, balance_bucket_definition_pk FROM ar.balance_bucket_definition WHERE code = ANY($1)")
            {
                Parameters = { new NpgsqlParameter { Value = command.OpeningBalances.Keys.ToArray() } },
            });
            await using var reader = await batch.ExecuteReaderAsync(cancellation);
            await reader.ReadAsync(cancellation);
            exists = reader.GetBoolean(0);
            await reader.NextResultAsync(cancellation);
            while (await reader.ReadAsync(cancellation))
            {
                definitions[reader.GetString(0)] = reader.GetGuid(1);
            }
        }

        if (exists)
        {
            var (eventType, newValue) = await Streams.FirstEventAsync(context, command.AccountPk, cancellation);
            if (eventType == "account.placed" && JsonNode.DeepEquals(newValue, placed))
            {
                throw new AlreadyAppliedException();
            }
            throw new CommandRejectedException("A different account already exists under this key", "key_reused");
        }
        var unknown = command.OpeningBalances.Keys.Where(code => !definitions.ContainsKey(code)).Order(StringComparer.Ordinal).ToList();
        if (unknown.Count > 0)
        {
            throw new CommandRejectedException($"Unregistered balance buckets: {string.Join(", ", unknown)}", "bucket_not_registered");
        }

        var account = new AccountState
        {
            Pk = command.AccountPk,
            IsNew = true,
            ClientAccountNumber = command.ClientAccountNumber,
            DebtType = command.DebtType,
            ContractType = command.ContractType,
            CurrencyCode = command.CurrencyCode,
            ListDate = command.ListDate,
            ItemizationDate = command.ItemizationDate,
            InterestAccruedThrough = command.InterestAccruedThrough ?? command.ItemizationDate,
        };
        await context.RecordAsync<AccountPlaced>(account, prior: null, @new: placed, reason: "account_placed", cancellation: cancellation);

        var codes = command.OpeningBalances.Keys.ToArray();
        context.QueueSql(
            "INSERT INTO ar.account_balance_bucket (account_balance_bucket_pk, balance_minor_units, account_fk, balance_bucket_definition_fk) " +
            "SELECT * FROM unnest(?::uuid[], ?::bigint[], ?::uuid[], ?::uuid[])",
            codes.Select(_ => Guid.NewGuid()).ToArray(),
            codes.Select(code => command.OpeningBalances[code]).ToArray(),
            codes.Select(_ => command.AccountPk).ToArray(),
            codes.Select(code => definitions[code]).ToArray());
    }
}

public sealed class OpenDisputeHandler : ICommandHandler<OpenDispute>
{
    public async Task HandleAsync(OpenDispute command, CommandContext context, CancellationToken cancellation)
    {
        var account = (await Streams.LockAccountsAsync(context, [command.AccountPk], withBuckets: false, cancellation)).Accounts.Single();
        if (account.IsDisputed)
        {
            throw new CommandRejectedException("Account is already disputed", "already_disputed");
        }
        account.IsDisputed = true;
        // A dispute is a member of the account aggregate, so the event belongs
        // to the account and carries the dispute's identity (WAX-0001).
        await context.RecordAsync<DisputeOpened>(
            account,
            prior: new JsonObject { ["is_disputed"] = false },
            @new: new JsonObject { ["is_disputed"] = true },
            reason: "dispute_raised",
            metadata: new JsonObject { ["dispute_pk"] = command.DisputePk.ToString() },
            cancellation: cancellation);
    }
}

public sealed class PostPaymentHandler : ICommandHandler<PostPayment>
{
    /// <summary>HIVE-0007: allocate across accounts, then run each account's waterfall.</summary>
    public async Task HandleAsync(PostPayment command, CommandContext context, CancellationToken cancellation)
    {
        var receivedAtAsked = command.ReceivedAt is { } asked ? IsoFormat.TruncateToMicroseconds(asked) : (DateTimeOffset?)null;
        // What was asked, kept in the event so the trail shows whether money
        // reached an account by the usual order or by the payer's direction.
        var request = PaymentShapes.Request(command, receivedAtAsked);

        var wanted = command.AccountPks.Distinct().ToArray();
        var (paymentExists, accounts, rows) = await Streams.LockForPaymentAsync(context, command.PaymentPk, wanted, cancellation);
        if (paymentExists)
        {
            var (_, recorded) = await Streams.FirstEventAsync(context, command.PaymentPk, cancellation);
            // A payment recorded before a field was added to the request is the
            // same payment if it matches with that field at its default.
            var askedBefore = new JsonObject { ["consumer_directed_accounts"] = new JsonArray() };
            if (recorded?["request"] is JsonObject recordedRequest)
            {
                foreach (var (key, value) in recordedRequest)
                {
                    askedBefore[key] = value?.DeepClone();
                }
            }
            var same = JsonNode.DeepEquals(askedBefore, request)
                && recorded?["amount_minor_units"]?.GetValue<long>() == command.AmountMinorUnits
                && recorded?["currency_code"]?.GetValue<string>() == command.CurrencyCode;
            if (same)
            {
                throw new AlreadyAppliedException();
            }
            throw new CommandRejectedException("A different payment already exists under this key", "key_reused");
        }
        if (accounts.Count != wanted.Length)
        {
            throw new CommandRejectedException("One or more accounts do not exist", "account_not_found");
        }
        var known = accounts.Select(account => account.Pk).ToHashSet();

        if (command.DirectedAllocations.Keys.Intersect(command.BucketAmounts.Keys).Any())
        {
            throw new CommandRejectedException("An account may carry a directed amount or bucket amounts, not both", "direction_conflict");
        }
        if (!command.DirectedAllocations.Keys.Union(command.BucketAmounts.Keys).All(known.Contains))
        {
            throw new CommandRejectedException("Directions name an account that is not on this payment", "direction_off_payment");
        }
        foreach (var account in accounts)
        {
            if (account.Status != "active")
            {
                throw new CommandRejectedException($"Account {account.Pk} is not active", "account_not_active");
            }
            if (account.CurrencyCode != command.CurrencyCode)
            {
                throw new CommandRejectedException($"Account {account.Pk} is not held in {command.CurrencyCode}", "currency_mismatch");
            }
        }

        // HIVE-0007's floor: a disputed account receives only what the
        // consumer directed to it. Explicit bucket amounts land on the
        // account, so on a disputed account the command must say the consumer
        // directed them.
        var stated = command.ConsumerDirectedAccounts.ToHashSet();
        if (!stated.All(command.BucketAmounts.ContainsKey))
        {
            // The statement goes into the permanent record, so it may only be
            // made about an account whose bucket amounts this payment carries.
            throw new CommandRejectedException(
                "consumer_directed_accounts names an account with no bucket amounts on this payment",
                "direction_off_payment");
        }
        foreach (var account in accounts)
        {
            if (account.IsDisputed && command.BucketAmounts.ContainsKey(account.Pk) && !stated.Contains(account.Pk))
            {
                throw new CommandRejectedException(
                    $"Account {account.Pk} is disputed; bucket amounts need the consumer's direction",
                    "dispute_needs_direction");
            }
        }

        // Explicit bucket amounts are a direction to that account for their total.
        foreach (var (accountPk, amounts) in command.BucketAmounts)
        {
            foreach (var (code, amount) in amounts)
            {
                if (!rows[accountPk].TryGetValue(code, out var row) || amount > row.Balance)
                {
                    throw new CommandRejectedException($"Bucket {code} on account {accountPk} cannot take {amount}", "bucket_cannot_take");
                }
            }
        }
        var directed = new Dictionary<Guid, long>(command.DirectedAllocations);
        foreach (var (accountPk, amounts) in command.BucketAmounts)
        {
            directed[accountPk] = amounts.Values.Sum();
        }
        if (directed.Values.Sum() > command.AmountMinorUnits)
        {
            throw new CommandRejectedException("Directed amounts exceed the payment amount", "directed_exceeds_amount");
        }

        var positions = accounts
            .Select(account => new AccountPosition(account.Pk, account.ListDate, account.IsDisputed, rows[account.Pk].Values.Sum(row => row.Balance)))
            .ToList();
        // An account with explicit bucket amounts receives exactly those
        // amounts. The rest spreads over the other accounts and is otherwise
        // held as unapplied.
        var (shares, unapplied) = Allocation.AllocateAcrossAccounts(
            command.AmountMinorUnits, positions, command.AllocationMethod, directed, command.BucketAmounts.Keys.ToHashSet());

        var paydowns = new Dictionary<Guid, List<(string Code, long Amount)>>();
        foreach (var account in accounts)
        {
            var paydown = command.BucketAmounts.TryGetValue(account.Pk, out var explicitAmounts)
                ? explicitAmounts.Select(pair => (pair.Key, pair.Value)).ToList()
                : Allocation.RunWaterfall(shares[account.Pk], rows[account.Pk].Values.Select(row => (row.Code, row.Priority, row.Balance))).Paydown;
            if (paydown.Count > 0)
            {
                paydowns[account.Pk] = paydown;
            }
        }

        var applied = paydowns.Values.Sum(paydown => paydown.Sum(part => part.Amount));
        if (applied + unapplied != command.AmountMinorUnits)
        {
            throw new InvalidOperationException(
                $"Payment {command.PaymentPk} does not balance: {applied} applied plus {unapplied} unapplied is not {command.AmountMinorUnits}");
        }

        var receivedAt = receivedAtAsked ?? context.OccurredAt;
        var payment = new PaymentState
        {
            Pk = command.PaymentPk,
            IsNew = true,
            AmountMinorUnits = command.AmountMinorUnits,
            CurrencyCode = command.CurrencyCode,
            ReceivedAt = receivedAt,
            Distribution = Json.Distribution(paydowns),
            UnappliedMinorUnits = unapplied,
        };
        await context.RecordAsync<PaymentPosted>(
            payment,
            prior: null,
            @new: PaymentShapes.Posted(payment.AmountMinorUnits, payment.CurrencyCode, receivedAt, paydowns, unapplied, request),
            reason: "payment_received",
            cancellation: cancellation);
        foreach (var account in accounts)
        {
            if (paydowns.TryGetValue(account.Pk, out var paydown))
            {
                await Buckets.PostAsync<AccountPaymentApplied>(context, account, rows[account.Pk], paydown, -1, "payment_received", payment, cancellation);
            }
        }
    }
}

public sealed class ReversePaymentHandler : ICommandHandler<ReversePayment>
{
    /// <summary>HIVE-0007: back out the recorded distribution, not a recomputed one.</summary>
    public async Task HandleAsync(ReversePayment command, CommandContext context, CancellationToken cancellation)
    {
        PaymentState? payment = null;
        await using (var batch = context.CreateBatch())
        {
            batch.BatchCommands.Add(new NpgsqlBatchCommand(PaymentState.LockSql)
            {
                Parameters = { new NpgsqlParameter { Value = command.PaymentPk } },
            });
            batch.BatchCommands.Add(CommandContext.StreamHeadsCommand([command.PaymentPk]));
            await using var reader = await batch.ExecuteReaderAsync(cancellation);
            if (await reader.ReadAsync(cancellation))
            {
                payment = PaymentState.Read(reader);
            }
            await reader.NextResultAsync(cancellation);
            await context.ReadStreamHeadsAsync(reader, [command.PaymentPk], cancellation);
        }
        if (payment is null)
        {
            throw new CommandRejectedException("Payment does not exist", "payment_not_found");
        }
        if (payment.Status != "posted")
        {
            throw new CommandRejectedException("Payment is not in a reversible state", "payment_not_reversible");
        }
        var accountPks = payment.Distribution.Select(pair => Guid.Parse(pair.Key)).ToArray();
        var (accounts, rows) = await Streams.LockAccountsAsync(context, accountPks, withBuckets: true, cancellation);

        payment.Status = "reversed";
        await context.RecordAsync<PaymentReversed>(
            payment,
            prior: new JsonObject { ["status"] = "posted" },
            @new: new JsonObject { ["status"] = "reversed" },
            reason: "payment_returned",
            cancellation: cancellation);
        foreach (var account in accounts)
        {
            var amounts = payment.Distribution[account.Pk.ToString()]!.AsObject()
                .Select(pair => (pair.Key, pair.Value!.GetValue<long>()))
                .ToList();
            await Buckets.PostAsync<AccountPaymentReversed>(context, account, rows[account.Pk], amounts, 1, "payment_returned", payment, cancellation);
        }
    }
}

static class Buckets
{
    /// <summary>Move bucket balances and write the account's fan-out event together.</summary>
    public static async ValueTask PostAsync<TEvent>(
        CommandContext context,
        AccountState account,
        Dictionary<string, BucketRow> rows,
        IReadOnlyList<(string Code, long Amount)> amounts,
        int sign,
        string reason,
        PaymentState payment,
        CancellationToken cancellation)
        where TEvent : Wax.Events.WaxEvent, new()
    {
        var (prior, @new, metadata, moved) = Move(rows, amounts, sign, payment.Pk, payment.ReceivedAt);
        foreach (var row in moved)
        {
            context.QueueSql("UPDATE ar.account_balance_bucket SET balance_minor_units = ? WHERE account_balance_bucket_pk = ?", row.Balance, row.Pk);
        }
        await context.RecordAsync<TEvent>(account, prior, @new, reason, metadata, cancellation);
    }

    /// <summary>Move the balances in memory and shape the account event's values.
    /// The single-command path and the bulk writer both use it, so their events match.</summary>
    public static (JsonObject Prior, JsonObject New, JsonObject Metadata, List<BucketRow> Moved) Move(
        Dictionary<string, BucketRow> rows,
        IReadOnlyList<(string Code, long Amount)> amounts,
        int sign,
        Guid paymentPk,
        DateTimeOffset receivedAt)
    {
        var prior = new JsonObject();
        var @new = new JsonObject();
        var bucketAmounts = new JsonObject();
        var moved = new List<BucketRow>();
        foreach (var (code, amount) in amounts)
        {
            var row = rows[code];
            prior[code] = row.Balance;
            row.Balance += sign * amount;
            @new[code] = row.Balance;
            bucketAmounts[code] = amount;
            moved.Add(row);
        }
        return (
            new JsonObject { ["balance_buckets"] = prior },
            new JsonObject { ["balance_buckets"] = @new },
            new JsonObject
            {
                ["payment_pk"] = paymentPk.ToString(),
                ["received_at"] = IsoFormat.Default(receivedAt),
                ["bucket_amounts"] = bucketAmounts,
            },
            moved);
    }
}

/// <summary>The values of a payment's own events, shared by the single-command
/// path and the bulk writer so that the two write the same events.</summary>
static class PaymentShapes
{
    /// <summary>What was asked, kept in the event so the trail shows whether money
    /// reached an account by the usual order or by the payer's direction.</summary>
    public static JsonObject Request(PostPayment command, DateTimeOffset? receivedAtAsked) => new()
    {
        ["account_pks"] = Json.SortedKeys(command.AccountPks),
        ["allocation_method"] = command.AllocationMethod,
        ["directed_allocations"] = new JsonObject(command.DirectedAllocations
            .OrderBy(pair => pair.Key.ToString(), StringComparer.Ordinal)
            .Select(pair => KeyValuePair.Create(pair.Key.ToString(), (JsonNode?)pair.Value))),
        ["bucket_amounts"] = new JsonObject(command.BucketAmounts
            .OrderBy(pair => pair.Key.ToString(), StringComparer.Ordinal)
            .Select(pair => KeyValuePair.Create(pair.Key.ToString(), (JsonNode?)Json.Amounts(pair.Value)))),
        ["consumer_directed_accounts"] = Json.SortedKeys(command.ConsumerDirectedAccounts),
        ["received_at"] = receivedAtAsked is { } at ? IsoFormat.Default(at) : null,
    };

    public static JsonObject Posted(
        long amount, string currency, DateTimeOffset receivedAt,
        Dictionary<Guid, List<(string Code, long Amount)>> paydowns, long unapplied, JsonObject request) => new()
    {
        ["status"] = "posted",
        ["amount_minor_units"] = amount,
        ["currency_code"] = currency,
        ["received_at"] = IsoFormat.Default(receivedAt),
        ["distribution"] = Json.Distribution(paydowns),
        ["unapplied_minor_units"] = unapplied,
        ["request"] = request,
    };
}

static class Streams
{
    public static async Task<(string? EventType, JsonObject? NewValue)> FirstEventAsync(CommandContext context, Guid streamId, CancellationToken cancellation)
    {
        await using var command = context.CreateCommand(
            "SELECT type, data -> 'new_value' FROM core.mt_events WHERE stream_id = $1 AND version = 1");
        command.Parameters.AddWithValue(streamId);
        await using var reader = await command.ExecuteReaderAsync(cancellation);
        if (!await reader.ReadAsync(cancellation))
        {
            return (null, null);
        }
        return (reader.GetString(0), reader.IsDBNull(1) ? null : JsonNode.Parse(reader.GetString(1)) as JsonObject);
    }

    /// <summary>Lock the accounts in a fixed order, read their buckets if asked,
    /// and read their stream heads: one round trip.</summary>
    public static async Task<(List<AccountState> Accounts, Dictionary<Guid, Dictionary<string, BucketRow>> Rows)> LockAccountsAsync(
        CommandContext context, Guid[] accountPks, bool withBuckets, CancellationToken cancellation)
    {
        var wanted = accountPks.Distinct().ToArray();
        var accounts = new List<AccountState>();
        Dictionary<Guid, Dictionary<string, BucketRow>> rows = [];
        await using (var batch = context.CreateBatch())
        {
            batch.BatchCommands.Add(AccountState.LockCommand(wanted));
            if (withBuckets)
            {
                batch.BatchCommands.Add(BucketRow.ForAccountsCommand(wanted));
            }
            batch.BatchCommands.Add(CommandContext.StreamHeadsCommand(wanted));
            await using var reader = await batch.ExecuteReaderAsync(cancellation);
            while (await reader.ReadAsync(cancellation))
            {
                accounts.Add(AccountState.Read(reader));
            }
            if (withBuckets)
            {
                await reader.NextResultAsync(cancellation);
                rows = await BucketRow.ReadAsync(reader, wanted, cancellation);
            }
            await reader.NextResultAsync(cancellation);
            await context.ReadStreamHeadsAsync(reader, wanted, cancellation);
        }
        if (accounts.Count != wanted.Length)
        {
            throw new CommandRejectedException("One or more accounts do not exist", "account_not_found");
        }
        return (accounts, rows);
    }

    /// <summary>Lock one account, then run one more query that must see the
    /// state as of the lock, then read the account's stream head: one round
    /// trip. Returns the account and the extra query's first row, if any.</summary>
    public static async Task<(AccountState Account, object[]? Row)> LockAccountWithAsync(
        CommandContext context, Guid accountPk, NpgsqlBatchCommand extra, CancellationToken cancellation)
    {
        AccountState? account = null;
        object[]? row = null;
        await using (var batch = context.CreateBatch())
        {
            batch.BatchCommands.Add(AccountState.LockCommand([accountPk]));
            batch.BatchCommands.Add(extra);
            batch.BatchCommands.Add(CommandContext.StreamHeadsCommand([accountPk]));
            await using var reader = await batch.ExecuteReaderAsync(cancellation);
            if (await reader.ReadAsync(cancellation))
            {
                account = AccountState.Read(reader);
            }
            await reader.NextResultAsync(cancellation);
            if (await reader.ReadAsync(cancellation))
            {
                row = new object[reader.FieldCount];
                reader.GetValues(row);
            }
            await reader.NextResultAsync(cancellation);
            await context.ReadStreamHeadsAsync(reader, [accountPk], cancellation);
        }
        return account is null
            ? throw new CommandRejectedException("One or more accounts do not exist", "account_not_found")
            : (account, row);
    }

    /// <summary>Whether the payment exists, then the locked accounts, their
    /// buckets, and their stream heads: one round trip.</summary>
    public static async Task<(bool PaymentExists, List<AccountState> Accounts, Dictionary<Guid, Dictionary<string, BucketRow>> Rows)> LockForPaymentAsync(
        CommandContext context, Guid paymentPk, Guid[] accountPks, CancellationToken cancellation)
    {
        var accounts = new List<AccountState>();
        bool exists;
        Dictionary<Guid, Dictionary<string, BucketRow>> rows;
        await using var batch = context.CreateBatch();
        batch.BatchCommands.Add(new NpgsqlBatchCommand("SELECT EXISTS (SELECT 1 FROM ar.payment WHERE payment_pk = $1)")
        {
            Parameters = { new NpgsqlParameter { Value = paymentPk } },
        });
        batch.BatchCommands.Add(AccountState.LockCommand(accountPks));
        batch.BatchCommands.Add(BucketRow.ForAccountsCommand(accountPks));
        batch.BatchCommands.Add(CommandContext.StreamHeadsCommand(accountPks));
        await using var reader = await batch.ExecuteReaderAsync(cancellation);
        await reader.ReadAsync(cancellation);
        exists = reader.GetBoolean(0);
        await reader.NextResultAsync(cancellation);
        while (await reader.ReadAsync(cancellation))
        {
            accounts.Add(AccountState.Read(reader));
        }
        await reader.NextResultAsync(cancellation);
        rows = await BucketRow.ReadAsync(reader, accounts.Select(account => account.Pk), cancellation);
        await reader.NextResultAsync(cancellation);
        await context.ReadStreamHeadsAsync(reader, accountPks, cancellation);
        return (exists, accounts, rows);
    }
}

static class Json
{
    public static JsonObject Amounts(IEnumerable<KeyValuePair<string, long>> amounts) =>
        new(amounts.Select(pair => KeyValuePair.Create(pair.Key, (JsonNode?)pair.Value)));

    public static JsonArray SortedKeys(IEnumerable<Guid> keys) =>
        new(keys.Distinct().Select(key => key.ToString()).Order(StringComparer.Ordinal).Select(key => (JsonNode?)key).ToArray());

    /// <summary>A fresh node each call: a JSON node can belong to one parent only.</summary>
    public static JsonObject Distribution(Dictionary<Guid, List<(string Code, long Amount)>> paydowns) =>
        new(paydowns.Select(pair => KeyValuePair.Create(
            pair.Key.ToString(),
            (JsonNode?)new JsonObject(pair.Value.Select(part => KeyValuePair.Create(part.Code, (JsonNode?)part.Amount))))));
}
