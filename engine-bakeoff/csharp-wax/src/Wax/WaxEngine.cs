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
        // Two driver settings for the round 2 diagnostic pass, each applied
        // only when its variable is set, so the default connection string is
        // unchanged. No Reset On Close stops Npgsql sending DISCARD ALL when a
        // pooled connection is returned; Max Auto Prepare lets it prepare the
        // statements it sees most often on the server, as psycopg does. A
        // value that is set but not understood stops the process, so a typo
        // cannot run the default under another name.
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
        return builder.ConnectionString;
    }

    /// <summary>The variable's trimmed value, or null when it is unset or empty,
    /// as build.sh passes an unset variable.</summary>
    static string? Setting(string name) =>
        Environment.GetEnvironmentVariable(name)?.Trim() is { Length: > 0 } value ? value : null;
}
