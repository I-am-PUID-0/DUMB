"""Audited InfiniDysk SQLite and PostgreSQL migration contracts."""

INFINIDYSK_POSTGRES_MIGRATIONS_V120 = (
    "20260818010011_InitializePostgresDatabase",
    "20260818200000_Add-ArticleMiss-Cache",
    "20260818210000_Copy-Legacy-Pipelining-Keys",
    "20260818220000_Add-Par2-Repair-Jobs",
)
INFINIDYSK_PROFILE_STREAM_INDEX_MIGRATION = (
    "20260822140000_Add-Profile-Stream-State-Index"
)
INFINIDYSK_GENERATED_SYMLINK_METADATA_MIGRATION = (
    "20260824143000_Add-Generated-Symlink-Metadata"
)

# Each entry binds the complete SQLite source schema and migration history to
# the PostgreSQL schema created by that InfiniDysk generation. Keep prior exact
# contracts so completed cutovers remain authorized after DUMB learns a newer
# upstream schema.
INFINIDYSK_DATABASE_CONTRACTS = (
    {
        "id": "v1.2.0",
        "adapter_schema": "infinidysk-postgres-v1.2.0",
        "sqlite_terminal_migration": "20260820160000_Normalize-Guid-Text-Casing",
        "sqlite_migration_count": 49,
        "sqlite_migration_history_fingerprint": (
            "321ace5aa1e2a72c23d92f95cf5f9173e4aa6bdba68ff8cfcc7c0cba48020834"
        ),
        "sqlite_schema_fingerprint": (
            "0fce2aa487e5741ba4cc6f5014b9e6e1d534634ad8698aa6e2a59e69d6d68c4e"
        ),
        "postgres_migrations": INFINIDYSK_POSTGRES_MIGRATIONS_V120,
        "postgres_schema_fingerprint": (
            "8d436ac58ce66de4f2bc44b97e5741735c516eee5fe296bea3f5dab7eca1424a"
        ),
    },
    {
        "id": "v1.2.3",
        "adapter_schema": "infinidysk-postgres-v1.2.3",
        "sqlite_terminal_migration": INFINIDYSK_PROFILE_STREAM_INDEX_MIGRATION,
        "sqlite_migration_count": 50,
        "sqlite_migration_history_fingerprint": (
            "3ce2ea6a9386d0554185eb70a098b50e20ca6365ceb80137332cc165039c54a4"
        ),
        "sqlite_schema_fingerprint": (
            "eae065a37d60f447a75132fb776a0cae783603fe769b1fcfd355e828b9e95574"
        ),
        "postgres_migrations": (
            *INFINIDYSK_POSTGRES_MIGRATIONS_V120,
            INFINIDYSK_PROFILE_STREAM_INDEX_MIGRATION,
        ),
        "postgres_schema_fingerprint": (
            "c82ddbfff24ef9522687796c6f41d6ead7d9694efbc690ac9d8cfe2c30425fa9"
        ),
    },
    {
        "id": "v1.2.5",
        "adapter_schema": "infinidysk-postgres-v1.2.5",
        "sqlite_terminal_migration": INFINIDYSK_GENERATED_SYMLINK_METADATA_MIGRATION,
        "sqlite_migration_count": 51,
        "sqlite_migration_history_fingerprint": (
            "9bce3501afceee53f435834ad703e1083a2d4f51c44bd16b6bb217a8d4d9955b"
        ),
        "sqlite_schema_fingerprint": (
            "42ade890f0f9394018630a3938c57d67acd0444b91acfaaca27289ed09fe80ae"
        ),
        "postgres_migrations": (
            *INFINIDYSK_POSTGRES_MIGRATIONS_V120,
            INFINIDYSK_PROFILE_STREAM_INDEX_MIGRATION,
            INFINIDYSK_GENERATED_SYMLINK_METADATA_MIGRATION,
        ),
        "postgres_schema_fingerprint": (
            "f9a845c95f4e218a0c3f36ea7eb1e14972f63a2ad6391382e3615d4cd0601902"
        ),
    },
)

# InfiniDysk recreates this scratch table and index while reconciling linked
# files. They are not application records and must not enter schema contracts,
# migration imports, or post-cutover authorization fingerprints.
INFINIDYSK_TRANSIENT_SCHEMA_OBJECTS = frozenset(
    {"TMP_LINKED_FILES", "TMP_LINKED_FILES_UNIQUE"}
)
