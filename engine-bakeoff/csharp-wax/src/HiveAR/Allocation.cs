namespace HiveAR;

public sealed record AccountPosition(Guid AccountPk, DateOnly ListDate, bool IsDisputed, long BalanceMinorUnits);

/// <summary>
/// Payment distribution (HIVE-0007 Payment Application and Waterfall), ported
/// line for line from the prototype's hivear/allocation.py. Pure functions
/// over integers: allocation across accounts runs first, then each account's
/// bucket waterfall.
/// </summary>
public static class Allocation
{
    /// <summary>
    /// Split one payment across accounts. The consumer's directions are honored
    /// first, and a disputed account receives nothing the consumer did not
    /// direct to it. An account in fixedAccounts receives exactly its directed
    /// amount and takes no part in the default spread.
    /// </summary>
    public static (Dictionary<Guid, long> Shares, long Remainder) AllocateAcrossAccounts(
        long amount,
        IReadOnlyList<AccountPosition> positions,
        string method,
        IReadOnlyDictionary<Guid, long> directed,
        IReadOnlySet<Guid> fixedAccounts)
    {
        var shares = positions.ToDictionary(position => position.AccountPk, _ => 0L);
        var room = positions.ToDictionary(position => position.AccountPk, position => position.BalanceMinorUnits);
        var remaining = amount;

        foreach (var (accountPk, directedAmount) in directed)
        {
            var applied = Math.Min(directedAmount, room[accountPk]);
            shares[accountPk] += applied;
            room[accountPk] -= applied;
            remaining -= directedAmount;
        }
        // Directed money beyond an account's balance is not moved to another account.
        var heldBack = directed.Values.Sum() - shares.Values.Sum();
        var remainingToSpread = remaining;

        var eligible = positions
            .Where(position => !position.IsDisputed && !fixedAccounts.Contains(position.AccountPk))
            .OrderBy(position => position.ListDate)
            .ThenBy(position => position.AccountPk.ToString(), StringComparer.Ordinal)
            .ToList();

        switch (method)
        {
            case "oldest_first":
                foreach (var position in eligible)
                {
                    var applied = Math.Min(remainingToSpread, room[position.AccountPk]);
                    shares[position.AccountPk] += applied;
                    room[position.AccountPk] -= applied;
                    remainingToSpread -= applied;
                }
                break;
            case "even_split":
                // Split evenly, cap each share at the account's balance, and
                // re-split whatever the capped accounts could not absorb.
                while (remainingToSpread > 0)
                {
                    var open = eligible.Where(position => room[position.AccountPk] > 0).ToList();
                    if (open.Count == 0)
                    {
                        break;
                    }
                    var share = Math.DivRem(remainingToSpread, open.Count, out var extra);
                    for (var index = 0; index < open.Count; index++)
                    {
                        var position = open[index];
                        var applied = Math.Min(share + (index < extra ? 1 : 0), room[position.AccountPk]);
                        shares[position.AccountPk] += applied;
                        room[position.AccountPk] -= applied;
                        remainingToSpread -= applied;
                    }
                }
                break;
            default:
                throw new ArgumentException($"Unknown allocation method: {method}", nameof(method));
        }

        return (shares, remainingToSpread + heldBack);
    }

    /// <summary>Fill buckets in priority order. Buckets with no balance are skipped.</summary>
    public static (List<(string Code, long Amount)> Paydown, long Remainder) RunWaterfall(
        long amount,
        IEnumerable<(string Code, int Priority, long Balance)> buckets)
    {
        var paydown = new List<(string Code, long Amount)>();
        var remaining = amount;
        foreach (var (code, _, balance) in buckets.OrderBy(bucket => bucket.Priority).ThenBy(bucket => bucket.Code, StringComparer.Ordinal))
        {
            var applied = Math.Min(remaining, balance);
            if (applied > 0)
            {
                paydown.Add((code, applied));
                remaining -= applied;
            }
        }
        return (paydown, remaining);
    }
}
