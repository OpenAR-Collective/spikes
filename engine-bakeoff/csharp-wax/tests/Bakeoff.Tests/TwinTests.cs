using HiveAR;
using HiveAR.Data;
using Npgsql;
using Wax;
using Wax.Commands;
using Wax.Hashing;
using Wax.Store;
using Xunit;

namespace Bakeoff.Tests;

/// <summary>A clock that moves one millisecond each time it is read, so two engines
/// given the same commands in the same order see the same instants.</summary>
sealed class SteppingClock : TimeProvider
{
    static readonly DateTimeOffset Start = new(2026, 10, 1, 12, 0, 0, TimeSpan.Zero);
    long reads;

    public override DateTimeOffset GetUtcNow() => Start.AddMilliseconds(Interlocked.Increment(ref reads));
}

/// <summary>
/// The same commands through plain SQL, through EF Core, and through the
/// batched statement shapes must store the same events and the same state.
/// Every database is a copy of one migrated template, so reference keys match,
/// and each engine has its own stepping clock, so the events match byte for
/// byte: hashes, chains, and global order included. Foundation-written.
/// </summary>
public sealed class TwinTests : IAsyncLifetime
{
    readonly string template = $"hivear_twin_t_{Guid.NewGuid():N}"[..28];
    readonly string sqlName = $"hivear_twin_s_{Guid.NewGuid():N}"[..28];
    readonly string efName = $"hivear_twin_e_{Guid.NewGuid():N}"[..28];
    readonly string batchedName = $"hivear_twin_b_{Guid.NewGuid():N}"[..28];
    WaxEngine sql = null!;
    WaxEngine ef = null!;
    WaxEngine batched = null!;

    public async Task InitializeAsync()
    {
        await Admin($"CREATE DATABASE {template}");
        await using (var seeded = await WaxEngine.CreateAsync(
            WaxSettings.ConnectionStringFromEnvironment(template), [new HiveArModule(StateAccess.Sql)], migrate: true))
        {
            await HiveArModule.SeedDemoAsync(seeded.DataSource);
        }
        NpgsqlConnection.ClearAllPools();
        await Admin($"CREATE DATABASE {sqlName} TEMPLATE {template}");
        await Admin($"CREATE DATABASE {efName} TEMPLATE {template}");
        await Admin($"CREATE DATABASE {batchedName} TEMPLATE {template}");
        sql = await WaxEngine.CreateAsync(
            WaxSettings.ConnectionStringFromEnvironment(sqlName), [new HiveArModule(StateAccess.Sql)], clock: new SteppingClock());
        ef = await WaxEngine.CreateAsync(
            WaxSettings.ConnectionStringFromEnvironment(efName), [new HiveArModule(StateAccess.EfCore)], clock: new SteppingClock());
        batched = await WaxEngine.CreateAsync(
            WaxSettings.ConnectionStringFromEnvironment(batchedName), [new HiveArModule(StateAccess.Batched)], clock: new SteppingClock());
    }

    public async Task DisposeAsync()
    {
        await sql.DisposeAsync();
        await ef.DisposeAsync();
        await batched.DisposeAsync();
        foreach (var name in new[] { sqlName, efName, batchedName, template })
        {
            await Admin($"DROP DATABASE IF EXISTS {name} WITH (FORCE)");
        }
    }

    static async Task Admin(string statement)
    {
        await using var admin = NpgsqlDataSource.Create(WaxSettings.ConnectionStringFromEnvironment());
        await using var command = admin.CreateCommand(statement);
        await command.ExecuteNonQueryAsync();
    }

    static Guid Key(string name) => DerivedUuid.From("twin:" + name);

    /// <summary>A command that the rule runner would dispatch, with the rule
    /// runner's attribution and a fixed trigger key, so both twins match.</summary>
    sealed record ByRule(ICommand Inner) : ICommand
    {
        public void Validate() => Inner.Validate();
    }

