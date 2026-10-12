"""Static unit tests for PostgreSQL migration manifest, ledger reconciliation, and URL validation.

Verifies:
1. All 19 migration files exist and match their SHA-256 checksums in the manifest.
2. Migration sequence numbering is strictly contiguous (0 to 18).
3. The schema_migrations ledger expectations match the actual SQL INSERT statements.
4. validate_postgres_test_url strictly fails closed against query delimiters, fragments,
   trailing slashes, extra path components, wrong databases, malformed ports, and never leaks credentials.
"""
import hashlib
import re
import unittest
from pathlib import Path

from scripts.manage_test_database import (
    APPROVED_TEST_DB,
    EXPECTED_LEDGER_ENTRIES,
    EXPECTED_MIGRATION_MANIFEST,
    validate_migration_manifest,
    validate_postgres_test_url,
)

ROOT = Path(__file__).resolve().parent.parent


class TestMigrationManifestAndLedger(unittest.TestCase):
    def test_manifest_file_presence_and_checksums(self):
        """Verify all 19 migrations exist and have matching SHA-256 hashes."""
        migrations_dir = ROOT / "migrations" / "postgres"
        verified = validate_migration_manifest(migrations_dir)
        self.assertEqual(len(verified), 19)
        self.assertEqual(len(EXPECTED_MIGRATION_MANIFEST), 19)

    def test_migration_sequence_ordering(self):
        """Verify migration numbering is contiguous from 0 to 18 without gaps."""
        names = list(EXPECTED_MIGRATION_MANIFEST.keys())
        indices = [int(n.split("_")[1]) for n in names]
        self.assertEqual(indices, list(range(19)))

    def test_schema_migrations_ledger_reconciliation(self):
        """Reconcile the 17 ledger entries against actual SQL INSERT statements across all 19 files.

        Verifies that:
        - v3_0 (base schema before ledger) and v3_17 (schema-only patch) have no ledger insert.
        - Exactly 17 migrations insert into schema_migrations.
        - The extracted sequence matches EXPECTED_LEDGER_ENTRIES identically.
        - There are no duplicate entries.
        """
        migrations_dir = ROOT / "migrations" / "postgres"
        files = sorted(migrations_dir.glob("*.sql"), key=lambda f: int(f.name.split("_")[1]))
        self.assertEqual(len(files), 19)

        extracted_ledger = []
        files_with_inserts = []
        files_without_inserts = []

        pattern = re.compile(
            r"INSERT\s+INTO\s+schema_migrations\s*\(version\)\s*VALUES\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
            re.IGNORECASE,
        )

        for f in files:
            content = f.read_text(encoding="utf-8")
            matches = pattern.findall(content)
            if matches:
                extracted_ledger.extend(matches)
                files_with_inserts.append(f.name)
            else:
                files_without_inserts.append(f.name)

        # Explicitly verify the two files without ledger inserts
        self.assertEqual(files_without_inserts, ["v3_0_production.sql", "v3_17_dual_mode_and_agentic_ledger.sql"])

        # Exactly 17 ledger inserts
        self.assertEqual(len(extracted_ledger), 17)
        self.assertEqual(len(extracted_ledger), len(set(extracted_ledger)), "Ledger entries must be unique")

        # Verify against EXPECTED_LEDGER_ENTRIES
        self.assertEqual(extracted_ledger, EXPECTED_LEDGER_ENTRIES)

    def test_transaction_boundary_distribution(self):
        """Verify transaction boundary claims across all 19 migrations.

        - v3_0 through v3_16 contain explicit COMMIT; statements.
        - v3_17 and v3_18 do NOT contain internal COMMIT; statements and rely on runner transaction management.
        SQL comments (-- and /* */) are stripped before regex inspection to avoid comment false positives.
        """
        migrations_dir = ROOT / "migrations" / "postgres"
        files = sorted(migrations_dir.glob("*.sql"), key=lambda f: int(f.name.split("_")[1]))

        def strip_sql_tokens(sql_text: str) -> str:
            # 1. Strip block comments /* ... */
            s = re.sub(r"/\*.*?\*/", " ", sql_text, flags=re.DOTALL)
            # 2. Strip line comments -- ...
            s = re.sub(r"--[^\n]*", " ", s)
            # 3. Strip dollar-quoted function bodies / strings $tag$...$tag$
            s = re.sub(r"\$(\w*)\$.*?\$\1\$", " ", s, flags=re.DOTALL)
            # 4. Strip single-quoted string literals '...' (preserving escaped quotes '')
            s = re.sub(r"'(''|[^'])*'", " ", s)
            return s

        commit_pattern = re.compile(r"\bCOMMIT\s*;", re.IGNORECASE)
        files_with_commit = []
        files_without_commit = []

        for f in files:
            raw_content = f.read_text(encoding="utf-8")
            clean_content = strip_sql_tokens(raw_content)
            if commit_pattern.search(clean_content):
                files_with_commit.append(f.name)
            else:
                files_without_commit.append(f.name)

        # Files v3_0 through v3_16 (17 files) contain internal COMMIT;
        expected_with_commit = [f"v3_{i}_" for i in range(17)]
        self.assertEqual(len(files_with_commit), 17)
        for prefix in expected_with_commit:
            self.assertTrue(
                any(name.startswith(prefix) for name in files_with_commit),
                f"Expected {prefix} to contain internal COMMIT;",
            )

        # Files v3_17 and v3_18 do NOT contain internal COMMIT;
        self.assertEqual(
            files_without_commit,
            ["v3_17_dual_mode_and_agentic_ledger.sql", "v3_18_shadow_session_code_version.sql"],
        )

    def test_manage_test_database_import_is_side_effect_free(self):
        """Verify importing scripts.manage_test_database in an isolated subprocess executes zero connections or CLI operations."""
        import subprocess
        import sys

        # Script run in an isolated subprocess: monkeypatches psycopg2.connect to fail immediately if called,
        # then imports scripts.manage_test_database and checks that sys.exit was not called.
        code = (
            "import sys, psycopg2\n"
            "def forbidden_connect(*args, **kwargs):\n"
            "    raise RuntimeError('FORBIDDEN: manage_test_database made a DB connection during import!')\n"
            "psycopg2.connect = forbidden_connect\n"
            "import scripts.manage_test_database as mtd\n"
            "# Assert module loaded and constants are accessible\n"
            "assert mtd.APPROVED_TEST_DB == 'nivesh_test'\n"
            "print('IMPORT_SUCCESS_NO_SIDE_EFFECTS')\n"
        )

        res = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )

        self.assertEqual(res.returncode, 0, f"Subprocess failed with stderr: {res.stderr}")
        self.assertIn("IMPORT_SUCCESS_NO_SIDE_EFFECTS", res.stdout)



