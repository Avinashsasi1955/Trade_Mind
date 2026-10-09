"""Unit test to verify that clean clones without security_master.json gracefully fall back and never reject instruments."""
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.security_master import resolve_security, load_security_master


class TestCleanCloneSecurityMaster(unittest.TestCase):
    def test_clean_clone_fallback_resolves_instruments(self):
        # Force load_security_master cache clear
        load_security_master.cache_clear()
        
        # Simulate non-existent security_master.json
        with patch.object(Path, "is_file", return_value=False):
            with patch.object(Path, "write_text"):
                sec = resolve_security("RELIANCE")
                self.assertIsNotNone(sec)
                self.assertEqual(sec["symbol"], "RELIANCE")
                self.assertEqual(sec["exchange"], "NSE")

                infy = resolve_security("INFY")
                self.assertIsNotNone(infy)
                self.assertEqual(infy["symbol"], "INFY")

                tata = resolve_security("TATAMOTORS")
                self.assertIsNotNone(tata)
                self.assertEqual(tata["symbol"], "TATAMOTORS")

        # Clear cache again so normal file loading is restored
        load_security_master.cache_clear()


if __name__ == "__main__":
    unittest.main()
