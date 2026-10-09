using System.Diagnostics;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using HiveAR;
using Wax;
using Wax.Batches;
using Wax.Commands;
using Wax.Hashing;
using Wax.Store;

namespace Bakeoff.Host;

/// <summary>
/// The C# side's driver for the workloads in WORKLOADS.md: the seed, the two
/// batch jobs, and the real-time mix. It sends commands in process through the
/// command dispatcher and prints one line of JSON. Everything else is measured
/// by the harness from outside. Foundation-written.
/// </summary>
static class Bench
{
    static readonly Guid BatchActor = DerivedUuid.From("bakeoff:batch");
    static readonly Guid InboundActor = DerivedUuid.From("bakeoff:inbound");

    public static async Task<int> SeedAsync(WaxEngine engine, Options options)
    {
        var accounts = options.Long("accounts", 500_000);
        var elapsed = await Seeder.SeedAsync(engine.DataSource, accounts, engine.Projections);
        Print(new { workload = "seed", accounts, elapsed_s = Math.Round(elapsed.TotalSeconds, 3) });
        return 0;
    }

    public static async Task<int> RunAsync(WaxEngine engine, string workload, Options options)
    {
        var workers = options.Int("workers", 1);
        var tally = new Tally();
        var retriedBefore = engine.Commands.ConflictRetries;
        var clock = Stopwatch.StartNew();
        Guid runPk;
        TimeSpan? measuredWindow = null;
        switch (workload)
        {
            case "batch-payments":
            {
                var runner = new BatchRunner(engine.DataSource, "ar.account", "account_pk");
                runPk = await runner.CreateRunAsync("payment_file", chunkSize: 1_000);
                var receivedAt = new DateTimeOffset(2026, 10, 1, 6, 0, 0, TimeSpan.Zero);
                await runner.RunAsync(runPk, workers, Environment.MachineName, accountPk => tally.Measure(() =>
                    engine.Commands.DispatchAsync(new PostPayment
                    {
                        PaymentPk = DerivedUuid.From($"bakeoff:payment:{runPk}:{accountPk}"),
                        AccountPks = [accountPk],
                        AmountMinorUnits = 2_500,
                        CurrencyCode = "USD",
                        AllocationMethod = "oldest_first",
                        ReceivedAt = receivedAt,
                    }, BatchActor, "batch", "batch")));
                break;
            }
            case "batch-accrual":
            {
                var runner = new BatchRunner(engine.DataSource, "ar.account", "account_pk");
                runPk = await runner.CreateRunAsync("interest_accrual", chunkSize: 1_000);
                await runner.RunAsync(runPk, workers, Environment.MachineName, accountPk => tally.Measure(() =>
                    engine.Commands.DispatchAsync(new AccrueInterest
                    {
                        AccountPk = accountPk,
                        AccrualDate = new DateOnly(2026, 10, 1),
                        AnnualRateBasisPoints = 1_800,
                    }, BatchActor, "batch", "batch")));
                break;
            }
            case "batch-payments-bulk":
            {
                var runner = new BatchRunner(engine.DataSource, "ar.account", "account_pk");
                runPk = await runner.CreateRunAsync("payment_file_bulk", chunkSize: 1_000);
                var receivedAt = new DateTimeOffset(2026, 10, 1, 6, 0, 0, TimeSpan.Zero);
                var bulk = new BulkWriter(engine.DataSource, await ReasonCodes.LoadAsync(engine.DataSource));
                var run = runPk;
                await runner.RunChunksAsync(runPk, workers, Environment.MachineName, keys => tally.MeasureChunk(
                    () => bulk.PostPaymentsAsync(keys, run, 2_500, receivedAt, BatchActor, CancellationToken.None),
                    keys.Select(accountPk => (Func<Task>)(() => engine.Commands.DispatchAsync(new PostPayment
                    {
                        PaymentPk = DerivedUuid.From($"bakeoff:payment:{run}:{accountPk}"),
                        AccountPks = [accountPk],
                        AmountMinorUnits = 2_500,
                        CurrencyCode = "USD",
                        AllocationMethod = "oldest_first",
                        ReceivedAt = receivedAt,
                    }, BatchActor, "batch", "batch"))).ToList()));
                break;
            }
            case "placement":
            case "placement-bulk":
            {
                var placements = options.Long("accounts", 100_000);
                runPk = Guid.NewGuid();
                var run = runPk;
                var bulk = workload == "placement-bulk"
                    ? new BulkWriter(engine.DataSource, await ReasonCodes.LoadAsync(engine.DataSource))
                    : null;
                long nextChunk = -1;
                await Task.WhenAll(Enumerable.Range(0, workers).Select(_ => Task.Run(async () =>
                {
                    // Chunks of 1,000 consecutive i, taken in order. There are
                    // no existing rows to divide, so no batch tables are needed.
                    while (true)
                    {
                        var start = Interlocked.Increment(ref nextChunk) * 1_000;
                        if (start >= placements)
                        {
                            return;
                        }
                        var chunk = Enumerable.Range(0, (int)Math.Min(1_000, placements - start))
                            .Select(offset => Placement(run, start + offset))
                            .ToList();
                        var singles = chunk.Select(account => (Func<Task>)(() => engine.Commands.DispatchAsync(new PlaceAccount
                        {
                            AccountPk = account.Pk,
                            ClientAccountNumber = account.ClientAccountNumber,
                            DebtType = "personal_loan",
                            ContractType = "third_party_contingency",
                            CurrencyCode = "USD",
                            ListDate = account.ListDate,
                            ItemizationDate = account.ItemizationDate,
                            InterestAccruedThrough = SeedAccount.AccruedThrough,
                            OpeningBalances = account.Balances,
                        }, BatchActor, "batch", "batch"))).ToList();
                        if (bulk is null)
                        {
                            foreach (var single in singles)
                            {
                                await tally.Measure(single);
                            }
                        }
                        else
                        {
                            await tally.MeasureChunk(() => bulk.PlaceAccountsAsync(chunk, BatchActor, CancellationToken.None), singles);
                        }
                    }
                })));
                break;
            }
            case "realtime":
                (runPk, measuredWindow) = await RealtimeAsync(engine, workers, options, tally);
                break;
            default:
                Console.Error.WriteLine($"unknown workload {workload}");
                return 2;
        }
        clock.Stop();
        Print(new
        {
            workload,
            workers,
            sent = tally.Sent,
            sent_total = tally.SentTotal,
            succeeded = tally.Succeeded,
            rejected = tally.Rejected,
            failed = tally.Failed,
            retried = engine.Commands.ConflictRetries - retriedBefore,
            // A batch is timed from start to end; the real-time mix by its measured window.
            elapsed_s = Math.Round((measuredWindow ?? clock.Elapsed).TotalSeconds, 3),
            latency_ms = tally.Percentiles(),
            run_pk = runPk,
            transactions = tally.Transactions,
            fallback_chunks = tally.FallbackChunks,
            // No Marten: commands append to core.event with plain SQL.
            append = "wax",
            hashing = EventHashing.Enabled ? "on" : "off",
        });
        return tally.Failed == 0 ? 0 : 1;
    }

