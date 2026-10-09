using HiveAR;
using JasperFx.Events.Daemon;
using Marten;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Npgsql;
using Wax;
using Wax.Store;

// The engine's command line, mirroring the Python side's management commands:
//   migrate          apply the schema as its owner
//   seed-demo        load the sample bucket registry
//   run-projections  keep the read models current (the default)
//   projections      show each projection's position
//   verify-chain     recompute every stream's hash chain
//   about            the runtime, library versions, and settings a result records

IReadOnlyList<IWaxModule> modules = [new HiveArModule()];
var connectionString = WaxSettings.ConnectionStringFromEnvironment();

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
    case "verify-chain":
    {
        await using var dataSource = NpgsqlDataSource.Create(connectionString);
        var report = await ChainVerifier.VerifyAllAsync(dataSource);
        foreach (var problem in report.Problems.Take(50))
        {
            Console.WriteLine($"{problem.StreamId}: {problem.Problem}");
        }
        Console.WriteLine($"{report.Streams} streams and {report.Events} events checked; {report.Problems.Count} problems.");
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
        var options = engine.Store.Options;
        Console.WriteLine($".NET {Environment.Version}, {System.Runtime.InteropServices.RuntimeInformation.OSDescription}");
        Console.WriteLine($"Marten {typeof(DocumentStore).Assembly.GetName().Version}, Npgsql {typeof(NpgsqlConnection).Assembly.GetName().Version}");
        Console.WriteLine($"event append mode: {options.Events.AppendMode}");
        Console.WriteLine($"server GC: {System.Runtime.GCSettings.IsServerGC}, processors: {Environment.ProcessorCount}");
        return 0;
    }
    case "run-projections":
    {
        var builder = Host.CreateApplicationBuilder();
        var dataSource = NpgsqlDataSource.Create(connectionString);
        builder.Services
            .AddMarten(options => WaxStoreSetup.Configure(options, dataSource, modules, mayChangeSchema: false))
            .AddAsyncDaemon(DaemonMode.Solo);
        await builder.Build().RunAsync();
        return 0;
    }
    default:
        Console.Error.WriteLine("usage: Bakeoff.Host [migrate | seed-demo | run-projections | projections | verify-chain | about]");
        return 2;
}
