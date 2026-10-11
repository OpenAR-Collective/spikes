using System.Text.Json.Nodes;
using Microsoft.EntityFrameworkCore;
using Microsoft.EntityFrameworkCore.Design;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Storage.ValueConversion;
using Wax.Commands;

namespace HiveAR.Data;

/// <summary>How the handlers read and write authoritative state.</summary>
public enum StateAccess
{
    /// <summary>Plain SQL through Npgsql, batched, as on csharp-wax.</summary>
    Sql,

    /// <summary>EF Core, inside the command's connection and transaction.</summary>
    EfCore,
}

/// <summary>
/// EF Core's model of the authoritative state tables. It maps onto the
/// hand-written schema exactly as it stands: no EF migrations, and no
/// migrations history table. The event store is not in it; events are
/// written by Wax's own event store, as on csharp-wax. Foundation-written.
/// </summary>
public sealed class ArDbContext(DbContextOptions<ArDbContext> options) : DbContext(options)
{
    public DbSet<AccountState> Accounts => Set<AccountState>();

    public DbSet<BucketRow> Buckets => Set<BucketRow>();

    public DbSet<BucketDefinition> BucketDefinitions => Set<BucketDefinition>();

    public DbSet<PaymentState> Payments => Set<PaymentState>();

    public DbSet<TaskRow> Tasks => Set<TaskRow>();

    public DbSet<PendingEventRow> PendingEvents => Set<PendingEventRow>();

    protected override void OnModelCreating(ModelBuilder model)
    {
        model.Entity<AccountState>(account =>
        {
            account.ToTable("account", "ar");
            account.HasKey(a => a.Pk);
            account.Property(a => a.Pk).HasColumnName("account_pk").ValueGeneratedNever();
            // The expected version, as an EF team would map it: an UPDATE that
            // finds the version moved affects no row and fails. The row lock
            // already prevents that; the check is kept as EF practice.
            account.Property(a => a.AggregateSequenceNumber).HasColumnName("aggregate_sequence_number").IsConcurrencyToken();
            account.Property(a => a.ClientAccountNumber).HasColumnName("client_account_number");
            account.Property(a => a.DebtType).HasColumnName("debt_type");
            account.Property(a => a.ContractType).HasColumnName("contract_type");
            account.Property(a => a.CurrencyCode).HasColumnName("currency_code");
            account.Property(a => a.Status).HasColumnName("status");
            account.Property(a => a.IsDisputed).HasColumnName("is_disputed");
            account.Property(a => a.ListDate).HasColumnName("list_date");
            account.Property(a => a.ItemizationDate).HasColumnName("itemization_date");
            account.Property(a => a.InterestAccruedThrough).HasColumnName("interest_accrued_through");
            account.Property(a => a.QueueCode).HasColumnName("queue_code");
            account.Ignore(a => a.IsNew);
            account.HasMany(a => a.Buckets).WithOne().HasForeignKey(b => b.AccountFk);
        });

        model.Entity<BucketRow>(bucket =>
        {
            bucket.ToTable("account_balance_bucket", "ar");
            bucket.HasKey(b => b.Pk);
            bucket.Property(b => b.Pk).HasColumnName("account_balance_bucket_pk").ValueGeneratedNever();
            bucket.Property(b => b.Balance).HasColumnName("balance_minor_units");
            bucket.Property(b => b.AccountFk).HasColumnName("account_fk");
            bucket.Property(b => b.DefinitionFk).HasColumnName("balance_bucket_definition_fk");
            bucket.HasOne(b => b.Definition).WithMany().HasForeignKey(b => b.DefinitionFk);
            // Taken from the definition after loading, as the plain SQL's join does.
            bucket.Ignore(b => b.Code);
            bucket.Ignore(b => b.Priority);
        });

        model.Entity<BucketDefinition>(definition =>
        {
            definition.ToTable("balance_bucket_definition", "ar");
            definition.HasKey(d => d.Pk);
            definition.Property(d => d.Pk).HasColumnName("balance_bucket_definition_pk").ValueGeneratedNever();
            definition.Property(d => d.Code).HasColumnName("code");
            definition.Property(d => d.Priority).HasColumnName("waterfall_priority");
        });

        model.Entity<PaymentState>(payment =>
        {
            payment.ToTable("payment", "ar");
            payment.HasKey(p => p.Pk);
            payment.Property(p => p.Pk).HasColumnName("payment_pk").ValueGeneratedNever();
            payment.Property(p => p.AggregateSequenceNumber).HasColumnName("aggregate_sequence_number").IsConcurrencyToken();
            payment.Property(p => p.AmountMinorUnits).HasColumnName("amount_minor_units");
            payment.Property(p => p.CurrencyCode).HasColumnName("currency_code");
            payment.Property(p => p.Status).HasColumnName("status");
            payment.Property(p => p.ReceivedAt).HasColumnName("received_at");
            payment.Property(p => p.Distribution).HasColumnName("distribution").HasColumnType("jsonb")
                .HasConversion<JsonObjectConverter>();
            payment.Property(p => p.UnappliedMinorUnits).HasColumnName("unapplied_minor_units");
            payment.Ignore(p => p.IsNew);
        });

        model.Entity<TaskRow>(task =>
        {
            task.ToTable("task", "ar");
            task.HasKey(t => t.Pk);
            task.Property(t => t.Pk).HasColumnName("task_pk").ValueGeneratedNever();
            task.Property(t => t.AccountFk).HasColumnName("account_fk");
            task.Property(t => t.TaskType).HasColumnName("task_type");
            task.Property(t => t.Status).HasColumnName("status");
            task.Property(t => t.CreatedAt).HasColumnName("created_at");
        });

        model.Entity<PendingEventRow>(entry =>
        {
            entry.ToTable("pending_event", "core");
            entry.HasKey(e => e.Pk);
            entry.Property(e => e.Pk).HasColumnName("pending_event_pk").ValueGeneratedNever();
            entry.Property(e => e.AggregatePkRef).HasColumnName("aggregate_pk_ref");
            entry.Property(e => e.AggregateType).HasColumnName("aggregate_type");
            entry.Property(e => e.TargetEventType).HasColumnName("target_event_type");
            entry.Property(e => e.DueAt).HasColumnName("due_at");
            entry.Property(e => e.Status).HasColumnName("status");
            entry.Property(e => e.ScheduledByEvent).HasColumnName("scheduled_by_event");
            entry.Property(e => e.FiredAt).HasColumnName("fired_at");
        });
    }
}

