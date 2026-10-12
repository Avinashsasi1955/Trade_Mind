#!/usr/bin/env python3
"""Dedicated administrative utility for managing the isolated PostgreSQL test database (nivesh_test).

CRITICAL NETWORK AND SECURITY LIMITATIONS:
- Localhost IP and port validation (host in {'localhost', '127.0.0.1'}, port=5432,
  inet_server_addr() == '127.0.0.1' or None for Unix domain sockets) verifies only that
  the client connects to a local socket endpoint.
- IT CANNOT RULE OUT SSH LOCAL PORT FORWARDING (e.g. `ssh -L 5432:remote-server:5432`)
  or other local port proxies. If an SSH tunnel is active on port 5432, connections will
  be transparently forwarded to the remote server, and PostgreSQL server-side metadata
  (current_database(), current_user, version()) will reflect that remote server.
- Operators MUST ensure no SSH tunnels or forwarding proxies are active on port 5432.
- Defense-in-depth: strict target database name enforcement ('nivesh_test'), refusal of
  any forbidden database ('nivesh', 'nivesh_v3_staging', 'postgres'), and comprehensive
  database-object catalog inspection before any setup operation.

PERMITTED DEFAULT OBJECTS IN A PRISTINE POSTGRESQL DATABASE:
- Schemas: exactly ['public'] (plus system schemas: pg_catalog, information_schema, pg_toast)
- Extensions: only 'plpgsql' (installed by default in pg_catalog)
- Tables (relkind 'r', 'p'): 0
- Views (relkind 'v'): 0
- Materialized views (relkind 'm'): 0
- Sequences (relkind 'S'): 0
- Foreign tables (relkind 'f'): 0
- User-defined functions / procedures: 0
- User-defined types (enums 'e', composites 'c', domains 'd'): 0
- Migration ledger ('schema_migrations'): does not exist
Any presence of objects outside this permitted list triggers an immediate safety halt.

TRANSACTION CONTROL AND PARTIAL-MIGRATION RECOVERY RUNBOOK:
- Migrations v3_0 through v3_16 contain explicit BEGIN; and COMMIT; statements within
  the SQL text. Each migration commits its transaction independently.
- Migrations v3_17 and v3_18 do NOT contain internal BEGIN;/COMMIT; blocks. The runner
  manages per-file transaction boundaries explicitly by calling t_conn.commit() after
  each migration file.
- If migration N fails:
  - If N is v3_17 or v3_18, any uncommitted DDL within that specific failed file is rolled
    back via t_conn.rollback().
  - All migrations prior to N (migrations 0 through N-1) were ALREADY COMMITTED to PostgreSQL.
- Therefore, there is NO GLOBAL TRANSACTION ROLLBACK across all migration files.
- If any migration fails, the database is permanently left in an unclean partial state.
- On failure, this utility reports the exact failed migration, inspects the ledger, and
  marks the database as UNCLEAN. It NEVER automatically retries, resets, drops, or recreates it.
- SAFE MANUAL RECOVERY PROCESS:
  1. Never attempt to re-run setup on an unclean database (setup will fail closed).
  2. Inspect the migration error output and failure logs.
  3. Verify that the target is genuine localhost:5432 and not an active SSH tunnel.
  4. Once the migration error is diagnosed and fixed, and only when authorized by operator review,
     drop the disposable test database manually using an authorized maintenance connection
     (e.g., DROP DATABASE nivesh_test; on postgres).
  5. Run status action to confirm database_not_found before retrying setup on a fresh database.
- Cleanup/drop operations are EXPLICITLY DISABLED in Phase 2A.3 pending separate authorization.


MIGRATION LEDGER RECONCILIATION:
- Total migration files: 19 (v3_0 through v3_18).
- v3_0_production.sql pre-dated the migration ledger table (created in v3_1).
- v3_17_dual_mode_and_agentic_ledger.sql altered tables and added indexes but omitted an
  INSERT INTO schema_migrations statement.
- Consequently, exactly 17 migrations insert records into schema_migrations:
  v3_1 through v3_16 (16 entries) + v3_18 (1 entry) = 17 ledger entries.
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import psycopg2
from psycopg2.extras import Json

ROOT = Path(__file__).resolve().parent.parent

APPROVED_TEST_HOSTS = {"localhost", "127.0.0.1"}
APPROVED_TEST_PORT = 5432
APPROVED_TEST_DB = "nivesh_test"
FORBIDDEN_DATABASES = {"nivesh", "nivesh_v3_staging", "postgres", "template0", "template1"}

PERMITTED_DEFAULT_SCHEMAS: Set[str] = {"public"}
PERMITTED_DEFAULT_EXTENSIONS: Set[str] = {"plpgsql"}

EXPECTED_MIGRATION_MANIFEST: Dict[str, str] = {
    "v3_0_production.sql": "e4d917fcc501a37824d14a24cef733dbea69af6ed7ef7ed289fc85bdc0f79b56",
    "v3_1_application.sql": "b0c87fbf81f6de4e49abb93a26a2ae158b364c0fa48c93fdee9dbf3101f9b844",
    "v3_2_research_runtime.sql": "53e434f91d317b88497cce8846d1cf7f7620419ff46b38ea01a67079e092cf02",
    "v3_3_shadow_sessions.sql": "c7111f499d8a8e9b82179c0666908ca5e568a924af626689f7fa31ae4ca7bafe",
    "v3_4_promotion_engine.sql": "876e660dfbe908fccec3dd663aca55c2adddceb5d2de3f985005c8928b2e9ea4",
    "v3_5_security.sql": "ad8caed6dbe6a056f0775c66393572ada1888e911e42d7ceb2ea76487fa9cfa8",
    "v3_6_live_paper_pipeline.sql": "71b443c300155ef9faf69e6cf1157a959540107aea24e992b1f1df09ef68651e",
    "v3_7_release_operations.sql": "a067812217e938009bdfb249a4e3b4beff93d7ee0f33f0130d03f1322af1973e",
    "v3_8_model_policy_evidence.sql": "941825faf9fff7cbed2f32f8e839031268957ebcfd7cf5746cee284e07c46fb1",
    "v3_9_market_data_providers.sql": "66dcde8d799cdbffd67b131166baba4f1c4de1c13bc26880f90a1367c75ecdc9",
    "v3_10_shadow_session_pnl.sql": "6f464b432a36ed02bc162f48a1ab79e629fdfb9a77043f2bbde5d6acedf5fbb6",
    "v3_11_paper_trade_lifecycle.sql": "d15dc345e681116a3d30ed8a8520c1fc77add46f5d033943ade5b0554c41072f",
    "v3_12_model_quality_feedback.sql": "e6944cfceba3204b2cc4ae29a2a386ad085f77ee11a772a507e3ca634535dea7",
    "v3_13_upstox_option_chain_oi.sql": "7062e8f407b85bafd0920164c8dc89f20277fb55d20726defa380742f1ad9de6",
    "v3_14_adaptive_trade_intelligence.sql": "a79717e0c3098e2eda0446588c15696b12a2978564a6dd259d7110fa6c76039a",
    "v3_15_validation_auditability.sql": "a692a726e18b4547aae0297503f563f6df73054ad0337e121e13bb9d80493f90",
    "v3_16_one_second_candles.sql": "c8d96a26506c8634a06e198a78ce37619dd9e48435ebafcf163b05c5d2b2593a",
    "v3_17_dual_mode_and_agentic_ledger.sql": "9d85f9d780dffe970ffc4a03fa578b7f30414617a28e0c97e57b43314014c19e",
    "v3_18_shadow_session_code_version.sql": "90a58d343ec061984a2e99f34496293f0c06ceebe53d0a5279703f10d713a44b",
}

EXPECTED_LEDGER_ENTRIES: List[str] = [
    "v3_1_application",
    "v3_2_research_runtime",
    "v3_3_shadow_sessions",
    "v3_4_promotion_engine",
    "v3_5_security",
    "v3_6_live_paper_pipeline",
    "v3_7_release_operations",
    "v3_8_model_policy_evidence",
    "v3_9_market_data_providers",
    "v3_10_shadow_session_pnl",
    "v3_11_paper_trade_lifecycle",
    "v3_12_model_quality_feedback",
    "v3_13_upstox_option_chain_oi",
    "v3_14_adaptive_trade_intelligence",
    "v3_15_validation_auditability",
    "v3_16_one_second_candles",
    "v3_18_shadow_session_code_version",
]

EXPECTED_POSTGRES_EXTENSIONS: Set[str] = {
    "plpgsql",
    "pgcrypto",  # Added in v3_0_production.sql
}

EXPECTED_CUSTOM_TYPES: Set[str] = {
    "instrument_kind",
    "bar_interval",
    "order_side",
    "execution_status",
}

EXPECTED_USER_FUNCTIONS: Set[str] = {
    "public.enforce_hard_daily_drawdown",  # Created in v3_0_production.sql
    "public.block_unpromoted_trading",     # Created in v3_4_promotion_engine.sql
}

EXPECTED_SCHEMA_TABLES: Set[str] = {
    "account_equity_snapshots",
    "adaptive_trade_guardrails",
    "adaptive_trade_rewards",
    "agent_runs",
    "agentic_sentinel_logs",
    "ai_gateway_logs",
    "auth_login_attempts",
    "bot_conversations",
    "bot_messages",
    "broker_auth_states",
    "broker_connections",
    "corporate_action_ingestions",
    "corporate_actions",
    "derivative_plans",
    "drift_reports",
    "exchange_trading_calendar",
    "feature_rows",
    "forward_shadow_sessions",
    "history_ingestions",
    "history_sync_coverage",
    "holdings",
    "india_vix_history",
    "instrument_master",
    "instrument_provider_keys",
    "kill_switches",
    "live_feature_snapshots",
    "live_market_bars",
    "market_data_gaps",
    "ml_feature_observations",
    "model_drift_reports",
    "model_quality_feedback",
    "model_release_manifests",
    "model_versions",
    "monitoring_events",
    "news_article_symbols",
    "news_articles",
    "news_articles_v3",
    "news_ingestion_logs",
    "news_sentiment_scores",
    "operational_preflight_runs",
    "option_chain_oi_snapshots",
    "order_events",
    "order_execution_events",
    "order_execution_ledger",
    "order_intents",
    "portfolios",
    "revoked_tokens",
    "risk_control_state",
    "risk_policies",
    "schema_migrations",
    "sentiment_snapshots",
    "settings",
    "shadow_execution_audits",
    "shadow_predictions",
    "shadow_session_daily_log",
    "stock_analyses",
    "system_promotion_ledger",
    "trade_candidate_audits",
    "trades",
    "universe_membership",
    "users",
    "validation_runs",
    "watchlist_snapshots",
}

EXPECTED_SEQUENCES: Set[str] = {
    "account_equity_snapshots_id_seq",
    "adaptive_trade_rewards_id_seq",
    "agent_runs_id_seq",
    "agentic_sentinel_logs_id_seq",
    "ai_gateway_logs_id_seq",
    "auth_login_attempts_id_seq",
    "bot_conversations_id_seq",
    "bot_messages_id_seq",
    "derivative_plans_id_seq",
    "drift_reports_id_seq",
    "holdings_id_seq",
    "instrument_master_id_seq",
    "market_data_gaps_id_seq",
    "model_drift_reports_id_seq",
    "model_quality_feedback_id_seq",
    "model_versions_id_seq",
    "monitoring_events_id_seq",
    "news_articles_id_seq",
    "news_ingestion_logs_id_seq",
    "operational_preflight_runs_id_seq",
    "option_chain_oi_snapshots_id_seq",
    "order_events_id_seq",
    "order_execution_events_id_seq",
    "order_intents_id_seq",
    "sentiment_snapshots_id_seq",
    "shadow_execution_audits_id_seq",
    "shadow_predictions_id_seq",
    "stock_analyses_id_seq",
    "trade_candidate_audits_id_seq",
    "trades_id_seq",
    "users_id_seq",
    "validation_runs_id_seq",
    "watchlist_snapshots_id_seq",
}


def redact_dsn(dsn: str) -> str:
    """Strips username and password from connection strings for safe logging."""
    try:
        clean = dsn.replace("postgresql+psycopg2://", "postgresql://", 1)
        parsed = urllib.parse.urlsplit(clean)
        host_port = f"{parsed.hostname or 'localhost'}:{parsed.port or APPROVED_TEST_PORT}"
        return f"postgresql://***:***@{host_port}/{parsed.path.lstrip('/')}"
    except Exception:
        return "postgresql://***:***@[redacted]"


def validate_postgres_test_url(raw_url: str) -> str:
    """Strictly validates POSTGRES_TEST_DATABASE_URL against security and isolation policies.

    Rejects:
    - Empty or whitespace-only URLs
    - Non-postgresql schemes
    - URL fragments (#...) and fragment delimiters ('#')
    - Query parameters (?...) and query delimiters ('?')
    - Extra path components or trailing slashes (path must be strictly '/nivesh_test')
    - Forbidden databases (nivesh, nivesh_v3_staging, postgres)
    - Non-test databases (anything other than nivesh_test)
    - Remote hosts (anything other than localhost or 127.0.0.1)
    - Malformed or non-approved ports (anything other than 5432)

    Never prints raw connection strings or credentials in exception messages.
    """
    if not raw_url or not isinstance(raw_url, str) or not raw_url.strip():
        raise RuntimeError(
            "FAIL-CLOSED: RUN_POSTGRES=1 requires explicit POSTGRES_TEST_DATABASE_URL. "
            "Refusing to fall back to ambient DATABASE_URL or application .env."
        )

    clean = raw_url.strip()

    # Reject query and fragment delimiters explicitly (including trailing '?' and '#')
    if "?" in clean:
        raise RuntimeError("FAIL-CLOSED: Query parameters and delimiters ('?') are strictly forbidden in test connection strings.")

    if "#" in clean:
        raise RuntimeError("FAIL-CLOSED: URL fragments and delimiters ('#') are strictly forbidden in test connection strings.")

    try:
        clean_dsn = clean.replace("postgresql+psycopg2://", "postgresql://", 1)
        parsed = urllib.parse.urlsplit(clean_dsn)
    except Exception as parse_err:
        raise RuntimeError(f"FAIL-CLOSED: Malformed database URL structure: {type(parse_err).__name__}") from None

    if parsed.scheme != "postgresql":
        raise RuntimeError(f"FAIL-CLOSED: Disallowed scheme '{parsed.scheme}'. Must be 'postgresql'.")

    # Path must be strictly '/nivesh_test' with no trailing slash and no extra path components
    if parsed.path != f"/{APPROVED_TEST_DB}":
        raise RuntimeError(
            f"SAFETY GATE TRIGGERED: Path must be exactly '/{APPROVED_TEST_DB}' without trailing slashes or extra path components."
        )

    try:
        host = (parsed.hostname or "").lower()
    except Exception:
        raise RuntimeError("FAIL-CLOSED: Malformed host in database connection URL.")

    if host not in APPROVED_TEST_HOSTS:
        raise RuntimeError(
            f"FAIL-CLOSED: Disallowed host '{host}'. Only approved local endpoints ({APPROVED_TEST_HOSTS}) are permitted."
        )

    try:
        port = parsed.port
    except ValueError as port_err:
        raise RuntimeError(f"FAIL-CLOSED: Malformed port in database connection URL: {port_err}") from None

    if port != APPROVED_TEST_PORT:
        raise RuntimeError(f"FAIL-CLOSED: Disallowed port {port}. Strictly {APPROVED_TEST_PORT} required.")

    return clean


def validate_test_connection_params(
    host: str, port: int, dbname: str, allow_maintenance: bool = False
) -> None:
    """Validates target database parameters prior to any connection attempt."""
    clean_host = host.strip().lower()
    if clean_host not in APPROVED_TEST_HOSTS:
        raise ValueError(
            f"SAFETY VIOLATION: Disallowed host '{clean_host}'. Only localhost ({APPROVED_TEST_HOSTS}) is permitted."
        )

    if port != APPROVED_TEST_PORT:
        raise ValueError(
            f"SAFETY VIOLATION: Disallowed port {port}. Approved test port is strictly {APPROVED_TEST_PORT}."
        )

    clean_db = dbname.strip()
    if not clean_db:
        raise ValueError("SAFETY VIOLATION: Database name cannot be empty.")

    if allow_maintenance and clean_db == "postgres":
        return

    if clean_db in FORBIDDEN_DATABASES or clean_db != APPROVED_TEST_DB:
        raise ValueError(
            f"SAFETY VIOLATION: Refusing to operate on database '{clean_db}'. "
            f"Target database must be strictly '{APPROVED_TEST_DB}'."
        )


def verify_connection_metadata(conn: psycopg2.extensions.connection, expected_db: str) -> Dict:
    """Queries server-side session metadata to verify database identity before any operation.

    SECURITY AND NETWORK BOUNDARY LIMITATIONS:
    - Client-side URL validation confirms only the destination hostname/port string.
    - Server-side metadata queries confirm the identity, role, and recovery status of
      the PostgreSQL instance that actually accepted the connection.
    - HOWEVER, NEITHER CLIENT-SIDE VALIDATION NOR SERVER METADATA CAN CONCLUSIVELY RULE OUT
      LOCAL PORT FORWARDING (e.g. `ssh -L 5432:...`), local iptables redirects, or transparent
      proxies that forward localhost:5432 to an external or remote instance.
    - Operators MUST independently ensure that no forwarding processes or tunnels exist.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_database(), current_user, inet_server_addr(), inet_server_port(), pg_is_in_recovery(), version();"
            )
            row = cur.fetchone()
            actual_db, actual_user, server_addr, server_port, in_recovery, pg_version = row

        if actual_db != expected_db:
            raise RuntimeError(
                f"SAFETY VIOLATION: Connected database '{actual_db}' does not match expected '{expected_db}'. "
                "Aborting immediately."
            )

        if actual_db in FORBIDDEN_DATABASES and actual_db != "postgres":
            raise RuntimeError(
                f"SAFETY VIOLATION: Connected database is forbidden target '{actual_db}'. Aborting immediately."
            )

        if in_recovery:
            raise RuntimeError("SAFETY VIOLATION: Target PostgreSQL server is a read-only replica.")

        return {
            "database": actual_db,
            "user": actual_user,
            "server_addr": str(server_addr) if server_addr else "local_socket",
            "server_port": server_port,
            "in_recovery": in_recovery,
            "pg_version": pg_version,
        }
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise


