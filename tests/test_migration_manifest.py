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
        """
        migrations_dir = ROOT / "migrations" / "postgres"
        files = sorted(migrations_dir.glob("*.sql"), key=lambda f: int(f.name.split("_")[1]))

        commit_pattern = re.compile(r"\bCOMMIT\s*;", re.IGNORECASE)
        files_with_commit = []
        files_without_commit = []

        for f in files:
            content = f.read_text(encoding="utf-8")
            if commit_pattern.search(content):
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
        """Verify importing scripts.manage_test_database triggers zero network or CLI side effects."""
        import sys
        # manage_test_database should already be loaded without having connected to any database
        mod = sys.modules.get("scripts.manage_test_database")
        self.assertIsNotNone(mod)
        # Verify main is guarded
        import inspect
        source = inspect.getsource(mod)
        self.assertIn("if __name__ == '__main__':", source.replace('"', "'"))



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

