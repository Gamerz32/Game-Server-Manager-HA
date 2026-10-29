# Security Policy

## Reporting a vulnerability

Please report security issues using GitHub's private vulnerability reporting feature for this repository. If private reporting is unavailable, contact the repository maintainers through GitHub and request a private disclosure channel. Do not post exploit details in a public issue or pull request before a fix or mitigation is available.

Include the affected commit or version, relevant configuration, impact, and a minimal reproduction. Do not include live credentials or personal data in the report.

## Security review scope

This project manages dedicated game server containers from Home Assistant. Security reports should identify a concrete security impact, such as:

- Bypassing access controls on the manager API or reaching control-plane actions outside their intended Home Assistant ingress path.
- Unauthorized Docker Engine or host access, container escape, or unauthorized control of managed servers or their data.
- Path traversal, unsafe archive extraction, or crafted world, add-on, mod, or catalog content that causes arbitrary file access, overwrite, or code execution.
- Command, configuration, environment, or template injection.
- Server-side request forgery or unsafe outbound requests caused by user-controlled image references, URLs, or catalog metadata.
- Exposure of credentials or other sensitive data through persistent files, API responses, logs, or diagnostics.
- Denial of service or resource exhaustion caused by reachable inputs without reasonable bounds.
- A vulnerable dependency, container image, workflow, or deployment setting with a demonstrated impact on this project.

## Trust boundaries

- Treat requests, server configuration, uploaded worlds and add-on archives, catalog metadata, and downloaded content as untrusted input.
- The manager uses the Docker Engine API to create and control containers. A defect that expands access across this boundary can affect the Home Assistant host.
- Game-server network ports may be intentionally published for player connections; assess reports based on whether they enable unauthorized control, data access, or another concrete security impact.