def validate_migration_manifest(migrations_dir: Path) -> List[Dict]:
    """Read-only preflight: validates file presence, sequence ordering, and SHA-256 checksums."""
    if not migrations_dir.is_dir():
        raise FileNotFoundError(f"Migrations directory not found: {migrations_dir}")

    disk_files = {f.name: f for f in migrations_dir.glob("*.sql")}
    expected_names = list(EXPECTED_MIGRATION_MANIFEST.keys())

    # Check for missing files
    missing = set(expected_names) - set(disk_files.keys())
    if missing:
        raise ValueError(f"PREFLIGHT FAILED: Missing expected migration files: {sorted(missing)}")

    # Check for unexpected extra SQL files
    extra = set(disk_files.keys()) - set(expected_names)
    if extra:
        raise ValueError(f"PREFLIGHT FAILED: Unexpected unmanifested SQL files found: {sorted(extra)}")

    # Verify contiguous numbering
    indices = []
    for name in expected_names:
        parts = name.split("_")
        if len(parts) < 2 or not parts[0].startswith("v") or not parts[1].isdigit():
            raise ValueError(f"PREFLIGHT FAILED: Invalid migration naming format: '{name}'")
        indices.append(int(parts[1]))

    expected_indices = list(range(len(expected_names)))
    if indices != expected_indices:
        raise ValueError(
            f"PREFLIGHT FAILED: Migration sequence ordering mismatch. Found indices {indices}, expected {expected_indices}."
        )

    # Verify SHA-256 checksums
    verified_files = []
    for name in expected_names:
        file_path = disk_files[name]
        content = file_path.read_bytes()
        actual_hash = hashlib.sha256(content).hexdigest()
        expected_hash = EXPECTED_MIGRATION_MANIFEST[name]
        if actual_hash != expected_hash:
            raise ValueError(
                f"PREFLIGHT FAILED: Checksum mismatch for '{name}'. "
                f"Expected {expected_hash}, calculated {actual_hash}."
            )
        verified_files.append({"file": name, "sha256": actual_hash, "size_bytes": len(content)})

    return verified_files


