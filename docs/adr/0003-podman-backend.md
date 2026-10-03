# 0003 — Docker / Podman engine selection

Accepted 2026-10-02.

Use Podman's Docker-compatible API through docker-py 7.1.0, pinned to API 1.40
for Podman, auto-negotiated for Docker. No CLI subprocess backend or new SDK.
Keep existing tool names, payloads, TWYLT_DOCKER_* policies, labels and audit codes.
Engine selection is trusted process configuration; JSON environment affects only
the application. No implicit backend fallback. Explicit host selection wins over
engine-specific environment variables; unrelated engine variables are ignored.

All subprocesses receive the resolved endpoint/API version, including pull and
independent watchdog. Atomic container names limit concurrency within one engine
storage, not across independent engines/users. Operators must use identical engine,
endpoint and capacity settings across workers. Product identity is not probed.

Podman's compatibility create handler maps NanoCPUs to CPUPeriod/CPUQuota, Memory
and MemorySwap to native options. Retain fail-closed Docker info capability checks;
add a rootless cgroup v2 gate. Podman log options omit unsupported max-file; retain
json-file compatibility driver and max-size. No silent relaxation of resource,
network, bind, capability or deadline policies. Host administrators own socket,
cgroup delegation and SELinux setup.

References consulted 2026-10-02:
- https://docs.podman.io/en/latest/markdown/podman-system-service.1.html
- https://docs.podman.io/en/v4.7.2/markdown/podman-run.1.html
- https://github.com/containers/podman/blob/v5.4.2/pkg/api/handlers/compat/info.go
- https://github.com/containers/podman/blob/v5.4.2/pkg/api/handlers/compat/containers_create.go

The two source files were inspected for info capability flags and create mappings.
Unit/fake-engine tests are not live Podman certification. Live tests additionally
check effective cgroup v2 settings and isolated interfaces on the target host.