/// <summary>A jsonb column as a JsonObject. A named class, so that a compiled
/// model can refer to it.</summary>
public sealed class JsonObjectConverter() : ValueConverter<JsonObject, string>(
    value => value.ToJsonString(null),
    text => JsonNode.Parse(text, null, default)!.AsObject());

/// <summary>Lets the EF Core tools build the model to generate the compiled model.</summary>
public sealed class ArDbContextDesignFactory : IDesignTimeDbContextFactory<ArDbContext>
{
    public ArDbContext CreateDbContext(string[] args) =>
        new(new DbContextOptionsBuilder<ArDbContext>().UseNpgsql().Options);
}

/// <summary>A balance bucket's definition: reference data the waterfall reads.</summary>
public sealed class BucketDefinition
{
    public Guid Pk { get; set; }
    public string Code { get; set; } = "";
    public int Priority { get; set; }
}

/// <summary>A task row, written by CreateTask.</summary>
public sealed class TaskRow
{
    public Guid Pk { get; set; }
    public Guid AccountFk { get; set; }
    public string TaskType { get; set; } = "";
    public string Status { get; set; } = "";
    public DateTimeOffset CreatedAt { get; set; }
}

/// <summary>A Pending Event Register entry (WAX-0027).</summary>
public sealed class PendingEventRow
{
    public Guid Pk { get; set; }
    public Guid AggregatePkRef { get; set; }
    public string AggregateType { get; set; } = "";
    public string TargetEventType { get; set; } = "";
    public DateTimeOffset DueAt { get; set; }
    public string Status { get; set; } = "";
    public Guid? ScheduledByEvent { get; set; }
    public DateTimeOffset? FiredAt { get; set; }
}

/// <summary>
/// HiveAR's data access, provided to every command through the dispatcher.
/// In EF Core mode each command gets a pooled DbContext on the command's own
/// connection and transaction; its SaveChanges runs after the events have been
/// written and before the commit, so events and state stay atomic.
/// </summary>
public sealed class ArData
{
    readonly PooledDbContextFactory<ArDbContext>? factory;

    public ArData(StateAccess access)
    {
        Access = access;
        if (access == StateAccess.EfCore)
        {
            // The compiled model in Data/CompiledModel, generated with
            // "dotnet ef dbcontext optimize"; regenerate it whenever the model changes.
            factory = new PooledDbContextFactory<ArDbContext>(
                new DbContextOptionsBuilder<ArDbContext>().UseModel(CompiledModel.ArDbContextModel.Instance).UseNpgsql().Options);
        }
    }

    public StateAccess Access { get; }

    /// <summary>WAX_STATE=efcore selects EF Core (the harness side csharp-wax-ef);
    /// anything else, plain SQL, csharp-wax's own path.</summary>
    public static StateAccess FromEnvironment() =>
        string.Equals(Environment.GetEnvironmentVariable("WAX_STATE"), "efcore", StringComparison.OrdinalIgnoreCase)
            ? StateAccess.EfCore
            : StateAccess.Sql;

    /// <summary>The command's DbContext in EF Core mode, or null in plain SQL mode.</summary>
    public static ArDbContext? Ef(CommandContext context)
    {
        var data = context.Service<ArData>();
        if (data?.factory is not { } factory)
        {
            return null;
        }
        return context.Scoped(() =>
        {
            var db = factory.CreateDbContext();
            db.Database.SetDbConnection(context.Connection, contextOwnsConnection: false);
            db.Database.UseTransaction(context.Transaction);
            context.BeforeCommit(async cancellation =>
            {
                try
                {
                    await db.SaveChangesAsync(cancellation);
                }
                catch (DbUpdateConcurrencyException error)
                {
                    throw new WaxConcurrencyException(error.Message);
                }
            });
            context.OnEnd(() => db.DisposeAsync());
            return db;
        });
    }
}
