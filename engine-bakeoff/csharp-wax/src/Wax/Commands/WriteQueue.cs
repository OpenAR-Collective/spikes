using System.Collections.Concurrent;
using System.Linq.Expressions;
using System.Reflection;
using System.Text;
using System.Text.Json.Nodes;
using JasperFx.Events;
using Marten;
using Marten.Services;
using Npgsql;
using NpgsqlTypes;
using Wax.Events;
using Wax.Store;

namespace Wax.Commands;

/// <summary>How a command's events are appended to the event store.</summary>
public enum AppendMode
{
    /// <summary>Through a Marten session: its event append function and its batching.</summary>
    Marten,

    /// <summary>With plain SQL into the same tables, without Marten's session or
    /// append function. Marten's daemon still reads the events for the read
    /// models and the rules. A bake-off variant that separates the language
    /// from the library on the write path.</summary>
    Direct,
}

/// <summary>A command's queued writes, sent together when the handler returns.</summary>
internal interface IWriteQueue : IAsyncDisposable
{
    /// <summary>A state write, with ? placeholders.</summary>
    void Sql(string sql, object[] values);

    /// <summary>One event on a stream whose version before it is priorVersion.</summary>
    void Append(Guid streamId, long priorVersion, WaxEvent body, string eventHash, string? priorEventHash);

    Task FlushAsync(CancellationToken cancellation);

    static IWriteQueue Create(AppendMode mode, NpgsqlConnection connection, NpgsqlTransaction transaction, IDocumentStore store) =>
        mode == AppendMode.Direct
            ? new DirectWriteQueue(connection, transaction, store.Advanced.Serializer)
            : new MartenWriteQueue(store.LightweightSession(SessionOptions.ForTransaction(transaction, shouldAutoCommit: false)));
}

/// <summary>The writes go through a Marten session enlisted in the command's transaction.</summary>
internal sealed class MartenWriteQueue(IDocumentSession session) : IWriteQueue
{
    // Marten names an event after the static type it is wrapped as, so each
    // body is wrapped as its own concrete type, never as WaxEvent.
    static readonly ConcurrentDictionary<Type, Func<WaxEvent, IEvent>> Wrappers = new();

    static IEvent Wrap(WaxEvent body) => Wrappers.GetOrAdd(body.GetType(), static type =>
    {
        var asEvent = typeof(Event).GetMethods(BindingFlags.Public | BindingFlags.Static)
            .Single(method => method.Name == nameof(Event.AsEvent) && method.IsGenericMethodDefinition && method.GetParameters().Length == 1)
            .MakeGenericMethod(type);
        var parameter = Expression.Parameter(typeof(WaxEvent));
        return Expression.Lambda<Func<WaxEvent, IEvent>>(
            Expression.Convert(Expression.Call(asEvent, Expression.Convert(parameter, type)), typeof(IEvent)),
            parameter).Compile();
    })(body);

    public void Sql(string sql, object[] values) => session.QueueSqlCommand(sql, values);

    public void Append(Guid streamId, long priorVersion, WaxEvent body, string eventHash, string? priorEventHash)
    {
        var wrapped = Wrap(body).WithHeader(StreamHeads.EventHashHeader, eventHash);
        if (priorEventHash is not null)
        {
            wrapped = wrapped.WithHeader(StreamHeads.PriorEventHashHeader, priorEventHash);
        }
        if (priorVersion == 0)
        {
            session.Events.StartStream(streamId, wrapped);
        }
        else
        {
            session.Events.Append(streamId, wrapped);
        }
    }

    public Task FlushAsync(CancellationToken cancellation) => session.SaveChangesAsync(cancellation);

    public ValueTask DisposeAsync() => session.DisposeAsync();
}

/// <summary>
/// The writes go out as one Npgsql batch of plain SQL. An event is a row in
/// Marten's mt_events, numbered from the same sequence inside the same
/// transaction as Marten's own append, and the stream's row in mt_streams is
/// inserted or moved to the new version. The command already holds the
/// aggregate's row lock and has checked its version, so nothing here checks
/// again; the unique index on (stream_id, version) is the backstop.
/// </summary>
internal sealed class DirectWriteQueue(NpgsqlConnection connection, NpgsqlTransaction transaction, ISerializer serializer) : IWriteQueue
{
    const string StartStreamSql =
        "INSERT INTO core.mt_streams (id, type, version, timestamp, tenant_id) VALUES ($1, NULL, $2, now(), '*DEFAULT*')";

    const string MoveStreamSql = "UPDATE core.mt_streams SET version = $2, timestamp = now() WHERE id = $1";

    const string InsertEventSql =
        "INSERT INTO core.mt_events (seq_id, id, stream_id, version, data, type, tenant_id, timestamp, mt_dotnet_type, is_archived, headers) " +
        "VALUES (nextval('core.mt_events_sequence'), $1, $2, $3, $4, $5, '*DEFAULT*', now(), $6, false, $7)";

    static readonly ConcurrentDictionary<string, string> Positional = new();
    static readonly ConcurrentDictionary<Type, string> DotnetTypes = new();

    readonly NpgsqlBatch batch = new(connection, transaction);

    public void Sql(string sql, object[] values)
    {
        var command = new NpgsqlBatchCommand(Positional.GetOrAdd(sql, ToPositional));
        foreach (var value in values)
        {
            command.Parameters.Add(new NpgsqlParameter { Value = value ?? DBNull.Value });
        }
        batch.BatchCommands.Add(command);
    }

    public void Append(Guid streamId, long priorVersion, WaxEvent body, string eventHash, string? priorEventHash)
    {
        var version = priorVersion + 1;
        batch.BatchCommands.Add(new NpgsqlBatchCommand(priorVersion == 0 ? StartStreamSql : MoveStreamSql)
        {
            Parameters =
            {
                new NpgsqlParameter { Value = streamId },
                new NpgsqlParameter { Value = version },
            },
        });
        var headers = new JsonObject { [StreamHeads.EventHashHeader] = eventHash };
        if (priorEventHash is not null)
        {
            headers[StreamHeads.PriorEventHashHeader] = priorEventHash;
        }
        var type = body.GetType();
        batch.BatchCommands.Add(new NpgsqlBatchCommand(InsertEventSql)
        {
            Parameters =
            {
                new NpgsqlParameter { Value = Guid.NewGuid() },
                new NpgsqlParameter { Value = streamId },
                new NpgsqlParameter { Value = version },
                new NpgsqlParameter { Value = serializer.ToJson(body), NpgsqlDbType = NpgsqlDbType.Jsonb },
                new NpgsqlParameter { Value = WaxEventTypes.NameOf(type) },
                new NpgsqlParameter { Value = DotnetTypes.GetOrAdd(type, static t => $"{t.FullName}, {t.Assembly.GetName().Name}") },
                new NpgsqlParameter { Value = headers.ToJsonString(), NpgsqlDbType = NpgsqlDbType.Jsonb },
            },
        });
    }

    public async Task FlushAsync(CancellationToken cancellation)
    {
        if (batch.BatchCommands.Count > 0)
        {
            await batch.ExecuteNonQueryAsync(cancellation);
        }
    }

    public ValueTask DisposeAsync() => batch.DisposeAsync();

    /// <summary>Marten-style ? placeholders as PostgreSQL's $1, $2, and so on.</summary>
    static string ToPositional(string sql)
    {
        var builder = new StringBuilder(sql.Length + 16);
        var index = 0;
        foreach (var character in sql)
        {
            if (character == '?')
            {
                builder.Append('$').Append(++index);
            }
            else
            {
                builder.Append(character);
            }
        }
        return builder.ToString();
    }
}
