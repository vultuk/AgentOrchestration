# Runtime Sidecar Filesystems

The compose sidecars run with `read_only: true` so runtime changes cannot be
written into the container root filesystem.

Writable paths are limited to tmpfs mounts:

- `/tmp`: transient process scratch space for readiness markers and local buffers.
- `/var/run/agent`: transient sidecar socket and runtime state directory.

Any new sidecar must set the `com.agent-orchestration.sidecar=true` label,
declare `com.agent-orchestration.writable-paths`, set `read_only: true`, and
mount each documented writable path as `tmpfs`. Sidecars also define a
healthcheck that reads readiness state from the tmpfs path created during
startup. The CI validation step rejects sidecars that omit the read-only
setting, skip startup healthchecks, use undocumented tmpfs paths, or add
writable bind volumes.
