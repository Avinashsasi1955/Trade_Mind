import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.generate_status import (
    get_git_commit,
    _read_git_commit_from_dir,
    main as generate_status_main,
)


class TestGenerateStatusGitCommit(unittest.TestCase):
    def setUp(self):
        self.orig_env = os.environ.copy()
        for k in (
            "GITHUB_SHA",
            "GIT_COMMIT",
            "NIVESH_CODE_VERSION",
            "CODE_VERSION",
            "GIT_COMMIT_HASH",
            "GIT_SHA",
            "CI_COMMIT_SHA",
        ):
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.orig_env)

    def test_get_git_commit_github_sha_env(self):
        """GitHub Actions sets GITHUB_SHA; get_git_commit must prioritize it."""
        dummy_sha = "a" * 40
        os.environ["GITHUB_SHA"] = dummy_sha
        self.assertEqual(get_git_commit(), dummy_sha)

    def test_get_git_commit_docker_env_vars(self):
        """Standard Docker/CI build arguments must be respected."""
        dummy_sha = "b" * 40
        os.environ["GIT_COMMIT"] = dummy_sha
        self.assertEqual(get_git_commit(), dummy_sha)

        os.environ.pop("GIT_COMMIT")
        os.environ["NIVESH_CODE_VERSION"] = "c" * 40
        self.assertEqual(get_git_commit(), "c" * 40)

    def test_get_git_commit_live_repo(self):
        """With no env overrides in this repo, get_git_commit should return a valid 40-char SHA."""
        commit = get_git_commit()
        self.assertIsInstance(commit, str)
        self.assertEqual(len(commit), 40)
        self.assertTrue(all(c in "0123456789abcdefABCDEF" for c in commit))

    def test_get_git_commit_pure_python_when_git_cli_missing(self):
        """When git CLI raises FileNotFoundError (e.g. no git in PATH), pure Python resolves .git."""
        with patch("shutil.which", return_value=None):
            with patch("subprocess.check_output", side_effect=FileNotFoundError(2, "No such file or directory: 'git'")):
                commit = get_git_commit()
                self.assertEqual(len(commit), 40)
                self.assertTrue(all(c in "0123456789abcdefABCDEF" for c in commit))

    def test_read_git_commit_from_dir_symbolic_ref(self):
        """Test direct .git directory reading with a symbolic branch ref."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            git_dir = Path(tmp_dir) / ".git"
            git_dir.mkdir(parents=True)
            heads_dir = git_dir / "refs" / "heads"
            heads_dir.mkdir(parents=True)

            test_sha = "1234567890abcdef1234567890abcdef12345678"
            (heads_dir / "main").write_text(f"{test_sha}\n", encoding="utf-8")
            (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

            res = _read_git_commit_from_dir(git_dir)
            self.assertEqual(res, test_sha)

    def test_read_git_commit_from_dir_packed_refs(self):
        """Test direct .git directory reading when ref is in packed-refs."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            git_dir = Path(tmp_dir) / ".git"
            git_dir.mkdir(parents=True)

            test_sha = "abcdef1234567890abcdef1234567890abcdef12"
            packed_content = (
                "# pack-refs with: sorted-keys\n"
                f"{test_sha} refs/heads/feature/branch\n"
            )
            (git_dir / "packed-refs").write_text(packed_content, encoding="utf-8")
            (git_dir / "HEAD").write_text("ref: refs/heads/feature/branch\n", encoding="utf-8")

            res = _read_git_commit_from_dir(git_dir)
            self.assertEqual(res, test_sha)

    def test_read_git_commit_from_dir_detached_head(self):
        """Test direct .git directory reading in detached HEAD state."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            git_dir = Path(tmp_dir) / ".git"
            git_dir.mkdir(parents=True)

            test_sha = "9876543210fedcba9876543210fedcba98765432"
            (git_dir / "HEAD").write_text(f"{test_sha}\n", encoding="utf-8")

            res = _read_git_commit_from_dir(git_dir)
            self.assertEqual(res, test_sha)

    def test_read_git_commit_from_worktree_file(self):
        """Test .git as a gitdir reference file (worktrees or submodules)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            real_git_dir = root / "real_git"
            real_git_dir.mkdir(parents=True)

            test_sha = "deadbeef12345678deadbeef12345678deadbeef"
            (real_git_dir / "HEAD").write_text(f"{test_sha}\n", encoding="utf-8")

            git_file = root / ".git"
            git_file.write_text(f"gitdir: {real_git_dir}\n", encoding="utf-8")

            res = _read_git_commit_from_dir(git_file)
            self.assertEqual(res, test_sha)

    def test_get_git_commit_unknown_fallback_clean(self):
        """When everything fails, fallback must be clean 'UNKNOWN' without exception traceback."""
        with tempfile.TemporaryDirectory() as empty_dir:
            with patch("shutil.which", return_value=None):
                with patch("subprocess.check_output", side_effect=Exception("Failed to run git")):
                    commit = get_git_commit(repo_root=Path(empty_dir))
                    self.assertEqual(commit, "UNKNOWN")
                    # Ensure no exception text is embedded
                    self.assertNotIn("Errno", commit)
                    self.assertNotIn("Exception", commit)
                    self.assertNotIn("Failed", commit)

    def test_main_db_unavailable_preserves_status_metrics(self):
        """When the database is unreachable, main() preserves existing sections/tables and updates commit hash."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            status_file = tmp_root / "STATUS.md"
            status_content = (
                "# TradeMind — Living Project Status (STATUS.md)\n\n"
                "*Generated Automatically by `scripts/generate_status.py`*\n"
                "*Generation Timestamp: `2026-10-13 16:06:21 IST`*\n"
                "*Exact Git Commit Hash: `UNKNOWN ([Errno 2] No such file or directory: 'git')`*\n\n"
                "## 2. Machine Learning Ground Truth\n"
                "| Model Version | Closed Trades | Net Realised P&L |\n"
                "| `direction-v3.0` | 32 | ₹3103.45 |\n"
            )
            status_file.write_text(status_content, encoding="utf-8")

            fake_sha = "f" * 40
            with patch("scripts.generate_status.ROOT", tmp_root):
                with patch("scripts.generate_status.get_git_commit", return_value=fake_sha):
                    with patch("scripts.generate_status.create_engine", side_effect=Exception("DB unreachable")):
                        generate_status_main()

            updated = status_file.read_text(encoding="utf-8")
            self.assertIn(f"*Exact Git Commit Hash: `{fake_sha}`*", updated)
            self.assertIn("| `direction-v3.0` | 32 | ₹3103.45 |", updated)
            self.assertNotIn("[Errno 2]", updated)


if __name__ == "__main__":
    unittest.main()
