"""NCCL checkpoint hooks, executed in the vLLM worker processes.

At TP>1 the NCCL communicators do not live in the process that patches the FastAPI
lifespan. vLLM v1 runs an API server process (uvicorn + AsyncLLM), an EngineCore
process (scheduler + MultiprocExecutor), and one worker process per rank; only the
workers hold communicators. ncclCheckpointPrepare() and ncclCheckpointRestore() are
collective over those ranks, so they have to be dispatched with collective_rpc.

This class is mixed into vLLM's Worker via --worker-extension-cls. See
vllm/v1/worker/worker_base.py, which dynamically appends it to Worker.__bases__ and
exposes its public methods to collective_rpc.
"""

from __future__ import annotations

import ctypes
import os
import threading

from vllm.logger import init_logger

logger = init_logger("vllm.snapshot.worker")

# ncclResult_t, from nccl.h.
_NCCL_SUCCESS = 0
_NCCL_RESULT_NAMES = {
    0: "ncclSuccess",
    1: "ncclUnhandledCudaError",
    2: "ncclSystemError",
    3: "ncclInternalError",
    4: "ncclInvalidArgument",
    5: "ncclInvalidUsage",
    6: "ncclRemoteError",
    7: "ncclInProgress",
}

_handle_lock = threading.Lock()
_handle: ctypes.CDLL | None = None
_version_logged = False


class NCCLCheckpointError(RuntimeError):
    """An ncclCheckpoint* entry point is missing or returned an error."""


def _global_scope() -> ctypes.CDLL:
    """Handle to the process-global symbol scope, where LD_PRELOAD put the shim.

    nccl_checkpoint.h resolves these entry points with dlsym(RTLD_DEFAULT, ...);
    ctypes.CDLL(None) is the equivalent. Opening the .so by path would instead give
    an RTLD_LOCAL handle to a second mapping of the library rather than to the
    preloaded interposer that is actually wrapping this process's NCCL calls.
    """
    global _handle
    with _handle_lock:
        if _handle is None:
            _handle = ctypes.CDLL(None)
    return _handle


def _resolve(name: str):
    try:
        fn = getattr(_global_scope(), name)
    except AttributeError as exc:
        raise NCCLCheckpointError(
            f"{name} is not in the global symbol scope. The NCCL checkpoint shim "
            f"must be preloaded; LD_PRELOAD is currently "
            f"{os.environ.get('LD_PRELOAD', '<unset>')!r}."
        ) from exc
    fn.restype = ctypes.c_int
    return fn


def _log_shim_version() -> None:
    """Log the shim/NCCL version pair once, as a liveness check on the shim."""
    global _version_logged
    if _version_logged:
        return
    _version_logged = True
    try:
        fn = _resolve("ncclCheckpointGetVersion")
        fn.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        checkpoint_version = ctypes.c_int()
        nccl_version = ctypes.c_int()
        res = fn(ctypes.byref(checkpoint_version), ctypes.byref(nccl_version))
        if res == _NCCL_SUCCESS:
            logger.info(
                "NCCL checkpoint shim v%s against NCCL %s",
                checkpoint_version.value,
                nccl_version.value,
            )
        else:
            logger.warning("ncclCheckpointGetVersion() returned %s", _describe(res))
    except NCCLCheckpointError as exc:
        logger.warning("Could not read NCCL checkpoint shim version: %s", exc)


def _describe(res: int) -> str:
    return f"{_NCCL_RESULT_NAMES.get(res, 'unknown')} ({res})"


def _call(name: str) -> None:
    fn = _resolve(name)
    fn.argtypes = []
    res = fn()
    if res != _NCCL_SUCCESS:
        raise NCCLCheckpointError(f"{name}() returned {_describe(res)}")


class NCCLCheckpointWorkerExtension:
    """Checkpoint hooks callable via AsyncLLM.collective_rpc.

    Method names must not collide with anything already on vLLM's Worker --
    worker_base.py asserts that before injecting this class, and a collision is a
    hard startup failure. Keep this class to the two RPC entry points; helpers
    belong at module scope.
    """

    def nccl_checkpoint_prepare(self) -> None:
        """Quiesce and destroy every NCCL communicator on this rank.

        Collective: all ranks must call it. Nothing may touch a communicator
        between this and nccl_checkpoint_restore().
        """
        rank = getattr(self, "rank", "?")
        _log_shim_version()
        logger.info("Rank %s: ncclCheckpointPrepare()", rank)
        _call("ncclCheckpointPrepare")
        logger.info("Rank %s: ncclCheckpointPrepare() done", rank)

    def nccl_checkpoint_restore(self) -> None:
        """Rebuild every communicator destroyed by nccl_checkpoint_prepare().

        Collective. Rank 0 generates fresh ncclUniqueIds and publishes them to the
        KV store named by NCCL_CHECKPOINT_KVS_PATH; the other ranks poll for them.
        A KVS that is not listening shows up here as a stall of up to
        NCCL_CHECKPOINT_KVS_TIMEOUT (300s upstream default), not as a fast failure.
        """
        rank = getattr(self, "rank", "?")
        logger.info("Rank %s: ncclCheckpointRestore()", rank)
        _call("ncclCheckpointRestore")
        logger.info("Rank %s: ncclCheckpointRestore() done", rank)