def inspect_database_objects(conn: psycopg2.extensions.connection) -> Dict[str, Any]:
    """Inspects all schemas and database objects to evaluate pristine or partial state.

    Permitted default objects in a freshly created PostgreSQL database:
    - Schemas: only 'public' (plus system schemas: pg_catalog, information_schema, pg_toast)
    - Extensions: only 'plpgsql' (in pg_catalog)
    - Tables, views, materialized views, sequences, foreign tables: 0
    - User-defined functions, procedures: 0
    - Custom types (enums, composites, domains): 0
    - Migration ledger ('schema_migrations'): does not exist
    """
    with conn.cursor() as cur:
        # 1. Non-system schemas (anything not starting with pg_ and not information_schema)
        cur.execute(
            """
            SELECT nspname FROM pg_namespace
            WHERE nspname NOT LIKE 'pg_%' AND nspname != 'information_schema';
            """
        )
        user_schemas = [row[0] for row in cur.fetchall()]

        # 2. Installed extensions
        cur.execute("SELECT extname, extversion FROM pg_extension;")
        extensions = {row[0]: row[1] for row in cur.fetchall()}

        # 3. Relations by relkind in user schemas
        cur.execute(
            """
            SELECT c.relkind, n.nspname, c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname != 'information_schema'
              AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f');
            """
        )
        rel_rows = cur.fetchall()
        table_names = {r[2] for r in rel_rows if r[0] in ('r', 'p')}
        view_names = {r[2] for r in rel_rows if r[0] == 'v'}
        matview_names = {r[2] for r in rel_rows if r[0] == 'm'}
        sequence_names = {r[2] for r in rel_rows if r[0] == 'S'}
        foreign_table_names = {r[2] for r in rel_rows if r[0] == 'f'}

        # 4. User-defined functions / procedures (excluding extension-packaged functions like pgcrypto)
        cur.execute(
            """
            SELECT p.proname, n.nspname
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            LEFT JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e'
            WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname != 'information_schema'
              AND d.objid IS NULL;
            """
        )
        user_funcs = [f"{r[1]}.{r[0]}" for r in cur.fetchall()]

        # 5. User-defined types (enums 'e', composites 'c', domains 'd')
        cur.execute(
            """
            SELECT t.typname, t.typtype, n.nspname
            FROM pg_type t
            JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname != 'information_schema'
              AND t.typtype IN ('e', 'c', 'd')
              AND NOT EXISTS (
                  SELECT 1 FROM pg_class c WHERE c.reltype = t.oid
              );
            """
        )
        type_rows = cur.fetchall()
        custom_types = {r[0]: r[1] for r in type_rows}

        # 6. Schema migrations ledger
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'schema_migrations'
            );
            """
        )
        has_migrations_table = cur.fetchone()[0]
        applied_migrations = []
        if has_migrations_table:
            cur.execute("SELECT version FROM schema_migrations ORDER BY applied_at;")
            applied_migrations = [row[0] for row in cur.fetchall()]

        # 7. Fixtures (if relevant tables exist)
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='instrument_master');"
        )
        has_inst = cur.fetchone()[0]
        instrument_count = 0
        if has_inst:
            cur.execute("SELECT COUNT(*) FROM instrument_master;")
            instrument_count = cur.fetchone()[0]

        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='live_market_bars');"
        )
        has_bars = cur.fetchone()[0]
        bar_count = 0
        if has_bars:
            cur.execute("SELECT COUNT(*) FROM live_market_bars;")
            bar_count = cur.fetchone()[0]

        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='model_versions');"
        )
        has_models = cur.fetchone()[0]
        active_model_count = 0
        if has_models:
            cur.execute("SELECT COUNT(*) FROM model_versions WHERE status='active';")
            active_model_count = cur.fetchone()[0]

        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='history_sync_coverage');"
        )
        has_coverage = cur.fetchone()[0]
        coverage_bars = 0
        if has_coverage:
            cur.execute("SELECT COALESCE(SUM(bars), 0) FROM history_sync_coverage WHERE status='complete';")
            coverage_bars = int(cur.fetchone()[0])

    # Evaluate pristine empty state
    unexpected_schemas = set(user_schemas) - PERMITTED_DEFAULT_SCHEMAS
    unexpected_extensions = set(extensions.keys()) - PERMITTED_DEFAULT_EXTENSIONS

    is_pristine = (
        len(unexpected_schemas) == 0
        and len(unexpected_extensions) == 0
        and len(table_names) == 0
        and len(view_names) == 0
        and len(matview_names) == 0
        and len(sequence_names) == 0
        and len(foreign_table_names) == 0
        and len(user_funcs) == 0
        and len(custom_types) == 0
        and not has_migrations_table
    )

    unexpected_objects = {}
    if unexpected_schemas:
        unexpected_objects["unexpected_schemas"] = sorted(unexpected_schemas)
    if unexpected_extensions:
        unexpected_objects["unexpected_extensions"] = sorted(unexpected_extensions)
    if table_names:
        unexpected_objects["tables"] = sorted(table_names)
    if view_names:
        unexpected_objects["views"] = sorted(view_names)
    if matview_names:
        unexpected_objects["matviews"] = sorted(matview_names)
    if sequence_names:
        unexpected_objects["sequences"] = sorted(sequence_names)
    if foreign_table_names:
        unexpected_objects["foreign_tables"] = sorted(foreign_table_names)
    if user_funcs:
        unexpected_objects["functions"] = sorted(user_funcs)
    if custom_types:
        unexpected_objects["custom_types"] = custom_types
    if has_migrations_table:
        unexpected_objects["has_migrations_ledger"] = True

    return {
        "is_pristine": is_pristine,
        "unexpected_objects": unexpected_objects,
        "schemas": user_schemas,
        "extensions": extensions,
        "tables": table_names,
        "views": view_names,
        "matviews": matview_names,
        "sequences": sequence_names,
        "functions": user_funcs,
        "custom_types": custom_types,
        "has_migrations_table": has_migrations_table,
        "applied_migrations": applied_migrations,
        "fixtures": {
            "instruments": instrument_count,
            "bars": bar_count,
            "active_models": active_model_count,
            "coverage_bars": coverage_bars,
        },
    }


def connect_maintenance(host: str, port: int, user: Optional[str] = None) -> psycopg2.extensions.connection:
    """Opens a connection to the maintenance database 'postgres' with autocommit for DDL."""
    validate_test_connection_params(host, port, "postgres", allow_maintenance=True)
    conn_params = {"host": host, "port": port, "dbname": "postgres", "connect_timeout": 5}
    if user:
        conn_params["user"] = user
    conn = psycopg2.connect(**conn_params)
    conn.autocommit = True
    verify_connection_metadata(conn, "postgres")
    return conn


def connect_test_db(host: str, port: int, user: Optional[str] = None) -> psycopg2.extensions.connection:
    """Opens a connection to 'nivesh_test' after rigorous pre- and post-connection validation."""
    validate_test_connection_params(host, port, APPROVED_TEST_DB, allow_maintenance=False)
    conn_params = {"host": host, "port": port, "dbname": APPROVED_TEST_DB, "connect_timeout": 5}
    if user:
        conn_params["user"] = user
    conn = psycopg2.connect(**conn_params)
    verify_connection_metadata(conn, APPROVED_TEST_DB)
    return conn


def get_status(host: str, port: int, user: Optional[str] = None) -> Dict:
    """Read-only diagnostic: checks migration manifest, server metadata, and database state."""
    validate_test_connection_params(host, port, APPROVED_TEST_DB)

    # 1. Manifest preflight
    migrations_dir = ROOT / "migrations" / "postgres"
    manifest_results = validate_migration_manifest(migrations_dir)

    # 2. Check if nivesh_test exists via maintenance connection
    m_conn = connect_maintenance(host, port, user)
    m_meta = verify_connection_metadata(m_conn, "postgres")
    db_exists = False
    try:
        with m_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (APPROVED_TEST_DB,))
            db_exists = bool(cur.fetchone())
    finally:
        m_conn.close()

    if not db_exists:
        return {
            "status": "database_not_found",
            "database": APPROVED_TEST_DB,
            "host": host,
            "port": port,
            "exists": False,
            "is_clean_for_setup": True,
            "is_ready_for_tests": False,
            "requires_reset": False,
            "server_metadata": m_meta,
            "manifest_verified_count": len(manifest_results),
            "permitted_default_objects": {
                "schemas": sorted(PERMITTED_DEFAULT_SCHEMAS),
                "extensions": sorted(PERMITTED_DEFAULT_EXTENSIONS),
                "tables": 0,
                "views": 0,
                "functions": 0,
                "enums": 0,
            },
            "notice": "Database does not exist yet. Clean for initial setup.",
        }

    # 3. Comprehensive database object inspection of nivesh_test
    t_conn = connect_test_db(host, port, user)
    try:
        t_meta = verify_connection_metadata(t_conn, APPROVED_TEST_DB)
        db_inventory = inspect_database_objects(t_conn)

        if db_inventory["is_pristine"]:
            return {
                "status": "empty_ready_for_setup",
                "database": APPROVED_TEST_DB,
                "host": host,
                "port": port,
                "exists": True,
                "is_clean_for_setup": True,
                "is_ready_for_tests": False,
                "requires_reset": False,
                "server_metadata": t_meta,
                "manifest_verified_count": len(manifest_results),
                "notice": "Database exists and is completely pristine. Ready for setup.",
            }

        # Rigorous migration-ledger and schema verification
        applied = db_inventory["applied_migrations"]
        applied_set = set(applied)
        expected_ledger_set = set(EXPECTED_LEDGER_ENTRIES)

        ledger_errors = []
        # Check for duplicates in applied ledger
        if len(applied) != len(applied_set):
            ledger_errors.append("Duplicate migration versions found in schema_migrations ledger.")

        # Check for unexpected ledger entries
        unexpected_ledger = applied_set - expected_ledger_set
        if unexpected_ledger:
            ledger_errors.append(f"Unexpected migration entries in ledger: {sorted(unexpected_ledger)}")

        # Check for missing ledger entries
        missing_ledger = expected_ledger_set - applied_set
        if missing_ledger:
            ledger_errors.append(f"Missing expected migration entries: {sorted(missing_ledger)}")

        # Check ordered sequence
        if not missing_ledger and not unexpected_ledger and applied != EXPECTED_LEDGER_ENTRIES:
            ledger_errors.append("Applied migrations are out of expected sequence order.")

        # Check expected schema tables (both missing and unexpected)
        existing_tables = set(db_inventory["tables"])
        missing_tables = EXPECTED_SCHEMA_TABLES - existing_tables
        if missing_tables:
            ledger_errors.append(f"Missing required application tables: {sorted(missing_tables)}")

        unexpected_tables = existing_tables - EXPECTED_SCHEMA_TABLES
        if unexpected_tables:
            ledger_errors.append(f"Unexpected application tables detected: {sorted(unexpected_tables)}")

        # Check expected custom enum types (both missing and unexpected)
        existing_types = set(db_inventory["custom_types"].keys())
        missing_types = EXPECTED_CUSTOM_TYPES - existing_types
        if missing_types:
            ledger_errors.append(f"Missing required custom types: {sorted(missing_types)}")

        unexpected_types = existing_types - EXPECTED_CUSTOM_TYPES
        if unexpected_types:
            ledger_errors.append(f"Unexpected custom types detected: {sorted(unexpected_types)}")

        # Check expected extensions (both missing and unexpected)
        existing_extensions = set(db_inventory.get("extensions", {}).keys())
        missing_extensions = EXPECTED_POSTGRES_EXTENSIONS - existing_extensions
        if missing_extensions:
            ledger_errors.append(f"Missing required extensions: {sorted(missing_extensions)}")

        unexpected_extensions = existing_extensions - EXPECTED_POSTGRES_EXTENSIONS
        if unexpected_extensions:
            ledger_errors.append(f"Unexpected extensions detected: {sorted(unexpected_extensions)}")

        # Check expected user functions (both missing and unexpected)
        existing_functions = set(db_inventory.get("functions", []))
        missing_functions = EXPECTED_USER_FUNCTIONS - existing_functions
        if missing_functions:
            ledger_errors.append(f"Missing required user functions: {sorted(missing_functions)}")

        unexpected_functions = existing_functions - EXPECTED_USER_FUNCTIONS
        if unexpected_functions:
            ledger_errors.append(f"Unexpected user functions detected: {sorted(unexpected_functions)}")

        # Check expected sequences (both missing and unexpected)
        existing_sequences = set(db_inventory.get("sequences", []))
        missing_sequences = EXPECTED_SEQUENCES - existing_sequences
        if missing_sequences:
            ledger_errors.append(f"Missing required sequences: {sorted(missing_sequences)}")

        unexpected_sequences = existing_sequences - EXPECTED_SEQUENCES
        if unexpected_sequences:
            ledger_errors.append(f"Unexpected sequences detected: {sorted(unexpected_sequences)}")

        # Check for unexpected views, matviews, foreign tables, or schemas
        if db_inventory.get("views"):
            ledger_errors.append(f"Unexpected views detected: {sorted(db_inventory['views'])}")

        if db_inventory.get("matviews"):
            ledger_errors.append(f"Unexpected materialized views detected: {sorted(db_inventory['matviews'])}")

        if db_inventory.get("foreign_tables"):
            ledger_errors.append(f"Unexpected foreign tables detected: {sorted(db_inventory['foreign_tables'])}")

        unexpected_schemas = set(db_inventory.get("schemas", [])) - PERMITTED_DEFAULT_SCHEMAS
        if unexpected_schemas:
            ledger_errors.append(f"Unexpected schemas detected: {sorted(unexpected_schemas)}")

        if ledger_errors:
            return {
                "status": "partial_migration_unclean",
                "database": APPROVED_TEST_DB,
                "host": host,
                "port": port,
                "exists": True,
                "is_clean_for_setup": False,
                "is_ready_for_tests": False,
                "requires_reset": True,
                "ledger_errors": ledger_errors,
                "applied_migrations_count": len(applied),
                "applied_migrations": applied,
                "server_metadata": t_meta,
                "manifest_verified_count": len(manifest_results),
                "error": "Database schema or migration ledger is inconsistent or partially initialized.",
            }

        # Verify deterministic fixtures
        fix = db_inventory["fixtures"]
        fixtures_valid = (
            fix["instruments"] == 1
            and fix["bars"] == 5
            and fix["active_models"] == 1
            and fix["coverage_bars"] == 5
        )
        if not fixtures_valid:
            return {
                "status": "fixtures_invalid_unclean",
                "database": APPROVED_TEST_DB,
                "host": host,
                "port": port,
                "exists": True,
                "is_clean_for_setup": False,
                "is_ready_for_tests": False,
                "requires_reset": True,
                "applied_migrations_count": len(applied),
                "fixtures": fix,
                "server_metadata": t_meta,
                "manifest_verified_count": len(manifest_results),
                "error": "Migrations fully verified, but seeded test fixtures do not match deterministic expectations.",
            }

        return {
            "status": "ready",
            "database": APPROVED_TEST_DB,
            "host": host,
            "port": port,
            "exists": True,
            "is_clean_for_setup": False,
            "is_ready_for_tests": True,
            "requires_reset": False,
            "applied_migrations_count": len(applied),
            "applied_migrations": applied,
            "fixtures": fix,
            "server_metadata": t_meta,
            "manifest_verified_count": len(manifest_results),
        }
    finally:
        t_conn.close()


def setup_test_database(
    host: str,
    port: int,
    user: Optional[str] = None,
    confirm_target: str = "",
) -> Dict:
    """Provisions nivesh_test, applies all 19 migrations, and seeds minimal deterministic fixtures.

    Refuses any database with existing tables, types, or partial migration state.
    """
    if confirm_target != APPROVED_TEST_DB:
        raise ValueError(
            f"SAFETY VIOLATION: Execution aborted. --confirm-setup must be explicitly set to '{APPROVED_TEST_DB}'."
        )

    validate_test_connection_params(host, port, APPROVED_TEST_DB)

    # 1. Read-only preflight of migrations manifest before any DB operation
    migrations_dir = ROOT / "migrations" / "postgres"
    print("[setup] Performing read-only migration manifest preflight...")
    validate_migration_manifest(migrations_dir)
    print(f"[setup] Manifest verified: all {len(EXPECTED_MIGRATION_MANIFEST)} migrations intact with matching checksums.")

    # 2. Ensure target database exists via maintenance connection
    m_conn = connect_maintenance(host, port, user)
    try:
        with m_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (APPROVED_TEST_DB,))
            if not cur.fetchone():
                print(f"[setup] Creating empty database '{APPROVED_TEST_DB}'...")
                cur.execute(f'CREATE DATABASE "{APPROVED_TEST_DB}";')
            else:
                print(f"[setup] Database '{APPROVED_TEST_DB}' already exists.")
    finally:
        m_conn.close()

    # 3. Connect to nivesh_test and inspect schema emptiness and unexpected objects
    t_conn = connect_test_db(host, port, user)
    try:
        inventory = inspect_database_objects(t_conn)
        if not inventory["is_pristine"]:
            raise RuntimeError(
                f"SAFETY GATE TRIGGERED: Database '{APPROVED_TEST_DB}' is not in a pristine, empty state. "
                f"Unexpected objects detected: {inventory['unexpected_objects']}. "
                "Setup strictly refuses any database with an existing schema, unexpected objects, or partial migration state. "
                "Manual review/reset is required before retrying."
            )

        # 4. Apply migrations sequentially
        # NOTICE: v3_0 through v3_16 contain internal COMMIT; statements and commit individually.
        # v3_17 and v3_18 do not contain internal transaction blocks; they are explicitly committed per file.
        applied_count = 0
        current_mig = None
        try:
            with t_conn.cursor() as cur:
                for migration_file in EXPECTED_MIGRATION_MANIFEST.keys():
                    current_mig = migration_file
                    file_path = migrations_dir / migration_file
                    sql = file_path.read_text(encoding="utf-8")

                    # Immediate pre-execution checksum validation (guarantees exact manifest match at point-of-use)
                    actual_sha = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                    expected_sha = EXPECTED_MIGRATION_MANIFEST[migration_file]
                    if actual_sha != expected_sha:
                        raise ValueError(
                            f"PRE-EXECUTION INTEGRITY VIOLATION for '{migration_file}': "
                            f"Expected hash {expected_sha}, found {actual_sha}."
                        )

                    print(f"[setup] Applying {migration_file}...")
                    cur.execute(sql)
                    t_conn.commit()
                    applied_count += 1

        except Exception as mig_err:
            try:
                t_conn.rollback()
            except Exception:
                pass

            # Inspect ledger state following failure
            committed_in_ledger = []
            try:
                with t_conn.cursor() as cur:
                    cur.execute("SELECT version FROM schema_migrations ORDER BY applied_at;")
                    committed_in_ledger = [r[0] for r in cur.fetchall()]
            except Exception:
                pass

            print(
                f"\n{'=' * 75}\n"
                f"[FATAL SETUP FAILURE] Migration failed on '{current_mig}': {mig_err}\n"
                f"STATUS: PARTIAL_MIGRATION_UNCLEAN\n"
                f"NOTICE: Migrations prior to '{current_mig}' committed independently.\n"
                f"Committed in ledger prior to failure: {committed_in_ledger}\n"
                f"Database '{APPROVED_TEST_DB}' is in a DIRTY, PARTIALLY MIGRATED state.\n"
                f"RECOVERY POLICY: The utility will NEVER automatically retry, reset, drop, or recreate this database.\n"
                f"Human review and explicit maintenance action is required.\n"
                f"{'=' * 75}\n",
                file=sys.stderr,
            )
            raise RuntimeError(
                f"SETUP_FAILED_DIRTY_STATE: Halted at '{current_mig}'. "
                f"Database '{APPROVED_TEST_DB}' contains partial migrations and requires manual review."
            ) from mig_err

        # 5. Seed deterministic test fixtures without ON CONFLICT DO NOTHING
        print("[setup] Seeding deterministic test fixtures (strict insert)...")
        try:
            with t_conn.cursor() as cur:
                # 5a. Instrument master (1 row: RELIANCE)
                cur.execute(
                    """
                    INSERT INTO instrument_master (
                        id, exchange, symbol, instrument_token, instrument_type,
                        lot_size, tick_size, is_active, valid_from
                    ) VALUES (
                        1001, 'NSE', 'RELIANCE', 738561, 'EQ'::instrument_kind,
                        1, 0.05, TRUE, CURRENT_TIMESTAMP
                    );
                    """
                )

                # 5b. Live market bars (5 daily candles)
                candles_data = [
                    ("2026-01-01 09:15:00+05:30", 2400.0, 2420.0, 2390.0, 2410.0, 100000),
                    ("2026-01-02 09:15:00+05:30", 2410.0, 2430.0, 2400.0, 2420.0, 110000),
                    ("2026-01-05 09:15:00+05:30", 2420.0, 2440.0, 2410.0, 2430.0, 120000),
                    ("2026-01-06 09:15:00+05:30", 2430.0, 2450.0, 2420.0, 2440.0, 130000),
                    ("2026-01-07 09:15:00+05:30", 2440.0, 2460.0, 2430.0, 2450.0, 140000),
                ]
                for bar_time, open_p, high_p, low_p, close_p, vol in candles_data:
                    cur.execute(
                        """
                        INSERT INTO live_market_bars (
                            instrument_id, interval, bar_time, open_price, high_price,
                            low_price, close_price, volume, open_interest, source
                        ) VALUES (
                            1001, 'day'::bar_interval, %s, %s, %s, %s, %s, %s, 0, 'fixture'
                        );
                        """,
                        (bar_time, open_p, high_p, low_p, close_p, vol),
                    )

                # 5c. History sync coverage
                cur.execute(
                    """
                    INSERT INTO history_sync_coverage (
                        instrument_id, interval, first_timestamp, last_timestamp,
                        bars, status, updated_at
                    ) VALUES (
                        1001, 'day'::bar_interval, '2026-01-01 09:15:00+05:30',
                        '2026-01-07 09:15:00+05:30', 5, 'complete', CURRENT_TIMESTAMP
                    );
                    """
                )

                # 5d. Active model version
                model_payload = {
                    "live_eligible": False,
                    "features": ["return_1d"],
                    "weights": [0.0],
                }
                model_metrics = {"accuracy": 0.55}
                cur.execute(
                    """
                    INSERT INTO model_versions (
                        id, model_name, version, feature_set, algorithm, payload,
                        training_samples, metrics, status, created_at
                    ) VALUES (
                        1001, 'shadow_ensemble', 'v3_test_01', 'daily_v2', 'logistic_calibrated',
                        %s, 100, %s, 'active', CURRENT_TIMESTAMP
                    );
                    """,
                    (Json(model_payload), Json(model_metrics)),
                )

                # 5e. Calendar dates for NSE 2026
                calendar_file = ROOT / "scripts" / "nse_holidays_2026.json"
                if calendar_file.exists():
                    holidays_data = json.loads(calendar_file.read_text(encoding="utf-8"))
                    trading_days = holidays_data.get("trading_days", [])
                    for day_str in trading_days:
                        cur.execute(
                            """
                            INSERT INTO exchange_trading_calendar (
                                exchange, session_date, session_status, source
                            ) VALUES (
                                'NSE', %s, 'OPEN', 'nse_holidays_2026.json'
                            );
                            """,
                            (day_str,),
                        )

                # 5f. Post-insert fixture verification (fail-closed)
                cur.execute("SELECT id, exchange, symbol FROM instrument_master WHERE symbol='RELIANCE';")
                inst_row = cur.fetchone()
                if not inst_row or inst_row[0] != 1001:
                    raise RuntimeError("FIXTURE VERIFICATION FAILED: Instrument RELIANCE missing or invalid.")

                cur.execute("SELECT COUNT(*) FROM live_market_bars WHERE instrument_id=1001;")
                bar_cnt = cur.fetchone()[0]
                if bar_cnt != 5:
                    raise RuntimeError(f"FIXTURE VERIFICATION FAILED: Expected 5 bars for RELIANCE, got {bar_cnt}.")

                cur.execute("SELECT version, status FROM model_versions WHERE status='active';")
                model_row = cur.fetchone()
                if not model_row or model_row[0] != "v3_test_01":
                    raise RuntimeError("FIXTURE VERIFICATION FAILED: Active model version missing or invalid.")

                # Optimize planner statistics across all tables
                cur.execute("ANALYZE;")


            t_conn.commit()
            print("[setup] Setup and fixture seeding completed and verified successfully.")
            return {
                "status": "success",
                "database": APPROVED_TEST_DB,
                "migrations_applied": applied_count,
                "fixtures_seeded": True,
            }
        except Exception as fix_err:
            t_conn.rollback()
            raise RuntimeError(f"FIXTURE_SEEDING_FAILED: {fix_err}") from fix_err
    finally:
        t_conn.close()


def cleanup_test_database(
    host: str, port: int, user: Optional[str] = None, confirm_target: str = ""
) -> Dict:
    """Explicitly disabled pending separate authorization and review."""
    raise RuntimeError(
        "SAFETY LOCK: Database cleanup/drop is explicitly disabled in Phase 2A.3. "
        "No database drops are permitted without separate explicit review and authorization."
    )


def main():
    parser = argparse.ArgumentParser(
        description="Manage the isolated PostgreSQL test database (nivesh_test)."
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=["status", "preflight", "setup", "cleanup"],
        help="Action to perform: status (read-only diagnostic), preflight (manifest validation), setup, or cleanup.",
    )
    parser.add_argument(
        "--target-db",
        required=True,
        help="Target database name. Must be strictly 'nivesh_test'.",
    )
    parser.add_argument(
        "--host",
        default="localhost",
        help="Database host. Approved values: localhost or 127.0.0.1.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5432,
        help="Database port. Approved value: 5432.",
    )
    parser.add_argument(
        "--user",
        default=None,
        help="PostgreSQL role/user. Defaults to current operating system user.",
    )
    parser.add_argument(
        "--confirm-setup",
        default="",
        help="Explicit confirmation string to perform setup. Must be 'nivesh_test'.",
    )
    parser.add_argument(
        "--confirm-cleanup",
        default="",
        help="Explicit confirmation string to perform cleanup. Must be 'nivesh_test'.",
    )

    args = parser.parse_args()

    # Enforce strict target DB argument
    if args.target_db != APPROVED_TEST_DB:
        print(
            f"ERROR: Refusing to operate on target '{args.target_db}'. Only '{APPROVED_TEST_DB}' is permitted.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        if args.action == "preflight":
            migrations_dir = ROOT / "migrations" / "postgres"
            res = validate_migration_manifest(migrations_dir)
            print(json.dumps({"status": "preflight_passed", "verified_files": len(res), "manifest": res}, indent=2))
        elif args.action == "status":
            res = get_status(args.host, args.port, args.user)
            print(json.dumps(res, indent=2))
        elif args.action == "setup":
            res = setup_test_database(
                args.host, args.port, args.user, args.confirm_setup
            )
            print(json.dumps(res, indent=2))
        elif args.action == "cleanup":
            res = cleanup_test_database(args.host, args.port, args.user, args.confirm_cleanup)
            print(json.dumps(res, indent=2))
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
