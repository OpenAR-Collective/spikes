using System.Text.Json.Nodes;
using HiveAR;
using Marten;
using Wax.Batches;
using Wax.Commands;
using Wax.Hashing;
using Wax.Rules;
using Wax.Store;
using Xunit;
using static Bakeoff.Tests.Scenario;

namespace Bakeoff.Tests;

/// <summary>The commands WORKLOADS.md adds, one at a time.</summary>
public sealed class WorkloadCommandTests(DatabaseFixture db) : IClassFixture<DatabaseFixture>
{
    readonly Scenario scenario = new(db);

    [Fact]
    public void Derived_keys_match_the_python_side()
    {
        var vectors = JsonNode.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "derived-uuids.json")))!.AsArray();
        Assert.NotEmpty(vectors);
        foreach (var vector in vectors)
        {
            Assert.Equal(vector!["uuid"]!.GetValue<string>(), DerivedUuid.From(vector["name"]!.GetValue<string>()).ToString());
        }
    }

    [Fact]
    public async Task Interest_accrues_once_per_day_on_the_principal()
    {
        var accountPk = await PlaceAccruing(principal: 100_000, interest: 5_000);
        var accrue = new AccrueInterest { AccountPk = accountPk, AccrualDate = new DateOnly(2026, 10, 1), AnnualRateBasisPoints = 1_800 };
        await scenario.Run(accrue);
        // 100,000 * 1,800 * 1 // 3,650,000 = 49.
        Assert.Equal(Buckets(("principal", 100_000), ("interest", 5_049)), await scenario.Balances(accountPk));
        var accrued = (await scenario.LoadStream(accountPk))[^1];
        Assert.Equal("account.interest_accrued", accrued.Type);
        JsonEqual("""{"balance_buckets": {"interest": 5000}, "interest_accrued_through": "2026-09-30"}""", accrued.PriorValue);
        JsonEqual("""{"balance_buckets": {"interest": 5049}, "interest_accrued_through": "2026-10-01"}""", accrued.NewValue);
        JsonEqual("""{"days": 1, "annual_rate_basis_points": 1800, "amount_minor_units": 49}""", accrued.Metadata);

        // The same date again, or an earlier one, is answered as success and writes nothing.
        await scenario.Run(accrue);
        await scenario.Run(accrue with { AccrualDate = new DateOnly(2026, 9, 15) });
        Assert.Equal(2, (await scenario.LoadStream(accountPk)).Count);
        Assert.Empty(await ChainVerifier.VerifyStreamAsync(db.Engine.DataSource, accountPk));
    }

    [Fact]
    public async Task Status_and_queue_changes_are_idempotent()
    {
        var accountPk = await scenario.Place(principal: 1_000);
        var status = new ChangeAccountStatus { AccountPk = accountPk, Status = "paid_in_full", ReasonCode = "paid_in_full" };
        await scenario.Run(status);
        await scenario.Run(status);
        var queue = new AssignToQueue { AccountPk = accountPk, QueueCode = "dispute_review" };
        await scenario.Run(queue);
        await scenario.Run(queue);
        var events = await scenario.LoadStream(accountPk);
        Assert.Equal(["account.placed", "account.status_changed", "account.queue_assigned"], events.Select(e => e.Type));
        JsonEqual("""{"queue_code": null}""", events[2].PriorValue);
        Assert.Equal("paid_in_full", await scenario.Scalar<string>("SELECT status FROM ar.account WHERE account_pk = $1", accountPk));
        Assert.Equal("dispute_review", await scenario.Scalar<string>("SELECT queue_code FROM ar.account WHERE account_pk = $1", accountPk));
    }

    [Fact]
    public async Task A_scheduled_event_fires_once_on_its_account()
    {
        var accountPk = await scenario.Place(principal: 1_000);
        var pendingEventPk = Guid.NewGuid();
        var schedule = new ScheduleFutureEvent
        {
            PendingEventPk = pendingEventPk,
            AccountPk = accountPk,
            TargetEventType = "account.first_contact_due",
            DueAt = DateTimeOffset.UtcNow.AddMinutes(-1),
        };
        await scenario.Run(schedule);
        await scenario.Run(schedule);
        await Assert.ThrowsAsync<CommandRejectedException>(() => scenario.Run(schedule with { TargetEventType = "account.dispute_review_due" }));

        await scenario.Run(new FirePendingEvent { PendingEventPk = pendingEventPk });
        await scenario.Run(new FirePendingEvent { PendingEventPk = pendingEventPk });
        var events = await scenario.LoadStream(accountPk);
        Assert.Equal(["account.placed", "pending_event.scheduled", "account.first_contact_due"], events.Select(e => e.Type));
        Assert.Equal(pendingEventPk.ToString(), events[2].Metadata!["pending_event_pk"]!.GetValue<string>());
        Assert.Equal("fired", await scenario.Scalar<string>("SELECT status FROM core.pending_event WHERE pending_event_pk = $1", pendingEventPk));
        Assert.Empty(await ChainVerifier.VerifyStreamAsync(db.Engine.DataSource, accountPk));
    }

    [Fact]
    public async Task A_task_is_created_once_per_key()
    {
        var accountPk = await scenario.Place(principal: 1_000);
        var task = new CreateTask { TaskPk = Guid.NewGuid(), AccountPk = accountPk, TaskType = "first_contact" };
        await scenario.Run(task);
        await scenario.Run(task);
        var reused = await Assert.ThrowsAsync<CommandRejectedException>(() => scenario.Run(task with { TaskType = "letter" }));
        Assert.Equal("key_reused", reused.Code);
        Assert.Equal(1L, await scenario.Scalar<long>("SELECT count(*) FROM ar.task WHERE account_fk = $1", accountPk));
    }

    async Task<Guid> PlaceAccruing(long principal, long interest)
    {
        var command = new PlaceAccount
        {
            ClientAccountNumber = "ACCRUE",
            DebtType = "personal_loan",
            ContractType = "third_party_contingency",
            CurrencyCode = "USD",
            ListDate = new DateOnly(2026, 1, 15),
            ItemizationDate = new DateOnly(2025, 12, 31),
            InterestAccruedThrough = new DateOnly(2026, 9, 30),
            OpeningBalances = new Dictionary<string, long> { ["principal"] = principal, ["interest"] = interest },
        };
        await scenario.Run(command);
        return command.AccountPk;
    }
}