    static async Task<(Guid RunPk, TimeSpan Window)> RealtimeAsync(WaxEngine engine, int workers, Options options, Tally tally)
    {
        var warmup = TimeSpan.FromSeconds(options.Double("warmup", 30));
        var duration = TimeSpan.FromSeconds(options.Double("duration", 120));
        var randomSeed = options.Text("random-seed", "1");
        var start = DateTimeOffset.UtcNow;
        var runPk = DerivedUuid.From($"bakeoff:realtime:{randomSeed}:{start.UtcDateTime.ToString("O", CultureInfo.InvariantCulture)}");
        long seedAccounts;
        await using (var count = engine.DataSource.CreateCommand("SELECT count(*) FROM ar.account WHERE client_account_number LIKE 'BK-%'"))
        {
            seedAccounts = (long)(await count.ExecuteScalarAsync())!;
        }
        var measureFrom = Stopwatch.GetTimestamp() + (long)(warmup.TotalSeconds * Stopwatch.Frequency);
        var stopAt = measureFrom + (long)(duration.TotalSeconds * Stopwatch.Frequency);
        tally.MeasureFrom = measureFrom;

        await Task.WhenAll(Enumerable.Range(0, workers).Select(worker => Task.Run(async () =>
        {
            var random = new Random(BitConverter.ToInt32(SHA256.HashData(Encoding.UTF8.GetBytes($"{randomSeed}:{worker}")), 0));
            long payments = 0, disputes = 0, placements = 0;
            while (Stopwatch.GetTimestamp() < stopAt)
            {
                var roll = random.Next(100);
                ICommand command;
                if (roll < 85)
                {
                    var account = DerivedUuid.From($"bakeoff:account:{random.NextInt64(seedAccounts)}");
                    var payoff = random.Next(30) == 0;
                    command = new PostPayment
                    {
                        PaymentPk = DerivedUuid.From($"bakeoff:live-payment:{runPk}:{worker}:{payments++}"),
                        AccountPks = [account],
                        AmountMinorUnits = payoff ? 2_000_000 : random.Next(500, 5_001),
                        CurrencyCode = "USD",
                    };
                }
                else if (roll < 95)
                {
                    command = new OpenDispute
                    {
                        AccountPk = DerivedUuid.From($"bakeoff:account:{random.NextInt64(seedAccounts)}"),
                        DisputePk = DerivedUuid.From($"bakeoff:live-dispute:{runPk}:{worker}:{disputes++}"),
                    };
                }
                else
                {
                    var n = placements++;
                    var balances = SeedAccount.For(n);
                    command = new PlaceAccount
                    {
                        AccountPk = DerivedUuid.From($"bakeoff:live-account:{runPk}:{worker}:{n}"),
                        ClientAccountNumber = $"LV-{worker}-{n}",
                        DebtType = "personal_loan",
                        ContractType = "third_party_contingency",
                        CurrencyCode = "USD",
                        ListDate = new DateOnly(2026, 9, 1),
                        ItemizationDate = new DateOnly(2026, 8, 2),
                        InterestAccruedThrough = new DateOnly(2026, 9, 30),
                        OpeningBalances = balances.Balances,
                    };
                }
                await tally.Measure(() => engine.Commands.DispatchAsync(command, InboundActor, "integration", "inbound"));
            }
        })));
        return (runPk, duration);
    }

