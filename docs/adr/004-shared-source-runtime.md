# ADR — docker 0.4.0: shared source runtime

Accepted 2026-10-08. Supersedes embedding common/workspace source in generated tools.

Generic guardrails and file transport belong to installed TWYLT >=1.1.1. Shared
business helpers live in shared/docker_common/common.py and are loaded using sys.path
anchored to the tool's __file__. Each tool contains only its contracts, metadata
and own business operation. No Python distribution is built for the pack.

Only logic required by multiple tools is shared. Tool-only models/functions stay
in that tool; external imports are selected accordingly. Schemas remain compatible.
Git and container-specific protections remain author-owned business policy. Docker
watchdog imports the installed TWYLT library rather than embedding its source.

Remove code-copy generators and retain export_schemas.py. Test-only legacy facades
aggregate new modules to preserve prior behavioral tests and monkeypatch targets;
production tools never import those facades. Keep tools/shared together; builder
launchers retain source paths. Distinct module namespaces reduce collisions; use
separate processes for different source-pack versions. Shared source is trusted.

Guardrails are opt-in outside images; transport cwd can be a descendant of an
additional allowed root. Cooperative checks are not an OS sandbox. The deployment
layout and error-code change justify the minor version bump.
