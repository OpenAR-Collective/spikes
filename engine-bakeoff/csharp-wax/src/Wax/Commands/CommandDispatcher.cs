using Npgsql;
using Wax.Hashing;

namespace Wax.Commands;

/// <summary>
/// The command path (WAX-0002 CQRS Model). Each command runs in one
/// transaction: the handler reads and locks what it needs, queues its events
/// and state writes, and they go out in one batch before the commit. A
/// conflict with a concurrent writer is retried from the start, up to three
/// attempts, as on the Python side.
/// </summary>
public sealed class CommandDispatcher(
    NpgsqlDataSource dataSource,
    ReasonCodes reasons,
    TimeProvider? clock = null)
{
    public const int MaxAttempts = 3;

    static readonly HashSet<string> ActorTypes = ["user", "system", "integration", "batch"];

    readonly Dictionary<Type, Func<ICommand, CommandContext, CancellationToken, Task>> handlers = [];
    readonly TimeProvider clock = clock ?? TimeProvider.System;
    long conflictRetries;

    /// <summary>How many attempts were started again after a conflict, since this dispatcher was made.</summary>
    public long ConflictRetries => Interlocked.Read(ref conflictRetries);

    public void Register<TCommand>(ICommandHandler<TCommand> handler) where TCommand : ICommand
    {
        if (!handlers.TryAdd(typeof(TCommand), (command, context, cancellation) => handler.HandleAsync((TCommand)command, context, cancellation)))
        {
            throw new InvalidOperationException($"{typeof(TCommand).Name} already has a handler");
        }
    }

    public async Task DispatchAsync(
        ICommand command,
        Guid actorPk,
        string actorType = "user",
        string? source = null,
        Guid? correlationPk = null,
        string? correlationType = null,
        CancellationToken cancellation = default)
    {
        if (!ActorTypes.Contains(actorType))
        {
            throw new ArgumentException($"Unknown actor type {actorType}", nameof(actorType));
        }
        if (correlationPk is null != correlationType is null)
        {
            throw new ArgumentException("A correlation needs both its key and its type");
        }
        command.Validate();
        if (!handlers.TryGetValue(command.GetType(), out var handler))
        {
            throw new InvalidOperationException($"No handler for {command.GetType().Name}");
        }

        for (var attempt = 1; ; attempt++)
        {
            var attribution = new Attribution(
                actorPk, actorType, IsoFormat.UtcNowMicroseconds(clock), source, correlationPk, correlationType);
            await using var connection = await dataSource.OpenConnectionAsync(cancellation);
            await using var transaction = await connection.BeginTransactionAsync(cancellation);
            await using var writes = new WriteQueue(connection, transaction);
            var context = new CommandContext(connection, transaction, writes, attribution, reasons);
            try
            {
                await handler(command, context, cancellation);
                await writes.FlushAsync(cancellation);
                await transaction.CommitAsync(cancellation);
                return;
            }
            catch (AlreadyAppliedException)
            {
                // Nothing was written; the transaction rolls back on dispose.
                return;
            }
            catch (Exception error) when (attempt < MaxAttempts && IsConflict(error))
            {
                // Another writer got there first. Start again from fresh state.
                Interlocked.Increment(ref conflictRetries);
            }
        }
    }

    /// <summary>A concurrent writer won the race: a stale version, an event or
    /// row that now exists, or a deadlock between two lockers.</summary>
    public static bool IsConflict(Exception error)
    {
        for (var current = error; current is not null; current = current.InnerException)
        {
            switch (current)
            {
                case WaxConcurrencyException:
                case PostgresException { SqlState: PostgresErrorCodes.UniqueViolation or PostgresErrorCodes.DeadlockDetected or PostgresErrorCodes.SerializationFailure }:
                    return true;
                case AggregateException aggregate when aggregate.InnerExceptions.Any(IsConflict):
                    return true;
            }
        }
        return false;
    }
}
