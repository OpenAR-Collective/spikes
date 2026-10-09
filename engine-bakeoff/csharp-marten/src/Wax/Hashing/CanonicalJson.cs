using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Wax.Hashing;

/// <summary>
/// Writes a JSON value byte for byte as Python's
/// <c>json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)</c>
/// does, so that the C# and Python sides of the bake-off hash the same bytes
/// for the same event. Keys are sorted by code point at every depth, and every
/// character outside printable ASCII is escaped as <c>\uXXXX</c>.
/// </summary>
public static class CanonicalJson
{
    public static string Serialize(JsonNode? node)
    {
        var builder = new StringBuilder(256);
        Write(builder, node);
        return builder.ToString();
    }

    internal static void Write(StringBuilder builder, JsonNode? node)
    {
        switch (node)
        {
            case null:
                builder.Append("null");
                break;
            case JsonObject obj:
                builder.Append('{');
                var first = true;
                foreach (var (key, value) in obj.OrderBy(pair => pair.Key, CodePointComparer.Instance))
                {
                    if (!first)
                    {
                        builder.Append(',');
                    }
                    first = false;
                    WriteString(builder, key);
                    builder.Append(':');
                    Write(builder, value);
                }
                builder.Append('}');
                break;
            case JsonArray array:
                builder.Append('[');
                for (var index = 0; index < array.Count; index++)
                {
                    if (index > 0)
                    {
                        builder.Append(',');
                    }
                    Write(builder, array[index]);
                }
                builder.Append(']');
                break;
            case JsonValue value:
                WriteValue(builder, value);
                break;
        }
    }

    static void WriteValue(StringBuilder builder, JsonValue value)
    {
        switch (value.GetValueKind())
        {
            case JsonValueKind.String:
                WriteString(builder, value.GetValue<string>());
                break;
            case JsonValueKind.True:
                builder.Append("true");
                break;
            case JsonValueKind.False:
                builder.Append("false");
                break;
            case JsonValueKind.Null:
                builder.Append("null");
                break;
            case JsonValueKind.Number:
                // Money and counts are whole numbers. A float would serialize
                // differently in the two languages, so it is refused outright.
                if (!value.TryGetValue<long>(out var whole))
                {
                    throw new NotSupportedException($"Only whole numbers can be hashed, not {value.ToJsonString()}");
                }
                builder.Append(whole.ToString(CultureInfo.InvariantCulture));
                break;
            default:
                throw new NotSupportedException($"Cannot hash a JSON {value.GetValueKind()}");
        }
    }

    internal static void WriteString(StringBuilder builder, string text)
    {
        builder.Append('"');
        foreach (var character in text)
        {
            switch (character)
            {
                case '"': builder.Append("\\\""); break;
                case '\\': builder.Append("\\\\"); break;
                case '\n': builder.Append("\\n"); break;
                case '\r': builder.Append("\\r"); break;
                case '\t': builder.Append("\\t"); break;
                case '\b': builder.Append("\\b"); break;
                case '\f': builder.Append("\\f"); break;
                default:
                    // Python escapes everything outside space through tilde,
                    // including DEL, and writes astral characters as the
                    // UTF-16 surrogate pair, which is what a C# string holds.
                    if (character < ' ' || character > '~')
                    {
                        builder.Append("\\u").Append(((int)character).ToString("x4", CultureInfo.InvariantCulture));
                    }
                    else
                    {
                        builder.Append(character);
                    }
                    break;
            }
        }
        builder.Append('"');
    }

    /// <summary>Python orders str keys by code point, which differs from
    /// ordinal UTF-16 order only for characters above the BMP.</summary>
    sealed class CodePointComparer : IComparer<string>
    {
        public static readonly CodePointComparer Instance = new();

        public int Compare(string? left, string? right)
        {
            var x = (left ?? string.Empty).EnumerateRunes();
            var y = (right ?? string.Empty).EnumerateRunes();
            while (true)
            {
                var hasX = x.MoveNext();
                var hasY = y.MoveNext();
                if (!hasX || !hasY)
                {
                    return hasX.CompareTo(hasY);
                }
                var difference = x.Current.Value.CompareTo(y.Current.Value);
                if (difference != 0)
                {
                    return difference;
                }
            }
        }
    }
}