class TestPostgresUrlValidation(unittest.TestCase):
    def test_valid_test_url_accepted(self):
        """Valid local test URLs must be accepted and returned."""
        valid_urls = [
            "postgresql://localhost:5432/nivesh_test",
            "postgresql://127.0.0.1:5432/nivesh_test",
            "postgresql://user:pass@localhost:5432/nivesh_test",
            "postgresql+psycopg2://localhost:5432/nivesh_test",
        ]
        for url in valid_urls:
            res = validate_postgres_test_url(url)
            self.assertEqual(res, url)

    def test_empty_or_whitespace_url_fails_closed(self):
        """Empty or whitespace strings must raise RuntimeError."""
        for empty in ["", "   ", None]:
            with self.assertRaisesRegex(RuntimeError, "FAIL-CLOSED"):
                validate_postgres_test_url(empty)

    def test_query_delimiters_rejected_without_credential_leak(self):
        """Trailing or populated query parameters must fail closed without exposing credentials."""
        secret = "super_secret_password_123"
        urls_with_query = [
            f"postgresql://user:{secret}@localhost:5432/nivesh_test?",
            f"postgresql://user:{secret}@localhost:5432/nivesh_test?sslmode=disable",
            f"postgresql://user:{secret}@localhost:5432/nivesh_test?options=-c%20search_path=public",
        ]
        for url in urls_with_query:
            with self.assertRaises(RuntimeError) as ctx:
                validate_postgres_test_url(url)
            self.assertIn("Query", str(ctx.exception))
            self.assertNotIn(secret, str(ctx.exception), "Credentials must never be leaked in error message")

    def test_fragment_delimiters_rejected_without_credential_leak(self):
        """Trailing or populated fragments must fail closed without exposing credentials."""
        secret = "super_secret_password_456"
        urls_with_frag = [
            f"postgresql://user:{secret}@localhost:5432/nivesh_test#",
            f"postgresql://user:{secret}@localhost:5432/nivesh_test#fragment",
        ]
        for url in urls_with_frag:
            with self.assertRaises(RuntimeError) as ctx:
                validate_postgres_test_url(url)
            self.assertIn("fragment", str(ctx.exception).lower())
            self.assertNotIn(secret, str(ctx.exception), "Credentials must never be leaked in error message")

    def test_path_structure_must_be_exact(self):
        """Trailing slashes and extra path components must fail closed."""
        invalid_paths = [
            "postgresql://localhost:5432/nivesh_test/",
            "postgresql://localhost:5432/nivesh_test/extra",
            "postgresql://localhost:5432/nivesh_test/sub/dir",
            "postgresql://localhost:5432/",
            "postgresql://localhost:5432",
        ]
        for url in invalid_paths:
            with self.assertRaises(RuntimeError) as ctx:
                validate_postgres_test_url(url)
            self.assertIn("Path must be exactly", str(ctx.exception))

    def test_forbidden_and_incorrect_database_names_rejected(self):
        """Staging, production, maintenance, and unknown database targets must fail closed."""
        forbidden = [
            "postgresql://localhost:5432/nivesh_v3_staging",
            "postgresql://localhost:5432/nivesh",
            "postgresql://localhost:5432/postgres",
            "postgresql://localhost:5432/template0",
            "postgresql://localhost:5432/template1",
            "postgresql://localhost:5432/other_db",
        ]
        for url in forbidden:
            with self.assertRaises(RuntimeError):
                validate_postgres_test_url(url)

    def test_disallowed_and_malformed_ports_rejected(self):
        """Ports other than 5432 or non-integer ports must fail closed without leaking credentials."""
        secret = "secret_pass_789"
        bad_port_urls = [
            f"postgresql://user:{secret}@localhost:5433/nivesh_test",
            f"postgresql://user:{secret}@localhost:5434/nivesh_test",
            f"postgresql://user:{secret}@localhost:abc/nivesh_test",
            f"postgresql://user:{secret}@localhost:999999/nivesh_test",
        ]
        for url in bad_port_urls:
            with self.assertRaises(RuntimeError) as ctx:
                validate_postgres_test_url(url)
            self.assertIn("port", str(ctx.exception).lower())
            self.assertNotIn(secret, str(ctx.exception), "Credentials must never be leaked in error message")

    def test_disallowed_hosts_rejected(self):
        """Remote hosts or unapproved IPs must fail closed."""
        remote_urls = [
            "postgresql://remote.server.internal:5432/nivesh_test",
            "postgresql://192.168.1.50:5432/nivesh_test",
            "postgresql://10.0.0.1:5432/nivesh_test",
        ]
        for url in remote_urls:
            with self.assertRaises(RuntimeError) as ctx:
                validate_postgres_test_url(url)
            self.assertIn("Disallowed host", str(ctx.exception))

    def test_run_postgres_flag_requires_strictly_one(self):
        """Verify RUN_POSTGRES must equal strictly '1' to activate postgres tests."""
        import os
        from unittest.mock import patch
        from tests.test_postgres_runtime import _should_run_postgres_tests

        invalid_flags = ["true", "TRUE", "yes", "0", "", "2", "True", "on", None]
        for flag in invalid_flags:
            with patch.dict(os.environ, {}, clear=False):
                if flag is None:
                    os.environ.pop("RUN_POSTGRES", None)
                else:
                    os.environ["RUN_POSTGRES"] = flag
                self.assertFalse(
                    _should_run_postgres_tests(),
                    f"Flag value {flag!r} should NOT activate postgres tests",
                )


