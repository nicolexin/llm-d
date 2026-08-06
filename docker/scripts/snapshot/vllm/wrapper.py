"""
vLLM Wrapper Entrypoint for GKE Fast Pod Snapshotting (docker/scripts/snapshot/vllm/wrapper.py).

Note: Scope is single-rank deployments (DP/multi-rank barrier coordination is not covered).

Patches vLLM's FastAPI application lifespan context manager to:
1. Run standard engine initialization (loads weights, compiles graphs).
2. Release physical VRAM maps via engine.sleep(level=1).
3. Trigger the snapshot checkpoint (clearing model weights cache on disk).
4. Re-allocate physical VRAM maps via engine.wake_up() upon container restore.
5. Yield control back to uvicorn to bind TCP ports and begin serving traffic.
"""

import ctypes
import os
import pathlib
import socket
import time
from contextlib import asynccontextmanager
from typing import Optional
from vllm.logger import init_logger

from ..providers import GKESnapshotProvider

logger = init_logger("vllm.snapshot.wrapper")


def nccl_checkpoint_prepare():
    """Pre-Checkpoint Hook to quiesce NCCL communicators via the preloaded shim."""
    shim_path = os.getenv("LD_PRELOAD", "/usr/local/lib/libnccl-checkpoint-shim.so")
    if os.path.exists(shim_path):
        try:
            shim = ctypes.CDLL(shim_path)
            if hasattr(shim, "checkpoint_prepare"):
                logger.info("Executing NCCL Checkpoint Shim checkpoint_prepare()...")
                shim.checkpoint_prepare()
        except Exception as e:
            logger.warning("Failed executing NCCL Checkpoint Shim checkpoint_prepare: %s", e)


def execute_nccl_re_rendezvous():
    """Write Rank-0 bootstrap endpoint for NCCL Checkpoint Shim and re-initialize."""
    kvs_path = os.getenv("NCCL_CHECKPOINT_KVS_PATH", "/var/run/nccl/kvs.txt")
    host_ip = os.getenv("VLLM_HOST_IP", "127.0.0.1")
    bootstrap_kvs = f"{host_ip}:6379"

    # Write KVS endpoint file for all 8 GPU ranks inside the pod
    kvs_file = pathlib.Path(kvs_path)
    kvs_file.parent.mkdir(parents=True, exist_ok=True)
    kvs_file.write_text(bootstrap_kvs)
    logger.info("Wrote Rank-0 NCCL bootstrap endpoint '%s' to %s", bootstrap_kvs, kvs_path)

    # Re-initialize NCCL via the preloaded shim
    shim_path = os.getenv("LD_PRELOAD", "/usr/local/lib/libnccl-checkpoint-shim.so")
    if os.path.exists(shim_path):
        try:
            shim = ctypes.CDLL(shim_path)
            if hasattr(shim, "checkpoint_restore"):
                logger.info("Executing NCCL Checkpoint Shim checkpoint_restore()...")
                shim.checkpoint_restore()
        except Exception as e:
            logger.warning("Failed executing NCCL Checkpoint Shim checkpoint_restore: %s", e)


def patch_vllm_lifespan(app, snapshot_provider: Optional[GKESnapshotProvider] = None):
    """
    Patches the FastAPI app lifespan context manager for vLLM snapshotting.

    Args:
        app: The vLLM FastAPI application instance.
        snapshot_provider: Snapshot provider instance. Defaults to GKESnapshotProvider()
                           when SNAPSHOT_PROVIDER is explicitly set to 'gke' or 'gvisor'.
    """
    if snapshot_provider is None:
        provider_type = os.getenv("SNAPSHOT_PROVIDER", "").lower()
        if provider_type in ("gke", "gvisor", "gke_gvisor"):
            snapshot_provider = GKESnapshotProvider()
        else:
            logger.info(
                "Snapshot provider disabled (SNAPSHOT_PROVIDER='%s'). Lifespan context unmodified.",
                provider_type,
            )
            return app

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def patched_lifespan(app_inst):
        async with original_lifespan(app_inst) as state:
            engine = getattr(app_inst.state, "engine_client", None)

            # Step 1: Release physical VRAM maps (keeps virtual memory addresses stable)
            if engine and hasattr(engine, "sleep"):
                logger.info("Executing engine.sleep(level=1) to release physical VRAM...")
                try:
                    await engine.sleep(level=1)
                except Exception as e:
                    logger.error("Failed to execute engine.sleep(level=1): %s", e)
            else:
                logger.error("Engine client is missing or does not support sleep(level=1).")

            # Step 2: Quiesce NCCL communicators (Pre-Checkpoint Hook)
            nccl_checkpoint_prepare()

            # Step 3: Trigger Checkpoint
            logger.info("Triggering snapshot checkpoint...")
            try:
                snapshot_provider.trigger()
            except Exception as e:
                logger.error("Failed to trigger snapshot checkpoint: %s", e)

            # --- PROCESS RESTORED ---
            logger.info("Process restored from snapshot checkpoint. Resuming engine...")

            # Step 4: Re-Rendezvous IP & Re-initialize NCCL (Post-Restore Hook)
            execute_nccl_re_rendezvous()

            # Step 5: Re-allocate physical VRAM maps
            if engine and hasattr(engine, "wake_up"):
                logger.info("Executing engine.wake_up() to restore VRAM...")
                try:
                    await engine.wake_up()
                except Exception as e:
                    logger.error("Failed to execute engine.wake_up(): %s", e)
            else:
                logger.error("Engine client is missing or does not support wake_up().")

            yield state

    app.router.lifespan_context = patched_lifespan
    logger.info("Successfully patched vLLM FastAPI lifespan context for GKE snapshotting with NCCL Checkpoint Shim.")
    return app





