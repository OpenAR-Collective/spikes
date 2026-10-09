using Npgsql;
using Wax.Store;

namespace Wax.Commands;

/// <summary>
/// A command's queued writes, events and state alike, sent as one Npgsql batch
/// of plain SQL when the handler returns, inside the command's transaction.
/// </summary>
internal sealed class WriteQueue(NpgsqlConnection connection, NpgsqlTransaction transaction) : IAsyncDisposable
{
    readonly NpgsqlBatch batch = new(connection, transaction);

    /// <summary>A state write, with ? placeholders.</summary>
    public void Sql(string sql, object[] values) => batch.BatchCommands.Add(SqlText.Command(sql, values));

    /// <summary>One event, already shaped and hashed.</summary>
    public void Append(NewEvent e) => batch.BatchCommands.Add(EventStore.InsertCommand(e));

    public async Task FlushAsync(CancellationToken cancellation)
    {
        if (batch.BatchCommands.Count > 0)
        {
            await batch.ExecuteNonQueryAsync(cancellation);
        }
    }

    public ValueTask DisposeAsync() => batch.DisposeAsync();
}
