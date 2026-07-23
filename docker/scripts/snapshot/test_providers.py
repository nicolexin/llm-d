"""
Unit tests for GKE Snapshot Providers, Cache Clearing, and vLLM Lifespan Wrapper.
"""

from __future__ import annotations

import asyncio
import errno
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from docker.scripts.snapshot import (
    GKESnapshotProvider,
    SnapshotError,
    patch_vllm_lifespan,
)

CHECKPOINT_PATH = "/proc/gvisor/checkpoint"
MOCK_FD = 42
NONEXISTENT_PATH = "/nonexistent/path/to/cache"
VALID_PROVIDERS = ("gke", "gke_sandbox", "gvisor")
DISABLED_PROVIDERS = ("none", "disabled", "false", "0")


class TestClearCache(unittest.TestCase):
    def test_clear_cache_with_explicit_path_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            model_dir = os.path.join(tmpdir, "models--org--repo")
            os.makedirs(model_dir)
            file_path = os.path.join(model_dir, "weights.bin")
            with open(file_path, "w") as f:
                f.write("dummy content")

            provider = GKESnapshotProvider(cache_dir=tmpdir)
            self.assertTrue(os.path.exists(model_dir))
            provider.clear_cache()
            self.assertFalse(os.path.exists(model_dir))

    def test_clear_cache_with_explicit_path_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("dummy file")
            file_path = f.name

        try:
            provider = GKESnapshotProvider(cache_dir=file_path)
            self.assertTrue(os.path.exists(file_path))
            provider.clear_cache()
            self.assertFalse(os.path.exists(file_path))
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)

    def test_clear_cache_with_env_var(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            model_dir = os.path.join(tmpdir, "models--org--repo")
            os.makedirs(model_dir)
            with patch.dict(os.environ, {"HF_HUB_CACHE": tmpdir}):
                provider = GKESnapshotProvider()
                self.assertTrue(os.path.exists(model_dir))
                provider.clear_cache()
                self.assertFalse(os.path.exists(model_dir))

    def test_clear_cache_non_existent(self):
        provider = GKESnapshotProvider(cache_dir=NONEXISTENT_PATH)
        # Should not raise exception
        provider.clear_cache()

    def test_clear_cache_selective_models_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            model_dir1 = os.path.join(tmpdir, "models--org--repo1")
            model_dir2 = os.path.join(tmpdir, "models--org--repo2")
            other_dir = os.path.join(tmpdir, "other_data")
            other_file = os.path.join(tmpdir, "version.txt")

            os.makedirs(model_dir1)
            os.makedirs(model_dir2)
            os.makedirs(other_dir)
            with open(other_file, "w") as f:
                f.write("version 1.0")

            provider = GKESnapshotProvider(cache_dir=tmpdir)
            provider.clear_cache()

            # models--* directories should be deleted
            self.assertFalse(os.path.exists(model_dir1))
            self.assertFalse(os.path.exists(model_dir2))
            # Non-models directories and files should be preserved
            self.assertTrue(os.path.exists(other_dir))
            self.assertTrue(os.path.exists(other_file))

    def test_clear_cache_no_models_directories_preserves_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            other_dir = os.path.join(tmpdir, "other_data")
            os.makedirs(other_dir)
            provider = GKESnapshotProvider(cache_dir=tmpdir)
            provider.clear_cache()
            self.assertTrue(os.path.exists(other_dir))
            self.assertTrue(os.path.exists(tmpdir))

    @patch("shutil.rmtree")
    def test_clear_cache_failure_raises_snapshot_error(self, mock_rmtree):
        mock_rmtree.side_effect = PermissionError("Permission denied")
        with tempfile.TemporaryDirectory() as tmpdir:
            model_dir = os.path.join(tmpdir, "models--org--repo")
            os.makedirs(model_dir)
            provider = GKESnapshotProvider(cache_dir=tmpdir)
            with self.assertRaises(SnapshotError) as ctx:
                provider.clear_cache()
            self.assertIn("Could not delete locally stored weights", str(ctx.exception))


class TestGKESnapshotProvider(unittest.TestCase):
    @patch.object(GKESnapshotProvider, "clear_cache")
    @patch("os.open")
    def test_open_checkpoint_file_not_writable(self, mock_open, mock_clear):
        mock_open.side_effect = PermissionError("Permission denied")
        provider = GKESnapshotProvider(proc_path=CHECKPOINT_PATH)
        with self.assertRaises(SnapshotError) as ctx:
            provider.trigger()
        self.assertIn("not writable", str(ctx.exception))

    @patch.object(GKESnapshotProvider, "clear_cache")
    @patch("os.open")
    @patch("os.write")
    @patch("os.read")
    @patch("os.close")
    def test_gke_snapshot_provider_success(
        self, mock_close, mock_read, mock_write, mock_open, mock_clear
    ):
        mock_open.return_value = MOCK_FD
        mock_read.return_value = b""

        provider = GKESnapshotProvider(proc_path=CHECKPOINT_PATH)
        provider.trigger()

        mock_clear.assert_called_once()
        mock_open.assert_called_once_with(CHECKPOINT_PATH, os.O_RDWR)
        mock_write.assert_called_once_with(MOCK_FD, b"1")
        mock_read.assert_called_once_with(MOCK_FD, 1)
        mock_close.assert_called_once_with(MOCK_FD)

    @patch.object(GKESnapshotProvider, "clear_cache")
    @patch("os.open")
    @patch("os.write")
    @patch("os.read")
    @patch("os.close")
    def test_gke_snapshot_provider_einval_failure(
        self, mock_close, mock_read, mock_write, mock_open, mock_clear
    ):
        mock_open.return_value = MOCK_FD
        err = OSError()
        err.errno = errno.EINVAL
        mock_read.side_effect = err

        provider = GKESnapshotProvider()
        with self.assertRaises(SnapshotError) as ctx:
            provider.trigger()
        self.assertIn("Snapshot was never initiated", str(ctx.exception))


class TestGKEVllmWrapper(unittest.TestCase):
    def test_patch_vllm_lifespan(self):
        mock_app = MagicMock()
        mock_router = MagicMock()

        @asynccontextmanager
        async def mock_original_lifespan(app):
            yield {"status": "ok"}

        mock_router.lifespan_context = mock_original_lifespan
        mock_app.router = mock_router

        mock_engine = AsyncMock()
        mock_engine.sleep = AsyncMock()
        mock_engine.wake_up = AsyncMock()
        mock_state = MagicMock(spec=["engine_client"])
        mock_state.engine_client = mock_engine
        mock_app.state = mock_state

        mock_provider = MagicMock(spec=GKESnapshotProvider)

        patched_app = patch_vllm_lifespan(mock_app, snapshot_provider=mock_provider)
        self.assertIsNotNone(patched_app)
        self.assertNotEqual(mock_router.lifespan_context, mock_original_lifespan)

        async def run_test():
            async with mock_router.lifespan_context(mock_app) as state:
                self.assertEqual(state, {"status": "ok"})

        asyncio.run(run_test())

        mock_engine.sleep.assert_called_once_with(level=1)
        mock_provider.trigger.assert_called_once()
        mock_engine.wake_up.assert_called_once()

    def test_patch_vllm_lifespan_disabled_provider(self):
        mock_app = MagicMock()
        mock_router = MagicMock()

        @asynccontextmanager
        async def mock_original_lifespan(app):
            yield {"status": "ok"}

        mock_router.lifespan_context = mock_original_lifespan
        mock_app.router = mock_router
        mock_app.state = MagicMock(engine_client=AsyncMock())
        mock_provider = MagicMock(spec=GKESnapshotProvider)

        with patch.dict(os.environ, {"SNAPSHOT_PROVIDER": "none"}):
            patched_app = patch_vllm_lifespan(mock_app, snapshot_provider=None)
            async def run_test():
                async with mock_router.lifespan_context(mock_app) as state:
                    self.assertEqual(state, {"status": "ok"})
            asyncio.run(run_test())

        mock_provider.trigger.assert_not_called()


if __name__ == "__main__":
    unittest.main()