class TestDatabaseSafetyAndReadiness(unittest.TestCase):
    def test_unexpected_database_objects_cause_readiness_failure(self):
        """Verify get_status marks database as unclean/not ready for each unexpected object category."""
        from unittest.mock import MagicMock, patch
        from scripts.manage_test_database import (
            get_status,
            EXPECTED_LEDGER_ENTRIES,
            EXPECTED_SCHEMA_TABLES,
            EXPECTED_POSTGRES_EXTENSIONS,
            EXPECTED_CUSTOM_TYPES,
            EXPECTED_USER_FUNCTIONS,
            EXPECTED_SEQUENCES,
        )

        mock_conn = MagicMock()
        mock_meta = {
            "database": "nivesh_test",
            "user": "test_user",
            "server_addr": "127.0.0.1",
            "server_port": 5432,
            "in_recovery": False,
            "pg_version": "PostgreSQL 17.0",
        }

        base_inventory = {
            "is_pristine": False,
            "applied_migrations": list(EXPECTED_LEDGER_ENTRIES),
            "tables": set(EXPECTED_SCHEMA_TABLES),
            "views": set(),
            "matviews": set(),
            "functions": list(EXPECTED_USER_FUNCTIONS),
            "foreign_tables": set(),
            "custom_types": {t: "e" for t in EXPECTED_CUSTOM_TYPES},
            "extensions": {e: "1.0" for e in EXPECTED_POSTGRES_EXTENSIONS},
            "sequences": list(EXPECTED_SEQUENCES),
            "schemas": ["public"],
            "fixtures": {"instruments": 1, "bars": 5, "active_models": 1, "coverage_bars": 5},
        }

        # Sub-case 1: Extra unexpected table
        inv_table = dict(base_inventory, tables=set(EXPECTED_SCHEMA_TABLES) | {"unexpected_rogue_table"})
        with patch("scripts.manage_test_database.validate_migration_manifest", return_value=[]), \
             patch("scripts.manage_test_database.connect_maintenance", return_value=mock_conn), \
             patch("scripts.manage_test_database.verify_connection_metadata", return_value=mock_meta), \
             patch("scripts.manage_test_database.connect_test_db", return_value=mock_conn), \
             patch("scripts.manage_test_database.inspect_database_objects", return_value=inv_table):
            with patch.object(mock_conn, "cursor") as mock_cur_factory:
                mock_cur = MagicMock()
                mock_cur.fetchone.return_value = (1,)
                mock_cur_factory.return_value.__enter__.return_value = mock_cur
                status = get_status("localhost", 5432)
                self.assertFalse(status["is_ready_for_tests"])
                self.assertTrue(any("unexpected_rogue_table" in err for err in status["ledger_errors"]))

        # Sub-case 2: Extra unexpected function
        inv_func = dict(base_inventory, functions=list(EXPECTED_USER_FUNCTIONS) + ["public.malicious_func"])
        with patch("scripts.manage_test_database.validate_migration_manifest", return_value=[]), \
             patch("scripts.manage_test_database.connect_maintenance", return_value=mock_conn), \
             patch("scripts.manage_test_database.verify_connection_metadata", return_value=mock_meta), \
             patch("scripts.manage_test_database.connect_test_db", return_value=mock_conn), \
             patch("scripts.manage_test_database.inspect_database_objects", return_value=inv_func):
            with patch.object(mock_conn, "cursor") as mock_cur_factory:
                mock_cur = MagicMock()
                mock_cur.fetchone.return_value = (1,)
                mock_cur_factory.return_value.__enter__.return_value = mock_cur
                status = get_status("localhost", 5432)
                self.assertFalse(status["is_ready_for_tests"])
                self.assertTrue(any("public.malicious_func" in err for err in status["ledger_errors"]))

        # Sub-case 3: Extra unexpected extension
        inv_ext = dict(base_inventory, extensions=dict(base_inventory["extensions"], pg_trgm="1.0"))
        with patch("scripts.manage_test_database.validate_migration_manifest", return_value=[]), \
             patch("scripts.manage_test_database.connect_maintenance", return_value=mock_conn), \
             patch("scripts.manage_test_database.verify_connection_metadata", return_value=mock_meta), \
             patch("scripts.manage_test_database.connect_test_db", return_value=mock_conn), \
             patch("scripts.manage_test_database.inspect_database_objects", return_value=inv_ext):
            with patch.object(mock_conn, "cursor") as mock_cur_factory:
                mock_cur = MagicMock()
                mock_cur.fetchone.return_value = (1,)
                mock_cur_factory.return_value.__enter__.return_value = mock_cur
                status = get_status("localhost", 5432)
                self.assertFalse(status["is_ready_for_tests"])
                self.assertTrue(any("pg_trgm" in err for err in status["ledger_errors"]))

        # Sub-case 4: Extra unexpected sequence
        inv_seq = dict(base_inventory, sequences=list(EXPECTED_SEQUENCES) + ["rogue_sequence_seq"])
        with patch("scripts.manage_test_database.validate_migration_manifest", return_value=[]), \
             patch("scripts.manage_test_database.connect_maintenance", return_value=mock_conn), \
             patch("scripts.manage_test_database.verify_connection_metadata", return_value=mock_meta), \
             patch("scripts.manage_test_database.connect_test_db", return_value=mock_conn), \
             patch("scripts.manage_test_database.inspect_database_objects", return_value=inv_seq):
            with patch.object(mock_conn, "cursor") as mock_cur_factory:
                mock_cur = MagicMock()
                mock_cur.fetchone.return_value = (1,)
                mock_cur_factory.return_value.__enter__.return_value = mock_cur
                status = get_status("localhost", 5432)
                self.assertFalse(status["is_ready_for_tests"])
                self.assertTrue(any("rogue_sequence_seq" in err for err in status["ledger_errors"]))

        # Sub-case 5: Extra unexpected custom type
        inv_type = dict(base_inventory, custom_types=dict(base_inventory["custom_types"], extra_enum="e"))
        with patch("scripts.manage_test_database.validate_migration_manifest", return_value=[]), \
             patch("scripts.manage_test_database.connect_maintenance", return_value=mock_conn), \
             patch("scripts.manage_test_database.verify_connection_metadata", return_value=mock_meta), \
             patch("scripts.manage_test_database.connect_test_db", return_value=mock_conn), \
             patch("scripts.manage_test_database.inspect_database_objects", return_value=inv_type):
            with patch.object(mock_conn, "cursor") as mock_cur_factory:
                mock_cur = MagicMock()
                mock_cur.fetchone.return_value = (1,)
                mock_cur_factory.return_value.__enter__.return_value = mock_cur
                status = get_status("localhost", 5432)
                self.assertFalse(status["is_ready_for_tests"])
                self.assertTrue(any("extra_enum" in err for err in status["ledger_errors"]))

        # Sub-case 6: Extra unexpected schema
        inv_schema = dict(base_inventory, schemas=["public", "staging_schema"])
        with patch("scripts.manage_test_database.validate_migration_manifest", return_value=[]), \
             patch("scripts.manage_test_database.connect_maintenance", return_value=mock_conn), \
             patch("scripts.manage_test_database.verify_connection_metadata", return_value=mock_meta), \
             patch("scripts.manage_test_database.connect_test_db", return_value=mock_conn), \
             patch("scripts.manage_test_database.inspect_database_objects", return_value=inv_schema):
            with patch.object(mock_conn, "cursor") as mock_cur_factory:
                mock_cur = MagicMock()
                mock_cur.fetchone.return_value = (1,)
                mock_cur_factory.return_value.__enter__.return_value = mock_cur
                status = get_status("localhost", 5432)
                self.assertFalse(status["is_ready_for_tests"])
                self.assertTrue(any("staging_schema" in err for err in status["ledger_errors"]))

    def test_failed_initialization_at_precheck_restores_all_state(self):
        """Verify PostgresRuntimeTests.setUpClass failure at precheck restores environment and module state."""
        import os
        from unittest.mock import MagicMock, patch
        from tests.test_postgres_runtime import PostgresRuntimeTests
        import backend.config as cfg
        import backend.postgres_repositories as pr
        import backend.history_store as hs
        import backend.ml_pipeline as ml

        orig_db_url = os.environ.get("DATABASE_URL")
        orig_env = os.environ.get("NIVESH_ENV")
        orig_cfg_url = getattr(cfg, "DATABASE_URL", None)
        orig_pr_url = getattr(pr, "DATABASE_URL", None)
        orig_hs_url = getattr(hs, "DATABASE_URL", None)
        orig_ml_url = getattr(ml, "DATABASE_URL", None)

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = ("nivesh_v3_staging", False)  # Disallowed DB
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        mock_pool = MagicMock()

        test_env = {
            "RUN_POSTGRES": "1",
            "POSTGRES_TEST_DATABASE_URL": "postgresql://localhost:5432/nivesh_test",
        }
        import backend.postgres_compat as pc
        orig_actual_pool = pc._pool
        pc._pool = mock_pool

        try:
            with patch.dict(os.environ, test_env, clear=False), \
                 patch("psycopg2.connect", return_value=mock_conn):

                with self.assertRaisesRegex(RuntimeError, "SAFETY GATE TRIGGERED"):
                    PostgresRuntimeTests.setUpClass()

                mock_conn.close.assert_called()
                # Verify exact restoration of every saved environment variable and module attribute
                self.assertEqual(os.environ.get("DATABASE_URL"), orig_db_url)
                self.assertEqual(os.environ.get("NIVESH_ENV"), orig_env)
                self.assertEqual(getattr(cfg, "DATABASE_URL", None), orig_cfg_url)
                self.assertEqual(getattr(pr, "DATABASE_URL", None), orig_pr_url)
                self.assertEqual(getattr(hs, "DATABASE_URL", None), orig_hs_url)
                self.assertEqual(getattr(ml, "DATABASE_URL", None), orig_ml_url)
                self.assertIs(pc._pool, mock_pool, "Original pool reference must be restored")
                self.assertEqual(len(PostgresRuntimeTests._original_env), 0)
                self.assertEqual(len(PostgresRuntimeTests._original_modules), 0)
                self.assertIsNone(PostgresRuntimeTests._original_pool)
        finally:
            pc._pool = orig_actual_pool

    def test_failed_initialization_after_config_mutation_restores_all_state(self):
        """Verify PostgresRuntimeTests.setUpClass failure after config mutation restores environment, modules, and pool."""
        import os
        import sys
        from unittest.mock import MagicMock, patch
        from tests.test_postgres_runtime import PostgresRuntimeTests
        import backend.config as cfg
        import backend.postgres_repositories as pr
        import backend.history_store as hs
        import backend.ml_pipeline as ml

        orig_db_url = os.environ.get("DATABASE_URL")
        orig_env = os.environ.get("NIVESH_ENV")
        orig_live = os.environ.get("NIVESH_LIVE_TRADING_ENABLED")
        orig_live_elig = os.environ.get("LIVE_ELIGIBLE")
        orig_cfg_url = getattr(cfg, "DATABASE_URL", None)
        orig_pr_url = getattr(pr, "DATABASE_URL", None)
        orig_hs_url = getattr(hs, "DATABASE_URL", None)
        orig_ml_url = getattr(ml, "DATABASE_URL", None)

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = ("nivesh_test", False)  # Valid precheck
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        mock_pool = MagicMock()
        import backend.postgres_compat as pc
        orig_actual_pool = pc._pool
        pc._pool = mock_pool

        test_env = {
            "RUN_POSTGRES": "1",
            "POSTGRES_TEST_DATABASE_URL": "postgresql://localhost:5432/nivesh_test",
        }

        class FailingModule:
            def __init__(self):
                self.should_fail = True
            def __setattr__(self, name, val):
                if name == "DATABASE_URL" and getattr(self, "should_fail", False):
                    self.should_fail = False
                    raise RuntimeError("SIMULATED_FAILURE_AFTER_MUTATION")
                super().__setattr__(name, val)

        failing_ml = FailingModule()

        try:
            with patch.dict(os.environ, test_env, clear=False), \
                 patch.object(sys.modules["backend"], "ml_pipeline", failing_ml), \
                 patch("psycopg2.connect", return_value=mock_conn):

                with self.assertRaisesRegex(RuntimeError, "SIMULATED_FAILURE_AFTER_MUTATION"):
                    PostgresRuntimeTests.setUpClass()

                mock_conn.close.assert_called()
                # Verify exact restoration of every saved environment variable and module attribute
                self.assertEqual(os.environ.get("DATABASE_URL"), orig_db_url)
                self.assertEqual(os.environ.get("NIVESH_ENV"), orig_env)
                self.assertEqual(os.environ.get("NIVESH_LIVE_TRADING_ENABLED"), orig_live)
                self.assertEqual(os.environ.get("LIVE_ELIGIBLE"), orig_live_elig)
                self.assertEqual(getattr(cfg, "DATABASE_URL", None), orig_cfg_url)
                self.assertEqual(getattr(pr, "DATABASE_URL", None), orig_pr_url)
                self.assertEqual(getattr(hs, "DATABASE_URL", None), orig_hs_url)
                self.assertEqual(getattr(ml, "DATABASE_URL", None), orig_ml_url)
                self.assertIs(pc._pool, mock_pool, "Original pool reference must be restored")
                self.assertEqual(len(PostgresRuntimeTests._original_env), 0)
                self.assertEqual(len(PostgresRuntimeTests._original_modules), 0)
                self.assertIsNone(PostgresRuntimeTests._original_pool)
        finally:
            pc._pool = orig_actual_pool


    def test_pool_cleanup_when_closeall_raises(self):
        """Verify tearDownClass guarantees pool reference is restored/cleared even if closeall() raises."""
        import backend.postgres_compat as pc
        from unittest.mock import MagicMock
        from tests.test_postgres_runtime import PostgresRuntimeTests

        bad_pool = MagicMock()
        bad_pool.closeall.side_effect = RuntimeError("CORRUPTED_POOL_CLOSEALL_FAILED")

        orig_pool = MagicMock()
        pc._pool = bad_pool
        PostgresRuntimeTests._original_pool = orig_pool

        # Run tearDownClass: must not raise and must restore pc._pool to orig_pool
        PostgresRuntimeTests.tearDownClass()

        self.assertIs(pc._pool, orig_pool, "Pool reference must be restored even if closeall raises")
        self.assertIsNone(PostgresRuntimeTests._original_pool)

    def test_verify_connection_metadata_closes_connection_on_failure(self):
        """Verify verify_connection_metadata closes the connection if server metadata check fails."""
        from unittest.mock import MagicMock
        from scripts.manage_test_database import verify_connection_metadata

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = ("wrong_db", "user", "127.0.0.1", 5432, False, "PostgreSQL 17")
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        with self.assertRaisesRegex(RuntimeError, "SAFETY VIOLATION"):
            verify_connection_metadata(mock_conn, "nivesh_test")

        mock_conn.close.assert_called()