    static IEnumerable<ICommand> Commands()
    {
        PlaceAccount Place(string name, Dictionary<string, long> balances) => new()
        {
            AccountPk = Key(name),
            ClientAccountNumber = "TW-" + name,
            DebtType = "personal_loan",
            ContractType = "third_party_contingency",
            CurrencyCode = "USD",
            ListDate = new DateOnly(2026, 1, 15),
            ItemizationDate = new DateOnly(2025, 12, 31),
            InterestAccruedThrough = new DateOnly(2026, 9, 30),
            OpeningBalances = balances,
        };
        yield return Place("a1", new() { ["principal"] = 100_000, ["interest"] = 5_000 });
        yield return Place("a2", new() { ["principal"] = 20_000, ["fee"] = 300 });
        yield return Place("a3", new() { ["principal"] = 1_000 });
        yield return Place("a1", new() { ["principal"] = 100_000, ["interest"] = 5_000 });   // already applied
        yield return new PostPayment { PaymentPk = Key("p1"), AccountPks = [Key("a1"), Key("a2")], AmountMinorUnits = 4_000, CurrencyCode = "USD" };
        yield return new PostPayment { PaymentPk = Key("p2"), AccountPks = [Key("a2")], AmountMinorUnits = 1_200, CurrencyCode = "USD" };
        yield return new ReversePayment { PaymentPk = Key("p2") };
        yield return new ReversePayment { PaymentPk = Key("p2") };   // refused: not reversible
        yield return new OpenDispute { AccountPk = Key("a3"), DisputePk = Key("d1") };
        yield return new OpenDispute { AccountPk = Key("a3"), DisputePk = Key("d2") };   // refused: already disputed
        yield return new PostPayment
        {
            PaymentPk = Key("p3"),
            AccountPks = [Key("a3"), Key("a1")],
            AmountMinorUnits = 1_500,
            CurrencyCode = "USD",
            BucketAmounts = new Dictionary<Guid, IReadOnlyDictionary<string, long>> { [Key("a1")] = new Dictionary<string, long> { ["interest"] = 500 } },
        };
        yield return new PostPayment { PaymentPk = Key("p1"), AccountPks = [Key("a1"), Key("a2")], AmountMinorUnits = 4_000, CurrencyCode = "USD" };   // already applied
        yield return new AccrueInterest { AccountPk = Key("a1"), AccrualDate = new DateOnly(2026, 10, 1), AnnualRateBasisPoints = 1_800 };
        yield return new AccrueInterest { AccountPk = Key("a2"), AccrualDate = new DateOnly(2026, 10, 1), AnnualRateBasisPoints = 1_800 };   // adds an interest bucket
        yield return new AccrueInterest { AccountPk = Key("a1"), AccrualDate = new DateOnly(2026, 10, 1), AnnualRateBasisPoints = 1_800 };   // already applied
        yield return new AssignToQueue { AccountPk = Key("a2"), QueueCode = "dispute_review" };
        yield return new CreateTask { TaskPk = Key("t1"), AccountPk = Key("a1"), TaskType = "first_contact" };
        yield return new CreateTask { TaskPk = Key("t1"), AccountPk = Key("a1"), TaskType = "first_contact" };   // already applied
        yield return new ScheduleFutureEvent
        {
            PendingEventPk = Key("e1"),
            AccountPk = Key("a1"),
            TargetEventType = "account.first_contact_due",
            DueAt = new DateTimeOffset(2026, 10, 1, 0, 0, 0, TimeSpan.Zero),
            ScheduledByEvent = Key("trigger"),
        };
        yield return new FirePendingEvent { PendingEventPk = Key("e1") };
        yield return new FirePendingEvent { PendingEventPk = Key("e1") };   // already fired
        yield return new PostPayment { PaymentPk = Key("p4"), AccountPks = [Key("a3")], AmountMinorUnits = 2_000_000, CurrencyCode = "USD" };
        yield return new ByRule(new ChangeAccountStatus { AccountPk = Key("a3"), Status = "paid_in_full", ReasonCode = "paid_in_full" });
    }

    static async Task<List<string>> Run(WaxEngine engine)
    {
        var outcomes = new List<string>();
        foreach (var command in Commands())
        {
            var name = (command is ByRule rule ? rule.Inner : command).GetType().Name;
            try
            {
                if (command is ByRule byRule)
                {
                    // As HiveArRules dispatches its actions, through the same dispatcher.
                    await engine.Commands.DispatchAsync(byRule.Inner, HiveArRules.RuleRunnerActor, "system", "rule", Key("rule-trigger"), "rule");
                }
                else
                {
                    await engine.Commands.DispatchAsync(command, Scenario.Actor, "user", "test");
                }
                outcomes.Add(name + ": done");
            }
            catch (CommandRejectedException refused)
            {
                outcomes.Add(name + ": " + refused.Code);
            }
        }
        return outcomes;
    }

    static async Task<List<string>> Rows(WaxEngine engine, string sql)
    {
        var rows = new List<string>();
        await using var command = engine.DataSource.CreateCommand($"SELECT row_to_json(x)::text FROM ({sql}) x");
        await using var reader = await command.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            rows.Add(reader.GetString(0));
        }
        return rows;
    }

    [Fact]
    public Task Ef_core_and_plain_sql_store_identical_events_and_state() => Compare(ef);

    [Fact]
    public Task Batched_and_plain_sql_store_identical_events_and_state() => Compare(batched);

    async Task Compare(WaxEngine other)
    {
        Assert.Equal(await Run(sql), await Run(other));

        string[] queries =
        [
            // Everything but the random event key and the store's wall-clock recorded_at.
            "SELECT global_sequence_number, aggregate_sequence_number, event_type, schema_version, aggregate_pk_ref, aggregate_type, " +
            "occurred_at, actor_pk_ref, actor_type, prior_value, new_value, reason_code_fk, source, correlation_pk_ref, correlation_type, " +
            "metadata, prior_event_hash, event_hash FROM core.event ORDER BY global_sequence_number",
            "SELECT * FROM ar.account ORDER BY account_pk",
            "SELECT account_fk, balance_bucket_definition_fk, balance_minor_units FROM ar.account_balance_bucket ORDER BY account_fk, balance_bucket_definition_fk",
            "SELECT * FROM ar.payment ORDER BY payment_pk",
            "SELECT * FROM ar.task ORDER BY task_pk",
            // fired_at is the server's clock in one mode and the client's in the other.
            "SELECT pending_event_pk, aggregate_pk_ref, aggregate_type, target_event_type, due_at, status, scheduled_by_event, " +
            "fired_at IS NOT NULL AS fired FROM core.pending_event ORDER BY pending_event_pk",
        ];
        foreach (var query in queries)
        {
            var expected = await Rows(sql, query);
            Assert.NotEmpty(expected);
            Assert.Equal(expected, await Rows(other, query));
        }
        Assert.True((await Rows(sql, "SELECT 1 FROM core.event")).Count >= 15);
        Assert.NotEmpty(await Rows(sql, "SELECT 1 FROM core.event WHERE source = 'rule'"));
        Assert.Empty((await ChainVerifier.VerifyAllAsync(sql.DataSource)).Problems);
        Assert.Empty((await ChainVerifier.VerifyAllAsync(other.DataSource)).Problems);
    }
}
