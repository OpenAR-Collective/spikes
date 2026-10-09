using System.Text.RegularExpressions;
using Wax.Commands;

namespace HiveAR;

/// <summary>
/// HiveAR commands (WAX-0002). Each carries the primary key of what it
/// creates, so a handler returns nothing and a retried request cannot post
/// twice. Every input is bounded and strict, with the same limits as the
/// Python side's pydantic models.
/// </summary>
public sealed record PlaceAccount : ICommand
{
    public Guid AccountPk { get; init; } = Guid.NewGuid();
    public required string ClientAccountNumber { get; init; }
    public required string DebtType { get; init; }
    public required string ContractType { get; init; }
    public required string CurrencyCode { get; init; }
    public required DateOnly ListDate { get; init; }
    public required DateOnly ItemizationDate { get; init; }
    public required IReadOnlyDictionary<string, long> OpeningBalances { get; init; }

    /// <summary>The last day interest has been accrued for; the itemization date
    /// when not given. Added for the bake-off's interest accrual.</summary>
    public DateOnly? InterestAccruedThrough { get; init; }

    public void Validate()
    {
        Rules.Text(ClientAccountNumber, "client_account_number");
        if (InterestAccruedThrough is { } accruedThrough)
        {
            Rules.PlausibleDate(accruedThrough, "interest_accrued_through");
        }
        Rules.Text(DebtType, "debt_type");
        Rules.OneOf(ContractType, "contract_type", "third_party_contingency", "purchase");
        Rules.Currency(CurrencyCode);
        Rules.PlausibleDate(ListDate, "list_date");
        Rules.PlausibleDate(ItemizationDate, "itemization_date");
        if (OpeningBalances.Count is < 1 or > 50)
        {
            throw Rules.Invalid("opening_balances", "must name between 1 and 50 buckets");
        }
        foreach (var (code, amount) in OpeningBalances)
        {
            Rules.BucketCode(code);
            Rules.Amount(amount, "opening_balances");
        }
    }
}

public sealed record OpenDispute : ICommand
{
    public required Guid AccountPk { get; init; }
    public Guid DisputePk { get; init; } = Guid.NewGuid();

    public void Validate()
    {
    }
}

public sealed record PostPayment : ICommand
{
    public Guid PaymentPk { get; init; } = Guid.NewGuid();
    public required IReadOnlyList<Guid> AccountPks { get; init; }
    public required long AmountMinorUnits { get; init; }
    public required string CurrencyCode { get; init; }
    public DateTimeOffset? ReceivedAt { get; init; }
    public string AllocationMethod { get; init; } = "oldest_first";

    /// <summary>The consumer's own directions: account to amount.</summary>
    public IReadOnlyDictionary<Guid, long> DirectedAllocations { get; init; } = new Dictionary<Guid, long>();

    /// <summary>Per-payment waterfall override: account to bucket to amount.</summary>
    public IReadOnlyDictionary<Guid, IReadOnlyDictionary<string, long>> BucketAmounts { get; init; } =
        new Dictionary<Guid, IReadOnlyDictionary<string, long>>();

    /// <summary>Accounts whose bucket amounts the consumer directed. Required
    /// for a disputed account; without it bucket amounts there are refused.</summary>
    public IReadOnlyList<Guid> ConsumerDirectedAccounts { get; init; } = [];

    public void Validate()
    {
        if (AccountPks.Count is < 1 or > 200)
        {
            throw Rules.Invalid("account_pks", "must name between 1 and 200 accounts");
        }
        Rules.Amount(AmountMinorUnits, "amount_minor_units");
        Rules.Currency(CurrencyCode);
        if (ReceivedAt is { } receivedAt)
        {
            Rules.PlausibleInstant(receivedAt, "received_at");
        }
        Rules.OneOf(AllocationMethod, "allocation_method", "even_split", "oldest_first");
        foreach (var amount in DirectedAllocations.Values)
        {
            Rules.Amount(amount, "directed_allocations");
        }
        foreach (var amounts in BucketAmounts.Values)
        {
            foreach (var (code, amount) in amounts)
            {
                Rules.BucketCode(code);
                Rules.Amount(amount, "bucket_amounts");
            }
        }
        if (ConsumerDirectedAccounts.Count > 200)
        {
            throw Rules.Invalid("consumer_directed_accounts", "may name at most 200 accounts");
        }
    }
}

public sealed record ReversePayment : ICommand
{
    public required Guid PaymentPk { get; init; }

    public void Validate()
    {
    }
}

static partial class Rules
{
    // Whole minor units, bounded so that no sum of them can overflow a 64-bit column.
    const long MaxAmount = 1_000_000_000_000_000;

    // Text a person typed. NUL cannot be stored and control characters have no place in it.
    [GeneratedRegex(@"^[^\x00-\x1f\x7f]+\z")]
    private static partial Regex TextPattern();

    [GeneratedRegex(@"^[a-z][a-z0-9_]*\z")]
    private static partial Regex BucketPattern();

    public static CommandRejectedException Invalid(string field, string problem) =>
        new($"{field} {problem}", "invalid_command");

    public static void Amount(long value, string field)
    {
        if (value is <= 0 or > MaxAmount)
        {
            throw Invalid(field, "must be a whole number of minor units above zero and at most 10^15");
        }
    }

    public static void Text(string? value, string field)
    {
        var length = value?.EnumerateRunes().Count() ?? 0;
        if (value is null || length is < 1 or > 200 || !TextPattern().IsMatch(value))
        {
            throw Invalid(field, "must be 1 to 200 characters with no control characters");
        }
    }

    public static void BucketCode(string code)
    {
        if (code.Length is < 1 or > 64 || !BucketPattern().IsMatch(code))
        {
            throw Invalid("bucket code", $"{code} is not a valid bucket code");
        }
    }

    public static void OneOf(string value, string field, params string[] allowed)
    {
        if (!allowed.Contains(value))
        {
            throw Invalid(field, $"must be one of {string.Join(", ", allowed)}");
        }
    }

    public static void Currency(string code)
    {
        if (!HiveAR.Currency.MinorUnitDigits.ContainsKey(code))
        {
            throw Invalid("currency_code", $"{code} is not a currency this installation takes");
        }
    }

    public static void PlausibleDate(DateOnly value, string field)
    {
        if (value < new DateOnly(1900, 1, 1) || value > new DateOnly(2200, 1, 1))
        {
            throw Invalid(field, "is outside the range this system records");
        }
    }

    public static void PlausibleInstant(DateTimeOffset value, string field)
    {
        if (value < new DateTimeOffset(1900, 1, 1, 0, 0, 0, TimeSpan.Zero) || value > DateTimeOffset.UtcNow.AddDays(1))
        {
            throw Invalid(field, "must be after 1900 and not more than a day ahead");
        }
    }
}

/// <summary>
/// Currencies this installation takes, with the number of minor-unit digits
/// each has (HIVE-0005). A short list on purpose, as in the prototype.
/// </summary>
public static class Currency
{
    public static readonly IReadOnlyDictionary<string, int> MinorUnitDigits = new Dictionary<string, int>
    {
        ["USD"] = 2,
        ["CAD"] = 2,
        ["EUR"] = 2,
        ["GBP"] = 2,
        ["AUD"] = 2,
        ["MXN"] = 2,
        ["JPY"] = 0,
    };
}
