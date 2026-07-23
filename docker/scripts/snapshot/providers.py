"""
Snapshot Providers for GKE Fast Pod Snapshotting.

This module provides GKESnapshotProvider for triggering gVisor sandbox checkpoints and clearing model weight caches.
"""

from __future__ import annotations

import errno
import logging
import os
import pathlib
import shutil
from typing import Optional

logger = logging.getLogger("llm-d.snapshot")

GVISOR_CHECKPOINT_PATH = "/proc/gvisor/checkpoint"


class SnapshotError(RuntimeError):
    """Raised when container process snapshotting or cache clearing fails."""
    pass


class GKESnapshotProvider:
    """
    Snapshot provider for GKE Sandboxes using gVisor's procfs control interface.
    Clears cached weight files and triggers container checkpointing by writing to /proc/gvisor/checkpoint.
    """

    def __init__(
        self,
        proc_path: str = GVISOR_CHECKPOINT_PATH,
        cache_dir: Optional[str] = None,
    ) -> None:
        self.proc_path = proc_path
        self.cache_dir = (
            cache_dir
            or os.getenv("HF_HUB_CACHE")
            or os.getenv("HUGGINGFACE_HUB_CACHE")
            or os.path.expanduser("~/.cache/huggingface/hub")
        )

    def clear_cache(self) -> None:
        """Clear out cached weight directories if a cache directory exists."""
        if not self.cache_dir:
            logger.debug("No cache directory specified to clear.")
            return

        target_path = pathlib.Path(os.path.expanduser(self.cache_dir))
        try:
            if target_path.exists():
                if target_path.is_file() or target_path.is_symlink():
                    logger.info("Removing cache file/link: %s", target_path)
                    target_path.unlink()
                else:
                    # Check for Hugging Face model weight directories (models--*)
                    models_dirs = list(target_path.glob("models--*"))
                    if models_dirs:
                        for mdir in models_dirs:
                            logger.info("Clearing model weight directory: %s", mdir)
                            if mdir.is_file() or mdir.is_symlink():
                                mdir.unlink()
                            else:
                                shutil.rmtree(mdir)
                    else:
                        logger.debug("No model weight directories (models--*) found under: %s", target_path)
            else:
                logger.debug("Cache path does not exist: %s", target_path)
        except OSError as e:
            raise SnapshotError(f"Could not delete locally stored weights at {target_path}: {e}") from e

    def trigger(self) -> None:
        """Execute weight cache clearing followed by gVisor checkpoint triggering and barrier sync."""
        self.clear_cache()

        try:
            fd = os.open(self.proc_path, os.O_RDWR)
        except PermissionError as e:
            raise SnapshotError(f"gVisor checkpoint file is not writable: {self.proc_path}") from e
        except OSError as e:
            raise SnapshotError(f"Failed to open gVisor checkpoint file '{self.proc_path}': {e}") from e

        try:
            try:
                os.write(fd, b"1")
            except OSError as e:
                raise SnapshotError(f"Failed to write to gVisor checkpoint file: {e}") from e

            try:
                # Attempting to read 1 byte blocks until checkpoint/restore completes.
                # On success, sentry signals EOF (returning b""), which we discard.
                # On failure, sentry returns an errno, causing os.read to raise OSError.
                res = os.read(fd, 1)
            except OSError as e:
                msg = (
                    f"Snapshot was never initiated: {e}"
                    if e.errno == errno.EINVAL
                    else f"gVisor checkpoint failed: {e}"
                )
                raise SnapshotError(msg) from e

            if res:
                raise SnapshotError(f"gVisor checkpoint returned non-EOF status: {res!r}")

            logger.info("gVisor checkpoint completed successfully")
        finally:
            os.close(fd)
