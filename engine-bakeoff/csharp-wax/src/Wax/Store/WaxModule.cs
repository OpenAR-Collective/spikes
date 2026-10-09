using System.Reflection;
using Wax.Commands;
using Wax.Projections;

namespace Wax.Store;

/// <summary>One ordered piece of schema, applied once and recorded.</summary>
public sealed record SchemaScript(string Name, string Sql);

/// <summary>
/// What a module (HiveAR here) adds to the engine: its schema, its
/// projections, and its command handlers. Its event types are found in its
/// assembly.
/// </summary>
public interface IWaxModule
{
    string Name { get; }

    IReadOnlyList<SchemaScript> SchemaScripts { get; }

    IReadOnlyList<Projection> Projections { get; }

    void RegisterHandlers(CommandDispatcher dispatcher);
}

public static class EmbeddedScripts
{
    /// <summary>The .sql resources in an assembly under a folder, in name order.</summary>
    public static IReadOnlyList<SchemaScript> From(Assembly assembly, string folder, string namePrefix)
    {
        var resourcePrefix = $"{assembly.GetName().Name}.{folder}.";
        return assembly.GetManifestResourceNames()
            .Where(name => name.StartsWith(resourcePrefix, StringComparison.Ordinal) && name.EndsWith(".sql", StringComparison.Ordinal))
            .Order(StringComparer.Ordinal)
            .Select(name =>
            {
                using var stream = assembly.GetManifestResourceStream(name)!;
                using var reader = new StreamReader(stream);
                return new SchemaScript($"{namePrefix}/{name[resourcePrefix.Length..]}", reader.ReadToEnd());
            })
            .ToList();
    }
}