    /// <summary>Placement account i of a run, as WORKLOADS.md defines it.</summary>
    static SeedAccount Placement(Guid runPk, long i) => SeedAccount.For(i) with
    {
        Pk = DerivedUuid.From($"bakeoff:placement:{runPk}:{i}"),
        ClientAccountNumber = $"PL-{i:D9}",
    };

    static void Print(object value) => Console.WriteLine(JsonSerializer.Serialize(value));

    /// <summary>Counts outcomes and keeps every latency, per thread, without contention.</summary>
    sealed class Tally
    {
        readonly ThreadLocal<List<double>> latencies = new(() => [], trackAllValues: true);
        long sent, succeeded, rejected, failed, sentTotal, transactions, fallbackChunks;

        public long MeasureFrom { get; set; }

        public long Sent => sent;

        /// <summary>Transactions the driver committed: one per succeeded command, or one per bulk chunk.</summary>
        public long Transactions => transactions;

        /// <summary>Bulk chunks that failed and were run again one command at a time.</summary>
        public long FallbackChunks => fallbackChunks;

        /// <summary>Every command sent, the warm-up's included, for CPU per command over the whole run.</summary>
        public long SentTotal => sentTotal;
        public long Succeeded => succeeded;
        public long Rejected => rejected;
        public long Failed => failed;

