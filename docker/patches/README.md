# Patches to `NVIDIA/nccl`

Applied to the source tree cloned at `NCCL_REF` by `docker/Dockerfile.snapshot`, in
filename order, with `git apply`. A patch that no longer applies fails the build
rather than being skipped, so bumping `NCCL_REF` forces a decision about each one.

Delete a patch once the equivalent fix is in the pinned release.

## `0001-nccl-checkpoint-config-version-skew.patch`

`allocInitParams()` in `contrib/nccl_checkpoint/shim.cc` copies the caller's
`ncclConfig_t` with a whole-struct assignment, then overwrites `size`, `magic` and
`version` with the current release's values.

`ncclConfig_t` is versioned by its leading `size` field precisely so a caller built
against an older `nccl.h` can pass a shorter struct — `parseCommConfig()` reads
`size` and leaves anything beyond it at the default. Both callers in this image are
older: vLLM and PyTorch ship against `nvidia-nccl-cu12` 2.27.5, whose
`NCCL_CONFIG_INITIALIZER` predates the six fields 2.30 added
(`nChannelsPerNetPeer`, `nvlinkCentricSched`, `graphUsageMode`, `numRmaCtx`,
`maxP2pPeers`, `graphStreamOrdering`).

So the copy reads past the caller's object, the new fields hold whatever was there,
and rewriting `size` tells NCCL to validate them anyway. At restore,
`restoreCommViaInit()` replays the saved config through `ncclCommInitRankConfig()`
and NCCL rejects the first field it reaches:

```
init.cc:2371 (parseCommConfig) NCCL WARN Invalid config nChannelsPerNetPeer attribute value 0
NCCL Checkpoint: shim_checkpoint.cc:140 (restoreCommViaInit) -> 4
```

`ncclCheckpointRestore()` then fails with `ncclInvalidArgument` on every rank.

The patch starts from `NCCL_CONFIG_INITIALIZER` and `memcpy`s only `configPtr->size`
bytes over it, so fields the caller never had keep their `NCCL_CONFIG_UNDEF_INT`
sentinel. It also reads `netName` / `commName` back out of the local copy instead of
the caller's struct, which removes the same overread on that path.

Not reported upstream yet.
