using System.Collections.Frozen;
using Npgsql;

namespace Wax.Commands;

/// <summary>
/// The reason code registry (WAX-0020) held in memory. Reason codes are
/// reference data, so each process reads them once and again only on a miss.
/// </summary>
public sealed class ReasonCodes
{
    const string Sql =
        "SELECT value.code, value.reference_value_pk FROM core.reference_value value " +
        "JOIN core.reference_type type ON type.reference_type_pk = value.reference_type_fk " +
        "WHERE type.code = 'reason_code'";

    readonly NpgsqlDataSource dataSource;
    FrozenDictionary<string, Guid> codes;

    ReasonCodes(NpgsqlDataSource dataSource, FrozenDictionary<string, Guid> codes)
    {
        this.dataSource = dataSource;
        this.codes = codes;
    }

    public static async Task<ReasonCodes> LoadAsync(NpgsqlDataSource dataSource, CancellationToken cancellation = default) =>
        new(dataSource, await ReadAsync(dataSource, cancellation));

    public async ValueTask<Guid> ResolveAsync(string code, CancellationToken cancellation)
    {
        if (codes.TryGetValue(code, out var known))
        {
            return known;
        }
        codes = await ReadAsync(dataSource, cancellation);
        return codes.TryGetValue(code, out var added)
            ? added
            : throw new CommandRejectedException($"Unknown reason code: {code}", "unknown_reason_code");
    }

    static async Task<FrozenDictionary<string, Guid>> ReadAsync(NpgsqlDataSource dataSource, CancellationToken cancellation)
    {
        var found = new Dictionary<string, Guid>();
        await using var command = dataSource.CreateCommand(Sql);
        await using var reader = await command.ExecuteReaderAsync(cancellation);
        while (await reader.ReadAsync(cancellation))
        {
            found[reader.GetString(0)] = reader.GetGuid(1);
        }
        return found.ToFrozenDictionary();
    }
}