        public async Task Measure(Func<Task> send)
        {
            var started = Stopwatch.GetTimestamp();
            var outcome = 0;
            try
            {
                await send();
            }
            catch (CommandRejectedException)
            {
                outcome = 1;
            }
            catch (Exception error)
            {
                outcome = 2;
                if (Interlocked.Read(ref failed) < 5)
                {
                    Console.Error.WriteLine(error);
                }
            }
            Interlocked.Increment(ref sentTotal);
            if (started < MeasureFrom)
            {
                // A warm-up command: run, not counted.
                return;
            }
            latencies.Value!.Add(Stopwatch.GetElapsedTime(started).TotalMilliseconds);
            Interlocked.Increment(ref sent);
            switch (outcome)
            {
                case 0: Interlocked.Increment(ref succeeded); Interlocked.Increment(ref transactions); break;
                case 1: Interlocked.Increment(ref rejected); break;
                default: Interlocked.Increment(ref failed); break;
            }
        }

        /// <summary>One bulk chunk: a single transaction for many commands. Its
        /// latency is the transaction's, and each account counts as one command.
        /// A chunk that fails is run again one command at a time.</summary>
        public async Task MeasureChunk(Func<Task<BulkOutcome>> bulk, IReadOnlyList<Func<Task>> singles)
        {
            var started = Stopwatch.GetTimestamp();
            BulkOutcome outcome;
            try
            {
                outcome = await bulk();
            }
            catch (Exception error)
            {
                if (Interlocked.Increment(ref fallbackChunks) <= 3)
                {
                    Console.Error.WriteLine($"bulk chunk failed, running it one command at a time: {error.Message}");
                }
                foreach (var single in singles)
                {
                    await Measure(single);
                }
                return;
            }
            latencies.Value!.Add(Stopwatch.GetElapsedTime(started).TotalMilliseconds);
            var commands = outcome.Succeeded + outcome.Rejected + outcome.Skipped;
            Interlocked.Add(ref sentTotal, commands);
            Interlocked.Add(ref sent, commands);
            // A key that already took effect is answered as success, as on the single-command path.
            Interlocked.Add(ref succeeded, outcome.Succeeded + outcome.Skipped);
            Interlocked.Add(ref rejected, outcome.Rejected);
            Interlocked.Increment(ref transactions);
        }

        public object Percentiles()
        {
            var all = latencies.Values.SelectMany(list => list).Order().ToArray();
            double At(double fraction) => all.Length == 0 ? 0 : Math.Round(all[Math.Min(all.Length - 1, (int)Math.Ceiling(fraction * all.Length) - 1)], 3);
            return new { p50 = At(0.50), p95 = At(0.95), p99 = At(0.99), max = all.Length == 0 ? 0 : Math.Round(all[^1], 3) };
        }
    }
}

/// <summary>--name value pairs from the command line.</summary>
sealed class Options(string[] args)
{
    readonly Dictionary<string, string> values = Parse(args);

    static Dictionary<string, string> Parse(string[] args)
    {
        var parsed = new Dictionary<string, string>();
        for (var index = 0; index < args.Length - 1; index++)
        {
            if (args[index].StartsWith("--", StringComparison.Ordinal))
            {
                parsed[args[index][2..]] = args[index + 1];
            }
        }
        return parsed;
    }

    public string Text(string name, string fallback) => values.GetValueOrDefault(name, fallback);

    public int Int(string name, int fallback) => values.TryGetValue(name, out var value) ? int.Parse(value, CultureInfo.InvariantCulture) : fallback;

    public long Long(string name, long fallback) => values.TryGetValue(name, out var value) ? long.Parse(value, CultureInfo.InvariantCulture) : fallback;

    public double Double(string name, double fallback) => values.TryGetValue(name, out var value) ? double.Parse(value, CultureInfo.InvariantCulture) : fallback;
}
