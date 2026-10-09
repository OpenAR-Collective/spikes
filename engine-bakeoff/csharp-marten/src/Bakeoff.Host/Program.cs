using Bakeoff.Host;
using HiveAR;
using JasperFx.Events.Daemon;
using Marten;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Npgsql;
using Wax;
using Wax.Hashing;
using Wax.Rules;
using Wax.Store;

// The engine's command line, mirroring the Python side's management commands
// and answering the commands WORKLOADS.md asks of each side:
//   migrate          apply the schema as its owner
//   seed-demo        load the sample bucket registry
//   seed             load the bake-off's seed (--accounts N)
//   bench            run a workload: batch-payments, batch-accrual, or realtime
//   run-engine       the projection worker, the rule runner, and the register poller
//   run-projections  the projection worker alone (the default)
//   projections      show each projection's position
//   verify-chain     recompute every stream's hash chain
//   about            the runtime, library versions, and settings a result records

IReadOnlyList<IWaxModule> modules = [new HiveArModule()];
var connectionString = WaxSettings.ConnectionStringFromEnvironment();
var options = new Options(args);

switch (args.FirstOrDefault() ?? "run-projections")
{
    case "migrate":
    {
        await using var engine = await WaxEngine.CreateAsync(connectionString, modules, migrate: true);
        Console.WriteLine("The schema is current.");
        return 0;
    }
    case "seed-demo":
    {
        await using var dataSource = NpgsqlDataSource.Create(connectionString);
        await HiveArModule.SeedDemoAsync(dataSource);
        Console.WriteLine("3 bucket definitions loaded");
        return 0;
    }
    case "seed":
    {
        await using var engine = await WaxEngine.CreateAsync(connectionString, modules);
        return await Bench.SeedAsync(engine, options);
    }
    case "bench":
    {
        await using var engine = await WaxEngine.CreateAsync(connectionString, modules);
        return await Bench.RunAsync(engine, args.ElementAtOrDefault(1) ?? "", options);
    }
    case "verify-chain":
    {
        // One line of JSON, in the shape the Python side prints, for the harness.
        await using var dataSource = NpgsqlDataSource.Create(connectionString);
        var clock = System.Diagnostics.Stopwatch.StartNew();
        var report = await ChainVerifier.VerifyAllAsync(dataSource);
        Console.WriteLine(System.Text.Json.JsonSerializer.Serialize(new
        {
            workload = "verify-chain",
            aggregates = report.Streams,
            events = report.Events,
            problems = report.Problems.Count,
            ok = report.Problems.Count == 0,
            first_problems = report.Problems.Take(20).Select(problem => $"{problem.StreamId}: {problem.Problem}"),
            elapsed_s = Math.Round(clock.Elapsed.TotalSeconds, 3),
        }));
        return report.Problems.Count == 0 ? 0 : 1;
    }
    case "projections":
    {
        await using var engine = await WaxEngine.CreateAsync(connectionString, modules);
        var statistics = await engine.Store.Advanced.FetchEventStoreStatistics();
        Console.WriteLine($"events: {statistics.EventCount}, streams: {statistics.StreamCount}, last sequence: {statistics.EventSequenceNumber}");
        foreach (var shard in await engine.Store.Advanced.AllProjectionProgress())
        {
            Console.WriteLine($"{shard.ShardName}: {shard.Sequence}");
        }
        return 0;
    }
    case "about":
    {
        // What a result records about this side: runtime, library versions, and settings.
        await using var engine = await WaxEngine.CreateAsync(connectionString, modules);
        var storeOptions = engine.Store.Options;
        Console.WriteLine($".NET {Environment.Version}, {System.Runtime.InteropServices.RuntimeInformation.OSDescription}");
        Console.WriteLine($"Marten {typeof(DocumentStore).Assembly.GetName().Version}, Npgsql {typeof(NpgsqlConnection).Assembly.GetName().Version}");
        Console.WriteLine($"event append mode: {storeOptions.Events.AppendMode}; commands append through: {engine.Commands.AppendMode}");
        Console.WriteLine($"server GC: {System.Runtime.GCSettings.IsServerGC}, processors: {Environment.ProcessorCount}");
        return 0;
    }
    case "run-engine":
    {
        // The long-running services of WORKLOADS.md in one process. Marten's
        // daemon runs one agent per projection, so --projection-workers does
        // not apply here; it is accepted and recorded for symmetry.
        var secondsPerDay = options.Double("seconds-per-day", 2);
        var pollInterval = TimeSpan.FromMilliseconds(options.Int("poll-interval-ms", 250));
        var ruleWorkers = options.Int("rule-workers", 1);
        var engine = await WaxEngine.CreateAsync(connectionString, modules);
        var registerActor = DerivedUuid.From("bakeoff:register");
        var builder = Host.CreateApplicationBuilder();
        builder.Services
            .AddMarten(storeOptions =>
            {
                WaxStoreSetup.Configure(storeOptions, engine.DataSource, modules, mayChangeSchema: false);
                storeOptions.Events.Subscribe(new RuleRunner(HiveArRules.All(engine.DataSource, secondsPerDay), engine.Commands, ruleWorkers));
            })
            .AddAsyncDaemon(DaemonMode.Solo);
        builder.Services.AddHostedService(services => new PendingEventPoller(
            engine.DataSource,
            (pendingEventPk, cancellation) => engine.Commands.DispatchAsync(
                new FirePendingEvent { PendingEventPk = pendingEventPk }, registerActor, "system", "register", cancellation: cancellation),
            parallelism: ruleWorkers,
            pollInterval: pollInterval,
            logger: services.GetRequiredService<ILogger<PendingEventPoller>>()));
        Console.WriteLine($"run-engine: seconds-per-day {secondsPerDay}, poll-interval-ms {pollInterval.TotalMilliseconds}, rule-workers {ruleWorkers}, append {engine.Commands.AppendMode}");
        await builder.Build().RunAsync();
        await engine.DisposeAsync();
        return 0;
    }
    case "run-projections":
    {
        var builder = Host.CreateApplicationBuilder();
        var dataSource = NpgsqlDataSource.Create(connectionString);
        builder.Services
            .AddMarten(storeOptions => WaxStoreSetup.Configure(storeOptions, dataSource, modules, mayChangeSchema: false))
            .AddAsyncDaemon(DaemonMode.Solo);
        await builder.Build().RunAsync();
        return 0;
    }
    default:
        Console.Error.WriteLine(
            "usage: Bakeoff.Host [migrate | seed-demo | seed | bench <workload> | run-engine | run-projections | projections | verify-chain | about]");
        return 2;
}
