using System.Text.RegularExpressions;
using Npgsql;

namespace Wax;

/// <summary>
/// A diagnostic: how often PostgreSQL chose a generic or a custom plan for the
/// statements a data source's connections have prepared on the server, read
/// from pg_prepared_statements, connection by connection. A session sees only
/// its own prepared statements, so the read holds every physical connection
/// the pool has open at once, counted in pg_stat_activity by this process's
/// client address and application name, and reads each one with its backend
/// PID, so a connection read twice would show. Holding the pool stalls
/// anything else that uses it, so the read belongs after a measured window.
/// A connection that Npgsql resets with DISCARD ALL, its default, shows
/// nothing, since the reset deallocates its prepared statements; with Max Auto
/// Prepare alone the lighter reset keeps them, and with No Reset On Close
/// nothing is reset. The plans of the statements inside a trigger or a
/// foreign-key check are cached apart and never show here, although
/// plan_cache_mode governs them too.
/// </summary>
public static partial class PlanChoices
{
    /// <summary>One prepared statement on one connection. Npgsql names the
    /// statements it prepares itself _auto0 and so on, so the text is what
    /// identifies one.</summary>
    public sealed record Statement(string Text, long GenericPlans, long CustomPlans);

    /// <summary>One physical connection and its prepared statements, the most executed first.</summary>
    public sealed record Connection(int Pid, int Prepared, IReadOnlyList<Statement> Statements);

    const string CountSql =
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND backend_type = 'client backend' " +
        "AND client_addr IS NOT DISTINCT FROM inet_client_addr() AND application_name = current_setting('application_name')";

    const string ReadSql =
        "SELECT pg_backend_pid(), statement, generic_plans, custom_plans FROM pg_prepared_statements " +
        "UNION ALL SELECT pg_backend_pid(), NULL, NULL, NULL WHERE NOT EXISTS (SELECT FROM pg_prepared_statements)";

    /// <summary>Reads at most maxConnections connections, each with at most
    /// limit statements; a statement's text is shortened to 100 characters.</summary>
    public static async Task<IReadOnlyList<Connection>> ReadAsync(NpgsqlDataSource dataSource, int limit = 12,
        int maxConnections = 128, CancellationToken cancellation = default)
    {
        var held = new List<NpgsqlConnection>();
        var result = new List<Connection>();
        try
        {
            held.Add(await dataSource.OpenConnectionAsync(cancellation));
            long open;
            await using (var count = new NpgsqlCommand(CountSql, held[0]))
            {
                open = (long)(await count.ExecuteScalarAsync(cancellation))!;
            }
            // While some are held, the pool hands out the idle ones before it
            // opens a new one, so holding as many as are open reaches them all.
            while (held.Count < Math.Min(open, maxConnections))
            {
                held.Add(await dataSource.OpenConnectionAsync(cancellation));
            }
            foreach (var connection in held)
            {
                var pid = 0;
                var statements = new List<Statement>();
                await using (var read = new NpgsqlCommand(ReadSql, connection))
                await using (var rows = await read.ExecuteReaderAsync(cancellation))
                {
                    while (await rows.ReadAsync(cancellation))
                    {
                        pid = rows.GetInt32(0);
                        if (!rows.IsDBNull(1))
                        {
                            statements.Add(new Statement(Shorten(rows.GetString(1)), rows.GetInt64(2), rows.GetInt64(3)));
                        }
                    }
                }
                result.Add(new Connection(pid, statements.Count, statements
                    .OrderByDescending(statement => statement.GenericPlans + statement.CustomPlans)
                    .ThenBy(statement => statement.Text, StringComparer.Ordinal)
                    .Take(limit)
                    .ToList()));
            }
        }
        finally
        {
            foreach (var connection in held)
            {
                await connection.DisposeAsync();
            }
        }
        return result;
    }

    static string Shorten(string sql)
    {
        var text = Whitespace().Replace(sql, " ").Trim();
        return text.Length <= 100 ? text : text[..100];
    }

    [GeneratedRegex(@"\s+")]
    private static partial Regex Whitespace();
}
