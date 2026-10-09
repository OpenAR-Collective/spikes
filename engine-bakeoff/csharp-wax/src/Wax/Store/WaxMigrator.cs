using Npgsql;

namespace Wax.Store;

/// <summary>
/// Applies the schema as its owner: Wax's core schema, including the event
/// store, then each module's scripts. Each script runs once, in its own
/// transaction, and is recorded in core.schema_migration.
/// </summary>
public static class WaxMigrator
{
    const string Bookkeeping =
        "CREATE SCHEMA IF NOT EXISTS core; " +
        "CREATE TABLE IF NOT EXISTS core.schema_migration (" +
        "script_name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())";

    public static async Task MigrateAsync(NpgsqlDataSource dataSource, IReadOnlyList<IWaxModule> modules, CancellationToken cancellation = default)
    {
        await using (var command = dataSource.CreateCommand(Bookkeeping))
        {
            await command.ExecuteNonQueryAsync(cancellation);
        }
        await ApplyAsync(dataSource, EmbeddedScripts.From(typeof(WaxMigrator).Assembly, "Schema", "wax"), cancellation);
        foreach (var module in modules)
        {
            await ApplyAsync(dataSource, module.SchemaScripts, cancellation);
        }
    }

    static async Task ApplyAsync(NpgsqlDataSource dataSource, IEnumerable<SchemaScript> scripts, CancellationToken cancellation)
    {
        foreach (var script in scripts)
        {
            await using var connection = await dataSource.OpenConnectionAsync(cancellation);
            await using var transaction = await connection.BeginTransactionAsync(cancellation);
            await using (var check = new NpgsqlCommand(
                "INSERT INTO core.schema_migration (script_name) VALUES ($1) ON CONFLICT DO NOTHING", connection, transaction))
            {
                check.Parameters.AddWithValue(script.Name);
                if (await check.ExecuteNonQueryAsync(cancellation) == 0)
                {
                    continue;
                }
            }
            await using (var apply = new NpgsqlCommand(script.Sql, connection, transaction))
            {
                await apply.ExecuteNonQueryAsync(cancellation);
            }
            await transaction.CommitAsync(cancellation);
        }
    }
}
