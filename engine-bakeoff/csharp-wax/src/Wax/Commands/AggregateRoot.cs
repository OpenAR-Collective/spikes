namespace Wax.Commands;

/// <summary>
/// The state row of an aggregate root (WAX-0001). AggregateSequenceNumber
/// mirrors the last event written for the aggregate and is the expected
/// version checked when the next event is appended.
///
/// A state row can only be saved through a <see cref="CommandContext"/>, and
/// only the dispatcher can make one, so writing authoritative state outside a
/// command handler does not compile.
/// </summary>
public abstract class AggregateRoot
{
    public abstract string AggregateType { get; }

    public required Guid Pk { get; init; }

    public int AggregateSequenceNumber { get; set; }

    /// <summary>True when this command created the aggregate rather than loading it.</summary>
    public bool IsNew { get; init; }

    internal bool Inserted { get; set; }

    protected internal abstract void QueueInsert(CommandContext context);

    protected internal abstract void QueueUpdate(CommandContext context);
}
