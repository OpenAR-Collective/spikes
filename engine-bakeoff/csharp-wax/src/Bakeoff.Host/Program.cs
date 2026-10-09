using Bakeoff.Host;
using HiveAR;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Npgsql;
using Wax;
using Wax.Hashing;
using Wax.Projections;
using Wax.Rules;
using Wax.Store;

// The engine's command line, mirroring the Python side's management commands
// and answering the commands WORKLOADS.md asks of each side:
//   migrate          apply the schema as its owner
//   seed-demo        load the sample bucket registry
//   seed             load the bake-off's seed (--accounts N)
//   bench            run a workload: batch-payments, batch-accrual, batch-payments-bulk,
//                    placement, placement-bulk, or realtime
//   run-engine       the projection worker, the rule runner, and the register poller
//   run-projections  the projection reader alone (the default)
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
        // Each projection's and rule runner partition's position, and the head.
        await using var engine = await WaxEngine.CreateAsync(connectionString, modules);
        var reader = engine.CreateReader();
        Console.WriteLine($"sequence position: {await reader.SequencePositionAsync()}");
        foreach (var row in await reader.StatusAsync())
        {
            var health = row.FailedAt is null ? "running" : $"stopped at {row.FailedGlobalSequenceNumber}: {row.FailureKind}, {row.FailureDetail}";
            Console.WriteLine($"{row.Projection}: {row.Position}, {health}{(row.DefinitionMatches ? "" : ", definition differs")}");
        }
        await using var rules = engine.DataSource.CreateCommand(
            "SELECT projection_name, last_global_sequence_number FROM core.projection_checkpoint WHERE projection_name LIKE 'wax.rules:%' ORDER BY 1");
        await using var rows = await rules.ExecuteReaderAsync();
        while (await rows.ReadAsync())
        {
            Console.WriteLine($"{rows.GetString(0)}: {rows.GetInt64(1)}");
        }
        return 0;
    }
    case "about":
    {
        // What a result records about this side: runtime, library versions, and settings.
        Console.WriteLine($".NET {Environment.Version}, {System.Runtime.InteropServices.RuntimeInformation.OSDescription}");
        Console.WriteLine($"Npgsql {typeof(NpgsqlConnection).Assembly.GetName().Version}; no Marten: events append to core.event with plain SQL; hashing: {(EventHashing.Enabled ? "on" : "off")}");
        Console.WriteLine($"server GC: {System.Runtime.GCSettings.IsServerGC}, processors: {Environment.ProcessorCount}");
        return 0;
    }
    case "run-engine":
    {
        // The long-running services of WORKLOADS.md in one process: J projection
        // readers sharing the projections through their checkpoint rows, K rule
        // runner partitions, and the register poller with K workers.
        var secondsPerDay = options.Double("seconds-per-day", 2);
        var pollInterval = TimeSpan.FromMilliseconds(options.Int("poll-interval-ms", 250));
        var ruleWorkers = options.Int("rule-workers", 1);
        var projectionWorkers = options.Int("projection-workers", 1);
        var engine = await WaxEngine.CreateAsync(connectionString, modules);
        var registerActor = DerivedUuid.From("bakeoff:register");
        var builder = Host.CreateApplicationBuilder();
        for (var j = 0; j < projectionWorkers; j++)
        {
            builder.Services.AddSingleton<IHostedService>(services =>
            {
                var reader = engine.CreateReader(services.GetRequiredService<ILogger<ProjectionReader>>());
                return new Loop(token => reader.RunAsync(pollInterval, token));
            });
        }
        var rules = HiveArRules.All(engine.DataSource, secondsPerDay);
        for (var k = 0; k < ruleWorkers; k++)
        {
            var partition = k;
            builder.Services.AddSingleton<IHostedService>(services =>
            {
                var runner = new RuleRunner(engine.DataSource, rules, engine.Commands, partition, ruleWorkers,
                    logger: services.GetRequiredService<ILogger<RuleRunner>>());
                return new Loop(token => runner.RunAsync(pollInterval, token));
            });
        }
        builder.Services.AddHostedService(services => new PendingEventPoller(
            engine.DataSource,
            (pendingEventPk, cancellation) => engine.Commands.DispatchAsync(
                new FirePendingEvent { PendingEventPk = pendingEventPk }, registerActor, "system", "register", cancellation: cancellation),
            parallelism: ruleWorkers,
            pollInterval: pollInterval,
            logger: services.GetRequiredService<ILogger<PendingEventPoller>>()));
        Console.WriteLine($"run-engine: seconds-per-day {secondsPerDay}, poll-interval-ms {pollInterval.TotalMilliseconds}, rule-workers {ruleWorkers}, projection-workers {projectionWorkers}, hashing {(EventHashing.Enabled ? "on" : "off")}");
        await builder.Build().RunAsync();
        await engine.DisposeAsync();
        return 0;
    }
    case "run-projections":
    {
        // The projection reader alone, for the worker service.
        var pollInterval = TimeSpan.FromMilliseconds(options.Int("poll-interval-ms", 250));
        var engine = await WaxEngine.CreateAsync(connectionString, modules);
        var builder = Host.CreateApplicationBuilder();
        builder.Services.AddSingleton<IHostedService>(services =>
        {
            var reader = engine.CreateReader(services.GetRequiredService<ILogger<ProjectionReader>>());
            return new Loop(token => reader.RunAsync(pollInterval, token));
        });
        await builder.Build().RunAsync();
        await engine.DisposeAsync();
        return 0;
    }
    default:
        Console.Error.WriteLine(
            "usage: Bakeoff.Host [migrate | seed-demo | seed | bench <workload> | run-engine | run-projections | projections | verify-chain | about]");
        return 2;
}

namespace Bakeoff.Host
{
    /// <summary>A long-running loop as a hosted service, stopped with the host.</summary>
    sealed class Loop(Func<CancellationToken, Task> run) : BackgroundService
    {
        protected override Task ExecuteAsync(CancellationToken stoppingToken) => run(stoppingToken);
    }
}
