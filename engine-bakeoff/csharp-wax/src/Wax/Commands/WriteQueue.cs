using Npgsql;
using Wax.Store;

namespace Wax.Commands;

/// <summary>
/// A command's queued writes, events and state alike, sent as one Npgsql batch
/// of plain SQL when the handler returns, inside the command's transaction.
/// In the multi-row mode a command's events go out as one INSERT, in the order
/// they were recorded, at the place in the batch where the first of them was
/// queued; otherwise each event is its own statement.
/// </summary>
internal sealed class WriteQueue(NpgsqlConnection connection, NpgsqlTransaction transaction, bool multiRowEvents = false) : IAsyncDisposable
{
    readonly NpgsqlBatch batch = new(connection, transaction);
    List<NewEvent>? events;
    int eventsAt = -1;

    /// <summary>A state write, with ? placeholders.</summary>
    public void Sql(string sql, object[] values) => batch.BatchCommands.Add(SqlText.Command(sql, values));

    /// <summary>A state write already shaped as a batch command.</summary>
    public void Command(NpgsqlBatchCommand command) => batch.BatchCommands.Add(command);

    /// <summary>One event, already shaped and hashed.</summary>
    public void Append(NewEvent e)
    {
        if (!multiRowEvents)
        {
            batch.BatchCommands.Add(EventStore.InsertCommand(e));
            return;
        }
        if (events is null)
        {
            events = [];
            eventsAt = batch.BatchCommands.Count;
        }
        events.Add(e);
    }

    public async Task FlushAsync(CancellationToken cancellation)
    {
        if (events is { Count: > 0 })
        {
            batch.BatchCommands.Insert(eventsAt, EventStore.InsertValuesCommand(events));
            events = null;
        }
        if (batch.BatchCommands.Count > 0)
        {
            await batch.ExecuteNonQueryAsync(cancellation);
        }
    }

    public ValueTask DisposeAsync() => batch.DisposeAsync();
}