/// <summary>The seed and the two batch jobs, on a small scale, with their gates.</summary>
public sealed class SeedAndBatchTests(DatabaseFixture db) : IClassFixture<DatabaseFixture>
{
    const int Accounts = 2_500;
    readonly Scenario scenario = new(db);

    [Fact]
    public async Task The_seed_then_both_batches_pass_their_gates()
    {
        var positions = db.Engine.Store.Advanced.AllAsyncProjectionShardNames().Select(shard => shard.Identity).Append("wax_rule_runner:All");
        await Seeder.SeedAsync(db.Engine.DataSource, db.Engine.Store, Accounts, positions);
        Assert.Equal((long)Accounts, await scenario.Scalar<long>("SELECT count(*) FROM ar.account"));
        Assert.Empty((await ChainVerifier.VerifyAllAsync(db.Engine.DataSource)).Problems);
        var first = SeedAccount.For(0);
        Assert.Equal(Buckets(("principal", first.Principal), ("interest", first.Interest), ("fee", first.Fee)), await scenario.Balances(first.Pk));
        var totalBefore = await scenario.Scalar<decimal>("SELECT sum(balance_minor_units) FROM ar.account_balance_bucket");

        var actor = DerivedUuid.From("bakeoff:batch");
        var runner = new BatchRunner(db.Engine.DataSource, "ar.account", "account_pk");
        var payments = await runner.CreateRunAsync("payment_file", 1_000);
        Assert.Equal(3L, await scenario.Scalar<long>("SELECT count(*) FROM core.batch_job_chunk WHERE batch_job_run_fk = $1", payments));
        await runner.RunAsync(payments, workers: 4, "test", accountPk => db.Engine.Commands.DispatchAsync(new PostPayment
        {
            PaymentPk = DerivedUuid.From($"bakeoff:payment:{payments}:{accountPk}"),
            AccountPks = [accountPk],
            AmountMinorUnits = 2_500,
            CurrencyCode = "USD",
            ReceivedAt = new DateTimeOffset(2026, 10, 1, 6, 0, 0, TimeSpan.Zero),
        }, actor, "batch", "batch"));
        Assert.Equal((long)Accounts, await scenario.Scalar<long>("SELECT count(*) FROM ar.payment"));
        Assert.Equal(totalBefore - 2_500m * Accounts, await scenario.Scalar<decimal>("SELECT sum(balance_minor_units) FROM ar.account_balance_bucket"));

        // The accrual gate: every account's interest grew by principal * 1800 // 3650000.
        await scenario.Execute("CREATE TABLE before_accrual AS SELECT b.account_fk, d.code, b.balance_minor_units FROM ar.account_balance_bucket b JOIN ar.balance_bucket_definition d ON d.balance_bucket_definition_pk = b.balance_bucket_definition_fk");
        var accrual = await runner.CreateRunAsync("interest_accrual", 1_000);
        await runner.RunAsync(accrual, workers: 4, "test", accountPk => db.Engine.Commands.DispatchAsync(new AccrueInterest
        {
            AccountPk = accountPk,
            AccrualDate = new DateOnly(2026, 10, 1),
            AnnualRateBasisPoints = 1_800,
        }, actor, "batch", "batch"));
        Assert.Equal(0L, await scenario.Scalar<long>(
            "SELECT count(*) FROM ar.account a " +
            "JOIN before_accrual p ON p.account_fk = a.account_pk AND p.code = 'principal' " +
            "JOIN before_accrual i ON i.account_fk = a.account_pk AND i.code = 'interest' " +
            "JOIN ar.account_balance_bucket b ON b.account_fk = a.account_pk " +
            "JOIN ar.balance_bucket_definition d ON d.balance_bucket_definition_pk = b.balance_bucket_definition_fk AND d.code = 'interest' " +
            "WHERE b.balance_minor_units <> i.balance_minor_units + p.balance_minor_units * 1800 / 3650000 " +
            "OR a.interest_accrued_through <> date '2026-10-01'"));
        Assert.Empty((await ChainVerifier.VerifyAllAsync(db.Engine.DataSource)).Problems);

        // The read models catch up from the seed's position and agree.
        using var daemon = await db.Engine.Store.BuildProjectionDaemonAsync();
        await daemon.StartAllAsync();
        await daemon.WaitForNonStaleData(TimeSpan.FromSeconds(120));
        await daemon.StopAllAsync();
        Assert.Equal(0L, await scenario.Scalar<long>(
            "SELECT count(*) FROM ar.account_summary_read_model s JOIN ar.account a ON a.account_pk = s.account_summary_read_model_pk " +
            "WHERE s.balance_minor_units <> (SELECT sum(balance_minor_units) FROM ar.account_balance_bucket WHERE account_fk = a.account_pk) " +
            "OR s.status <> a.status OR s.is_disputed <> a.is_disputed"));
        Assert.Equal((long)Accounts, await scenario.Scalar<long>("SELECT count(*) FROM ar.payment_read_model"));
        // The harness's progress view shows every projection at the head.
        Assert.Equal(3L, await scenario.Scalar<long>("SELECT count(*) FROM core.bakeoff_progress WHERE name LIKE 'ar_%' AND position = head"));
    }
}

