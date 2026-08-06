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
    from .vllm.wrapper import execute_nccl_re_rendezvous

    # Ensure /var/run/nccl/kvs.txt exists for initial NCCL initialization
    execute_nccl_re_rendezvous()

    sys.argv = ["vllm", "serve", *sys.argv[1:]]
    main()
