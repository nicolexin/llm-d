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

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from ..providers import GKESnapshotProvider

logger = logging.getLogger("llm-d.snapshot.wrapper")


def patch_vllm_lifespan(app, snapshot_provider: Optional[GKESnapshotProvider] = None):
    """
    Patches the FastAPI app lifespan context manager for vLLM snapshotting (single-rank scope).

    Args:
        app: The vLLM FastAPI application instance.
        snapshot_provider: Snapshot provider instance. Defaults to GKESnapshotProvider().
    """
    if snapshot_provider is None:
        if os.getenv("SNAPSHOT_PROVIDER", "").lower() in ("none", "disabled", "false", "0"):
            snapshot_provider = None
        else:
            snapshot_provider = GKESnapshotProvider()

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def patched_lifespan(app_inst):
        async with original_lifespan(app_inst) as state:
            engine = getattr(app_inst.state, "engine_client", None)

            # Release physical VRAM maps (keeps virtual memory addresses stable)
            if engine and hasattr(engine, "sleep"):
                logger.info("Executing engine.sleep(level=1) to release physical VRAM...")
                await engine.sleep(level=1)

            # Trigger Checkpoint (if provider is enabled)
            if snapshot_provider is not None:
                logger.info("Triggering snapshot checkpoint...")
                snapshot_provider.trigger()

            # --- PROCESS RESTORED ---
            logger.info("Process restored from snapshot checkpoint. Resuming engine...")

            # Re-allocate physical VRAM maps
            if engine and hasattr(engine, "wake_up"):
                logger.info("Executing engine.wake_up() to restore VRAM...")
                await engine.wake_up()

            yield state

    app.router.lifespan_context = patched_lifespan
    logger.info("Successfully patched vLLM FastAPI lifespan context for GKE snapshotting.")
    return app


if __name__ == "__main__":
    import uvicorn

    try:
        from vllm.entrypoints.openai.api_server import app
    except ImportError as err:
        raise RuntimeError("vLLM module must be installed to run vllm/wrapper.py entrypoint") from err

    patched_app = patch_vllm_lifespan(app)
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    logger.info("Starting vLLM OpenAI API server with patched lifespan on %s:%d", host, port)
    uvicorn.run(patched_app, host=host, port=port)
