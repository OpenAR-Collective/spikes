using System.Globalization;

namespace Wax.Hashing;

/// <summary>
/// Date and time text in the forms Python's isoformat() writes, so that the
/// values inside events read the same on both sides of the bake-off.
/// </summary>
public static class IsoFormat
{
    /// <summary>Python: <c>value.astimezone(UTC).isoformat(timespec="microseconds")</c>.</summary>
    public static string Microseconds(DateTimeOffset value) =>
        value.UtcDateTime.ToString("yyyy'-'MM'-'dd'T'HH':'mm':'ss'.'ffffff'+00:00'", CultureInfo.InvariantCulture);

    /// <summary>Python: <c>value.isoformat()</c> on an aware UTC datetime, which
    /// leaves out the fraction when it is zero.</summary>
    public static string Default(DateTimeOffset value)
    {
        var utc = TruncateToMicroseconds(value).UtcDateTime;
        return utc.Ticks % TimeSpan.TicksPerSecond == 0
            ? utc.ToString("yyyy'-'MM'-'dd'T'HH':'mm':'ss'+00:00'", CultureInfo.InvariantCulture)
            : Microseconds(value);
    }

    /// <summary>Python: <c>date.isoformat()</c>.</summary>
    public static string Date(DateOnly value) => value.ToString("yyyy'-'MM'-'dd", CultureInfo.InvariantCulture);

    /// <summary>The current time cut to whole microseconds, the precision
    /// PostgreSQL and Python keep, so a value hashes the same after a round trip.</summary>
    public static DateTimeOffset UtcNowMicroseconds(TimeProvider? clock = null) =>
        TruncateToMicroseconds((clock ?? TimeProvider.System).GetUtcNow());

    public static DateTimeOffset TruncateToMicroseconds(DateTimeOffset value) =>
        new(value.UtcTicks - value.UtcTicks % 10, TimeSpan.Zero);
}
