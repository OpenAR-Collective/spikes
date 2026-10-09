using JasperFx;
using JasperFx.Events;
using Marten;
using Npgsql;
using Weasel.Core;
using Wax.Events;

namespace Wax.Store;

/// <summary>
/// The one place Marten is configured, used by the engine, the projection
/// worker, and the migration alike, so the three never disagree.
/// </summary>
public static class WaxStoreSetup
{
    public const string CoreSchema = "core";

    public static void Configure(StoreOptions options, NpgsqlDataSource dataSource, IReadOnlyList<IWaxModule> modules, bool mayChangeSchema)
    {
        options.Connection(dataSource);
        options.DatabaseSchemaName = CoreSchema;
        options.Events.DatabaseSchemaName = CoreSchema;
        options.Events.StreamIdentity = StreamIdentity.AsGuid;
        // The hash chain travels in the headers (see StreamHeads).
        options.Events.MetadataConfig.HeadersEnabled = true;
        options.UseSystemTextJsonForSerialization(EnumStorage.AsString, Casing.SnakeCase);

        // The application never changes the schema; the migration does.
        options.AutoCreateSchemaObjects = mayChangeSchema ? AutoCreate.CreateOrUpdate : AutoCreate.None;

        // A read model that skipped an event would be wrong without a sign, so
        // a projection that cannot handle an event stops there instead.
        options.Projections.Errors.SkipApplyErrors = false;
        options.Projections.Errors.SkipSerializationErrors = false;
        options.Projections.Errors.SkipUnknownEvents = false;

        foreach (var type in WaxEventTypes.DeclaredIn(modules.Select(module => module.GetType().Assembly).Append(typeof(WaxEvent).Assembly).Distinct()))
        {
            options.Events.MapEventType(type, WaxEventTypes.NameOf(type));
        }
        foreach (var module in modules)
        {
            module.ConfigureStore(options);
        }
    }
}
