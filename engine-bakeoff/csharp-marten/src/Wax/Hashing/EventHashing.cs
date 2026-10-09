namespace Wax.Hashing;

/// <summary>
/// Whether events are hashed and chained. Always on in Wax. The bake-off's
/// hashing-off diagnostic sets WAX_HASH=off to measure what the chain costs:
/// every event then carries a fixed placeholder hash of a real hash's length,
/// as its prior hash too after its first, and the stream's head is not read:
/// the version check trusts the locked state row, with the unique index on
/// (stream_id, version) as the backstop, as on the Python side. A run with
/// hashing off cannot pass verify-chain, and is a diagnostic, never a contestant.
/// </summary>
public static class EventHashing
{
    public static readonly string Placeholder = new('0', 64);

    public static bool Enabled { get; } =
        !string.Equals(Environment.GetEnvironmentVariable("WAX_HASH"), "off", StringComparison.OrdinalIgnoreCase);
}
