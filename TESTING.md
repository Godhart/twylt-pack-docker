# Validation — Docker TWYLT Tool 0.3.0

2026-10-02, Linux, Python 3.12, TWYLT 1.0.0, Docker SDK 7.1.0.

`python -m pytest -q`: **107 passed, 6 skipped**.

Executed:
- Docker/Podman selection, endpoint precedence, API version, log-driver options;
- Podman rootless cgroup v1 denial and v2 admission, pull/list endpoint propagation;
- network policy: accepted/invalid values, deny-before-daemon, audit code, none/bridge mapping;
- tagged image filtering, deduplication, sorting, pagination, metadata and sanitized failures;
- both standalone tools expose valid TWYLT discovery metadata;
- shared workspace and incident-log regression suite, including parallel append;
- strict inputs, registry/pull rules, resource configuration and bind validation;
- fake engine lifecycle, timeout, exit/OOM handling, output truncation and cleanup failures;
- concurrent thread calls racing for atomic daemon-name slots (peak <= configured N);
- real installed SDK serialization of CPU/RAM/swap/network/bind settings;
- describe/file transport checks without Docker;
- actual independent guardian processes using a file-backed fake Docker API,
  including SIGKILL of the calling process, timeout kill and later removal.

Skipped:
1. Cross-process test through the real SDK and a fake Unix HTTP Docker daemon:
   this execution environment raises PermissionError when creating AF_UNIX sockets.
2–6. Opt-in live Docker/Podman tests for exit/logs/cleanup, timeout, writable bind mounts,
   tagged image inventory and effective cgroup v2 limits / isolated interfaces.
   Neither Docker nor Podman is available in this environment.

These results do NOT establish real Docker/Podman cgroup enforcement, network isolation,
volume mount behavior or cleanup on a production daemon. Run the supplied live tests
on a dedicated supported Linux Docker installation before relying on this release.
The source of the shared workspace policy matches Filesystem TWYLT Pack 0.5.0.
ToolHub GUI import, Windows, macOS, Docker Desktop and remote daemons were not tested;
the latter platforms/daemon modes are outside this implementation's support.
