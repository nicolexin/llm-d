"""vLLM lifespan patch for GKE fast pod snapshotting.

Wraps vLLM's FastAPI lifespan so that, after the engine has finished initializing
but before uvicorn binds its port, the pod:

1. Releases physical VRAM with engine.sleep(level=1), keeping virtual addresses stable.
2. Destroys every NCCL communicator, on all ranks, via the checkpoint shim.
3. Triggers the gVisor sandbox checkpoint and blocks until the sandbox is restored.
4. Starts the rendezvous KV store and rebuilds the communicators, on all ranks.
5. Re-maps VRAM with engine.wake_up() and hands control back to uvicorn.

The checkpoint deliberately happens before `yield`, so the snapshot contains no
listening sockets. That is also why the KV store is started after the restore
rather than at pod start -- see ensure_kvs_server().
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import socket
import subprocess
import time
from contextlib import asynccontextmanager
from typing import Optional

from vllm.logger import init_logger

from ..providers import GKESnapshotProvider, SnapshotError

logger = init_logger("vllm.snapshot.wrapper")

WORKER_EXTENSION_CLS = (
    "docker.scripts.snapshot.vllm.worker.NCCLCheckpointWorkerExtension"
)

DEFAULT_KVS_PATH = "/var/run/nccl/kvs.txt"
DEFAULT_KVS_ADDR = "127.0.0.1:6379"

# ncclCheckpointRestore polls the KV store for up to NCCL_CHECKPOINT_KVS_TIMEOUT
# (300s upstream default). Let the RPC outlast that so a KVS problem surfaces as the
# shim's own error rather than as an opaque RPC timeout.
PREPARE_RPC_TIMEOUT_S = 120.0
RESTORE_RPC_TIMEOUT_S = 420.0

# Keeps the KV store from being garbage collected out from under us.
_kvs_proc: Optional[subprocess.Popen] = None


def kvs_path() -> pathlib.Path:
    return pathlib.Path(os.getenv("NCCL_CHECKPOINT_KVS_PATH", DEFAULT_KVS_PATH))


def kvs_addr() -> tuple[str, int]:
    host, _, port = os.getenv("NCCL_CHECKPOINT_KVS_ADDR", DEFAULT_KVS_ADDR).partition(":")
    return host or "127.0.0.1", int(port or 6379)


def write_kvs_file() -> None:
    """Publish the KV store endpoint where the shim looks for it.

    kv_store_client.cc reads NCCL_CHECKPOINT_KVS_PATH and parses the contents as
    "<host>:<port>[/<prefix>]". The file only names an endpoint -- writing it does
    not create a KV store.
    """
    host, port = kvs_addr()
    path = kvs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{host}:{port}")
    logger.info("NCCL rendezvous endpoint %s:%s written to %s", host, port, path)


def _kvs_listening(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_kvs_server(startup_timeout: float = 15.0) -> Optional[subprocess.Popen]:
    """Start the NCCL rendezvous KV store unless something is already listening.

    Runs in this container as a child process rather than as a sidecar, and only
    after the checkpoint has been restored. The store is scratch space for one
    rendezvous -- rank 0 writes fresh ncclUniqueIds, the other ranks read them --
    so nothing in it needs to survive the freeze, and IDs captured before the
    checkpoint would be stale anyway. Starting it earlier would also put the first
    bound TCP socket into the snapshot, which this design otherwise avoids.
    """
    global _kvs_proc

    host, port = kvs_addr()
    if _kvs_listening(host, port):
        logger.info("NCCL rendezvous KV store already listening on %s:%s", host, port)
        return _kvs_proc

    redis_server = shutil.which("redis-server")
    if redis_server is None:
        raise SnapshotError(
            "redis-server not found on PATH. The upstream NCCL checkpoint shim "
            "speaks RESP and has no file-backed KV store; install redis-server or "
            "point NCCL_CHECKPOINT_KVS_ADDR at a reachable one."
        )

    proc = subprocess.Popen(
        [
            redis_server,
            "--bind", host,
            "--port", str(port),
            "--protected-mode", "no",
            # Rendezvous state is worthless once restore completes; never persist it.
            "--save", "",
            "--appendonly", "no",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )

    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SnapshotError(
                f"redis-server exited immediately with status {proc.returncode}"
            )
        if _kvs_listening(host, port):
            logger.info(
                "Started NCCL rendezvous KV store on %s:%s (pid %s)", host, port, proc.pid
            )
            _kvs_proc = proc
            return proc
        time.sleep(0.1)

    proc.kill()
    raise SnapshotError(
        f"redis-server did not accept connections on {host}:{port} "
        f"within {startup_timeout}s"
    )


async def _rendezvous(engine) -> None:
    """Bring up the KV store and rebuild NCCL communicators on every rank."""
    ensure_kvs_server()
    write_kvs_file()
    logger.info("Rebuilding NCCL communicators on all ranks...")
    await engine.collective_rpc(
        "nccl_checkpoint_restore", timeout=RESTORE_RPC_TIMEOUT_S
    )


async def _snapshot_and_restore(engine, provider: GKESnapshotProvider) -> None:
    """Run the checkpoint/restore sequence.

    Failure handling is deliberately asymmetric. Before any communicator has been
    touched, giving up and serving normally is strictly better than crashlooping.
    Once ncclCheckpointPrepare() has run the communicators are gone, and a pod that
    keeps going without rebuilding them would accept traffic and fail every
    collective -- so from that point on, failures are fatal.
    """
    # Check this before touching anything. Without a checkpoint interface the whole
    # sequence would still run -- destroying and rebuilding every communicator, and
    # starting a KV store -- to accomplish nothing but a slower startup.
    if not provider.is_available():
        logger.warning(
            "gVisor checkpoint interface is unavailable; skipping snapshot entirely "
            "and starting normally."
        )
        return

    try:
        logger.info("Snapshot 1/5: engine.sleep(level=1)")
        await engine.sleep(level=1)
    except Exception:
        logger.exception(
            "SNAPSHOT ABORTED: engine.sleep(level=1) failed -- is --enable-sleep-mode "
            "set? Nothing has been modified; serving normally."
        )
        return

    logger.info("Snapshot 2/5: quiescing NCCL communicators on all ranks")
    await engine.collective_rpc("nccl_checkpoint_prepare", timeout=PREPARE_RPC_TIMEOUT_S)

    try:
        logger.info("Snapshot 3/5: triggering checkpoint (blocks until restore)")
        await asyncio.to_thread(provider.trigger)
        logger.info("Snapshot 4/5: sandbox restored, re-rendezvousing NCCL")
    except Exception:
        logger.exception(
            "Checkpoint trigger failed. Communicators are already down, so they are "
            "being rebuilt in place before serving."
        )
        await _rendezvous(engine)
        await engine.wake_up()
        return

    await _rendezvous(engine)

    logger.info("Snapshot 5/5: engine.wake_up()")
    await engine.wake_up()
    logger.info("Snapshot restore complete; handing off to uvicorn.")


def patch_vllm_lifespan(app, snapshot_provider: Optional[GKESnapshotProvider] = None):
    """Patch the FastAPI app's lifespan to checkpoint the pod during startup.

    Args:
        app: The vLLM FastAPI application instance.
        snapshot_provider: Snapshot provider. Defaults to GKESnapshotProvider() when
            SNAPSHOT_PROVIDER is 'gke', 'gvisor' or 'gke_gvisor'; otherwise the app
            is returned unmodified.
    """
    if snapshot_provider is None:
        provider_type = os.getenv("SNAPSHOT_PROVIDER", "").lower()
        if provider_type not in ("gke", "gvisor", "gke_gvisor"):
            logger.info(
                "Snapshot provider disabled (SNAPSHOT_PROVIDER=%r). Lifespan unmodified.",
                provider_type,
            )
            return app
        snapshot_provider = GKESnapshotProvider()

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def patched_lifespan(app_inst):
        async with original_lifespan(app_inst) as state:
            engine = getattr(app_inst.state, "engine_client", None)
            if engine is None:
                logger.error(
                    "No engine_client on app state; skipping snapshot and serving normally."
                )
            else:
                await _snapshot_and_restore(engine, snapshot_provider)
            yield state

    app.router.lifespan_context = patched_lifespan
    logger.info("Patched vLLM lifespan for GKE snapshotting with the NCCL checkpoint shim.")
    return app
