"""Entrypoint that starts `vllm serve` with the snapshot hook installed.

Run as:  python3 -m docker.scripts.snapshot.launcher <vllm serve args...>

Two placement rules, both of which fail silently if broken:

1. This file must not be named __main__.py, and must not be invoked as
   `python3 -m snapshot`. When vLLM spawns child processes, CPython's spawn
   bootstrap re-imports the parent's main module as __mp_main__ -- which is
   how this patch reaches vLLM's spawned API server processes when
   --api-server-count > 1 -- but it skips modules whose name ends in
   ".__main__".
2. The rebind must stay above the __main__ guard, for the same reason.
"""

import sys

from vllm.entrypoints.openai import api_server

from .vllm.wrapper import patch_vllm_lifespan

_orig_build_app = api_server.build_app


def _build_app(*args, **kwargs):
    return patch_vllm_lifespan(_orig_build_app(*args, **kwargs))


api_server.build_app = _build_app

if __name__ == "__main__":
    from vllm.entrypoints.cli.main import main

    from .vllm.wrapper import WORKER_EXTENSION_CLS, write_kvs_file

    argv = list(sys.argv[1:])

    # The checkpoint hooks have to run in the worker processes -- they, not the API
    # server, own the NCCL communicators. See vllm/worker.py.
    if not any(a.split("=", 1)[0] == "--worker-extension-cls" for a in argv):
        argv.append(f"--worker-extension-cls={WORKER_EXTENSION_CLS}")

    # Publish the rendezvous endpoint up front so the file is in place no matter
    # when the shim reads it. The KV store itself is not started until after the
    # snapshot restores.
    write_kvs_file()

    sys.argv = ["vllm", "serve", *argv]
    main()
