using Marten;
using Npgsql;
using Wax.Commands;
using Wax.Store;

namespace Wax;

/// <summary>
/// The composition root: one data source, one Marten store, and the command
/// dispatcher with every module's handlers registered.
/// </summary>
public sealed class WaxEngine : IAsyncDisposable
{
    WaxEngine(NpgsqlDataSource dataSource, DocumentStore store, CommandDispatcher commands)
    {
        DataSource = dataSource;
        Store = store;
        Commands = commands;
    }

    public NpgsqlDataSource DataSource { get; }

    public DocumentStore Store { get; }

    public CommandDispatcher Commands { get; }

    /// <summary>Build the engine. With migrate, apply the schema first; that
    /// needs the owner's connection, never the runtime role's.</summary>
    public static async Task<WaxEngine> CreateAsync(
        string connectionString,
        IReadOnlyList<IWaxModule> modules,
        bool migrate = false,
        AppendMode? appendMode = null,
        CancellationToken cancellation = default)
    {
        var dataSource = NpgsqlDataSource.Create(connectionString);
        var store = DocumentStore.For(options => WaxStoreSetup.Configure(options, dataSource, modules, mayChangeSchema: migrate));
        if (migrate)
        {
            await WaxMigrator.MigrateAsync(dataSource, store, modules, cancellation);
        }
        var reasons = await ReasonCodes.LoadAsync(dataSource, cancellation);
        var commands = new CommandDispatcher(dataSource, store, reasons, appendMode ?? WaxSettings.AppendModeFromEnvironment());
        foreach (var module in modules)
        {
            module.RegisterHandlers(commands);
        }
        return new WaxEngine(dataSource, store, commands);
    }

    public async ValueTask DisposeAsync()
    {
        Store.Dispose();
        await DataSource.DisposeAsync();
    }
}

public static class WaxSettings
{
    /// <summary>WAX_APPEND=direct selects the direct append; anything else, Marten's.</summary>
    public static AppendMode AppendModeFromEnvironment() =>
        string.Equals(Environment.GetEnvironmentVariable("WAX_APPEND"), "direct", StringComparison.OrdinalIgnoreCase)
            ? AppendMode.Direct
            : AppendMode.Marten;

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
        return builder.ConnectionString;
    }
}
