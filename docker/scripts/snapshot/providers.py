"""
Snapshot Providers for GKE Fast Pod Snapshotting.

This module provides GKESnapshotProvider for triggering gVisor sandbox checkpoints
and clearing model weight caches when configured.
"""

from __future__ import annotations

import errno
import logging
import os
import pathlib
import shutil
from vllm.logger import init_logger

logger = init_logger("vllm.snapshot.providers")

GVISOR_CHECKPOINT_PATH = "/proc/gvisor/checkpoint"


class SnapshotError(RuntimeError):
    """Raised when container process snapshotting or cache clearing fails."""
    pass


class GKESnapshotProvider:
    """
    Snapshot provider for GKE Sandboxes using gVisor's procfs control interface.
    Clears cached weight files if a cache directory is explicitly specified,
    and triggers container checkpointing by writing to /proc/gvisor/checkpoint.
    """

    def __init__(
        self,
        proc_path: str = GVISOR_CHECKPOINT_PATH,
        cache_dir: Optional[str] = None,
    ) -> None:
        self.proc_path = proc_path
        self.cache_dir = cache_dir or os.getenv("SNAPSHOT_CLEAR_CACHE_DIR")

    def is_available(self) -> bool:
        """Check if the gVisor procfs checkpoint interface is available and writable."""
        return os.path.exists(self.proc_path) and os.access(self.proc_path, os.W_OK)

    def clear_cache(self) -> None:
        """Clear out cached model weights if an explicit cache directory is configured."""
        if not self.cache_dir:
            logger.debug("No cache directory specified to clear (SNAPSHOT_CLEAR_CACHE_DIR unset).")
            return

        safetensors_strategy = os.getenv("VLLM_SAFETENSORS_LOAD_STRATEGY", "").lower()
        if safetensors_strategy != "eager":
            logger.warning(
                "Cache clearing is enabled, but VLLM_SAFETENSORS_LOAD_STRATEGY is '%s' (expected 'eager'). "
                "Non-eager loading may cause vLLM to hold file descriptors to model weight files.",
                safetensors_strategy or "unset",
            )

        target_path = pathlib.Path(os.path.expanduser(self.cache_dir))
        try:
            if target_path.exists():
                logger.info("Clearing specified weight cache path contents: %s", target_path)
                if target_path.is_file() or target_path.is_symlink():
                    target_path.unlink()
                else:
                    for child in target_path.iterdir():
                        if child.is_file() or child.is_symlink():
                            child.unlink()
                        else:
                            shutil.rmtree(child)
            else:
                logger.debug("Cache path does not exist: %s", target_path)
        except OSError as e:
            logger.error("Failed to clear weight cache at %s: %s", target_path, e)

    def trigger(self) -> None:
        """Execute weight cache clearing followed by gVisor checkpoint triggering."""
        if not self.is_available():
            logger.warning(
                "gVisor checkpoint interface '%s' is not available or writable. Skipping snapshot checkpoint.",
                self.proc_path,
            )
            return

        self.clear_cache()

        try:
            fd = os.open(self.proc_path, os.O_RDWR)
        except OSError as e:
            logger.error("Failed to open gVisor checkpoint file '%s': %s", self.proc_path, e)
            return

        try:
            try:
                os.write(fd, b"1")
            except OSError as e:
                logger.error("Failed to write to gVisor checkpoint file: %s", e)
                return

            try:
                # Attempting to read 1 byte blocks until checkpoint/restore completes.
                res = os.read(fd, 1)
            except OSError as e:
                if e.errno == errno.EINVAL:
                    logger.error("gVisor snapshot was never initiated: %s", e)
                else:
                    logger.error("gVisor checkpoint failed during restore wait: %s", e)
                return

            if res and res not in (b"", b"r", b"e"):
                logger.warning("gVisor checkpoint returned unexpected status: %r", res)

            logger.info("gVisor checkpoint completed successfully")
        finally:
            os.close(fd)

