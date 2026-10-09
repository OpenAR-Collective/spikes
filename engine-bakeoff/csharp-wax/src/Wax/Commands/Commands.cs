namespace Wax.Commands;

/// <summary>
/// A named, immutable request (WAX-0002 CQRS Model). Validate checks the
/// command's own shape before anything touches the database, as the Python
/// side's pydantic models do when a command is built.
/// </summary>
public interface ICommand
{
    void Validate();
}

/// <summary>One handler per command type. It runs inside the command's single
/// transaction and returns nothing: the outcome is success or an exception.</summary>
public interface ICommandHandler<in TCommand> where TCommand : ICommand
{
    Task HandleAsync(TCommand command, CommandContext context, CancellationToken cancellation);
}

/// <summary>
/// The command is invalid against its own shape or the current state. Nothing
/// was written. Code names the refusal for a client that shows its own wording.
/// </summary>
public class CommandRejectedException(string message, string code = "rejected") : Exception(message)
{
    public string Code { get; } = code;
}

/// <summary>
/// This exact command, under this key, took effect earlier. Nothing is written,
/// and the caller is answered as if it had just succeeded: a request retried
/// after a lost response must not be told it failed.
/// </summary>
public sealed class AlreadyAppliedException() : Exception("This command already took effect under this key");

/// <summary>Another writer advanced the aggregate past the expected version.</summary>
public sealed class WaxConcurrencyException(string message) : Exception(message);

/// <summary>Who, when, and why: the non-payload fields shared by a command's events.</summary>
public sealed record Attribution(
    Guid ActorPk,
    string ActorType,
    DateTimeOffset OccurredAt,
    string? Source,
    Guid? CorrelationPk,
    string? CorrelationType);
