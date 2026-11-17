using Microsoft.Extensions.Logging;
using Npgsql;
using Wax.Commands;
using Wax.Events;
using Wax.Projections;
using Wax.Store;

namespace Wax;

/// <summary>
/// The composition root: one data source, the command dispatcher with every
/// module's handlers registered, and every module's projections.
/// </summary>
public sealed class WaxEngine : IAsyncDisposable
{
    WaxEngine(NpgsqlDataSource dataSource, CommandDispatcher commands, IReadOnlyList<Projection> projections)
    {
        DataSource = dataSource;
        Commands = commands;
        Projections = projections;
    }

    public NpgsqlDataSource DataSource { get; }

    public CommandDispatcher Commands { get; }

    public IReadOnlyList<Projection> Projections { get; }

    /// <summary>A reader over every module's projections. Each reader keeps its own high-water mark.</summary>
    public ProjectionReader CreateReader(ILogger? logger = null) => new(DataSource, Projections, logger);

    /// <summary>Build the engine. With migrate, apply the schema first; that
    /// needs the owner's connection, never the runtime role's.</summary>
    public static async Task<WaxEngine> CreateAsync(
        string connectionString,
        IReadOnlyList<IWaxModule> modules,
        bool migrate = false,
        CancellationToken cancellation = default,
        TimeProvider? clock = null)
    {
        var dataSource = NpgsqlDataSource.Create(connectionString);
        WaxEventTypes.Register(modules.Select(module => module.GetType().Assembly).Append(typeof(WaxEvent).Assembly).Distinct());
        if (migrate)
        {
            await WaxMigrator.MigrateAsync(dataSource, modules, cancellation);
        }
        var reasons = await ReasonCodes.LoadAsync(dataSource, cancellation);
        var commands = new CommandDispatcher(dataSource, reasons, clock);
        foreach (var module in modules)
        {
            module.RegisterHandlers(commands);
        }
        return new WaxEngine(dataSource, commands, modules.SelectMany(module => module.Projections).ToList());
    }

    public async ValueTask DisposeAsync() => await DataSource.DisposeAsync();
}

public static class WaxSettings
{
    /// <summary>The connection named by the same DATABASE_* variables the Python side reads.</summary>
    public static string ConnectionStringFromEnvironment(string? databaseOverride = null)
    {
        var builder = new NpgsqlConnectionStringBuilder
        {
            Host = Environment.GetEnvironmentVariable("DATABASE_HOST") ?? "localhost",
            Port = int.Parse(Environment.GetEnvironmentVariable("DATABASE_PORT") ?? "5432", System.Globalization.CultureInfo.InvariantCulture),
            Database = databaseOverride ?? Environment.GetEnvironmentVariable("DATABASE_NAME") ?? "hivear",
            Username = Environment.GetEnvironmentVariable("DATABASE_USER") ?? "hivear",
            Password = Environment.GetEnvironmentVariable("DATABASE_PASSWORD") ?? "local-evaluation-only",
        };
        if (int.TryParse(Environment.GetEnvironmentVariable("DATABASE_MAX_POOL_SIZE"), out var poolSize))
        {
            builder.MaxPoolSize = poolSize;
        }
        // Two driver settings that are csharp-wax's own, after round 2's
        // diagnostic pass found that Npgsql's defaults, not C#, made its
        // database cost per command higher than the Python side's. No Reset On
        // Close stops Npgsql sending DISCARD ALL when a pooled connection is
        // returned, so a session keeps its cached plans, the insert trigger's
        // and the foreign-key checks' among them. Max Auto Prepare 20 lets it
        // prepare on the server the statements it sees most often, as psycopg
        // does after five uses. Every connection this process opens carries
        // only settings it chose, so nothing needs a reset between uses.
        //
        // Each variable overrides its setting; DATABASE_NO_RESET_ON_CLOSE=false
        // with DATABASE_MAX_AUTO_PREPARE=0 runs Npgsql's defaults, as every
        // csharp-wax run before this change did. DATABASE_PLAN_CACHE_MODE sets
        // PostgreSQL's plan_cache_mode for the session, and
        // DATABASE_SYNCHRONOUS_COMMIT its synchronous_commit, each for a
        // diagnostic. A value that is set but not understood stops the
        // process, so a typo cannot run the default under another name.
        builder.NoResetOnClose = true;
        builder.MaxAutoPrepare = 20;
        var noReset = Setting("DATABASE_NO_RESET_ON_CLOSE");
        if (noReset is not null)
        {
            builder.NoResetOnClose = noReset.ToLowerInvariant() switch
            {
                "1" or "true" or "yes" or "on" => true,
                "0" or "false" or "no" or "off" => false,
                _ => throw new InvalidOperationException($"DATABASE_NO_RESET_ON_CLOSE must be true or false, not '{noReset}'."),
            };
        }
        var maxAutoPrepare = Setting("DATABASE_MAX_AUTO_PREPARE");
        if (maxAutoPrepare is not null)
        {
            builder.MaxAutoPrepare = int.TryParse(maxAutoPrepare, System.Globalization.NumberStyles.None,
                System.Globalization.CultureInfo.InvariantCulture, out var count)
                ? count
                : throw new InvalidOperationException($"DATABASE_MAX_AUTO_PREPARE must be a whole number, not '{maxAutoPrepare}'.");
        }
        var options = SessionOptions(Setting("DATABASE_PLAN_CACHE_MODE"), Setting("DATABASE_SYNCHRONOUS_COMMIT"));
        if (options is not null)
        {
            builder.Options = options;
        }
        return builder.ConnectionString;
    }

    /// <summary>The PostgreSQL settings a session starts with, as Npgsql's
    /// Options, or null for none. Both are for diagnostics only: planCacheMode
    /// sets plan_cache_mode, and synchronousCommit sets synchronous_commit,
    /// which with off lets a commit return before its WAL reaches the disk, so
    /// it is never a setting a measured configuration runs with. A value that
    /// is not understood stops the process.</summary>
    public static string? SessionOptions(string? planCacheMode, string? synchronousCommit)
    {
        var options = new List<string>();
        if (planCacheMode is not null)
        {
            options.Add(planCacheMode switch
            {
                "auto" or "force_custom_plan" or "force_generic_plan" => $"-c plan_cache_mode={planCacheMode}",
                _ => throw new InvalidOperationException(
                    $"DATABASE_PLAN_CACHE_MODE must be auto, force_custom_plan, or force_generic_plan, not '{planCacheMode}'."),
            });
        }
        if (synchronousCommit is not null)
        {
            options.Add(synchronousCommit switch
            {
                "on" or "off" => $"-c synchronous_commit={synchronousCommit}",
                _ => throw new InvalidOperationException(
                    $"DATABASE_SYNCHRONOUS_COMMIT must be on or off, not '{synchronousCommit}'."),
            });
        }
        return options.Count > 0 ? string.Join(' ', options) : null;
    }

    /// <summary>The driver settings a connection string carries, in the words a
    /// result records.</summary>
    public static string DriverSettings(string connectionString)
    {
        var builder = new NpgsqlConnectionStringBuilder(connectionString);
        return $"Npgsql No Reset On Close {builder.NoResetOnClose}, Max Auto Prepare {builder.MaxAutoPrepare}, " +
            $"options '{builder.Options ?? ""}'";
    }

    /// <summary>The variable's trimmed value, or null when it is unset or empty,
    /// as build.sh passes an unset variable.</summary>
    static string? Setting(string name) =>
        Environment.GetEnvironmentVariable(name)?.Trim() is { Length: > 0 } value ? value : null;
}