/// <summary>The real-time rules, end to end: the rule runner as a Marten
/// subscription and the register poller, with one simulated day of 0.1 seconds.</summary>
public sealed class RuleTests(DatabaseFixture db) : IClassFixture<DatabaseFixture>
{
    readonly Scenario scenario = new(db);

    [Fact]
    public async Task Events_fire_rules_and_scheduled_events_fire_in_turn()
    {
        const double secondsPerDay = 0.1;
        var rules = new RuleRunner(HiveArRules.All(db.Engine.DataSource, secondsPerDay), db.Engine.Commands, parallelism: 4);
        using var store = DocumentStore.For(options =>
        {
            WaxStoreSetup.Configure(options, db.Engine.DataSource, [new HiveArModule()], mayChangeSchema: true);
            options.Events.Subscribe(rules);
        });
        var poller = new PendingEventPoller(
            db.Engine.DataSource,
            (pendingEventPk, cancellation) => db.Engine.Commands.DispatchAsync(
                new FirePendingEvent { PendingEventPk = pendingEventPk }, DerivedUuid.From("bakeoff:register"), "system", "register", cancellation: cancellation));

        var placed = await scenario.Place(principal: 1_000);
        var disputed = await scenario.Place(principal: 5_000);
        await scenario.Run(new OpenDispute { AccountPk = disputed });
        await scenario.Post([placed], 2_000_000);

        using var daemon = await store.BuildProjectionDaemonAsync();
        await daemon.StartAllAsync();
        var deadline = DateTime.UtcNow.AddSeconds(60);
        while (DateTime.UtcNow < deadline)
        {
            await poller.PollOnceAsync(CancellationToken.None);
            var tasks = await scenario.Scalar<long>("SELECT count(*) FROM ar.task");
            var queued = await scenario.Scalar<long>("SELECT count(*) FROM ar.account WHERE queue_code = 'dispute_review'");
            if (tasks == 2 && queued == 1)
            {
                break;
            }
            await Task.Delay(200);
        }
        await daemon.StopAllAsync();

        // Both new accounts got a first-contact task; the dispute reached the
        // review queue 30 simulated days later; the payoff marked the account paid.
        Assert.Equal(2L, await scenario.Scalar<long>("SELECT count(*) FROM ar.task WHERE task_type = 'first_contact'"));
        Assert.Equal("dispute_review", await scenario.Scalar<string>("SELECT queue_code FROM ar.account WHERE account_pk = $1", disputed));
        Assert.Equal("paid_in_full", await scenario.Scalar<string>("SELECT status FROM ar.account WHERE account_pk = $1", placed));
        // Every action names the event that triggered it, for the harness's rule latency.
        Assert.Equal(0L, await scenario.Scalar<long>(
            "SELECT count(*) FROM core.bakeoff_event_log action LEFT JOIN core.bakeoff_event_log trigger ON trigger.event_key = action.correlation_pk_ref " +
            "WHERE action.correlation_type = 'rule' AND trigger.event_key IS NULL"));
        Assert.True(await scenario.Scalar<long>("SELECT count(*) FROM core.bakeoff_event_log WHERE correlation_type = 'rule'") >= 6);
        Assert.Empty((await ChainVerifier.VerifyAllAsync(db.Engine.DataSource)).Problems);
    }
}
