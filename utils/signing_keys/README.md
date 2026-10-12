# Managed bootstrap trust roots

DUMB bundles these public keys and pins their full primary fingerprints in
`utils/artifact_verification.py`. Runtime installation does not download new
trust roots or consult key servers. `gpgv` uses only the bundled publisher key
in a private keyring and requires a SHA-256, SHA-384, or SHA-512 signature.

| Publisher | Primary fingerprint | Key source |
| --- | --- | --- |
| Bun | `F3DCC08A8572C0749B3E18888EAB4D40A7B22B59` | [Official Bun Dockerfile at reviewed revision](https://github.com/oven-sh/bun/blob/c7b06d94bac19817ba34b6677bb1099fb4f6d2be/dockerhub/alpine/Dockerfile) |
| Microsoft .NET | `2B930AB1228D11D5D7F6B6ACB9CF1A51FC7D3ACF` | [Official installer key](https://dot.net/v1/dotnet-install.asc), [Microsoft's fingerprint and signature instructions](https://learn.microsoft.com/en-us/dotnet/core/tools/dotnet-install-script#signature-validation-of-dotnet-installsh) |

Reviewed on 2026-10-12 against the official sources and their current signatures.
Bun's `SHASUMS256.txt.asc` is a clear-signed checksum manifest. Its authenticated
body must match the separately downloaded manifest before an archive checksum
is used. Microsoft's `dotnet-install.sig` authenticates `dotnet-install.sh`.
SDK archive SHA-512 values come from official HTTPS release metadata; these
checksums do not add a separate publisher signature to the SDK archive.

When a publisher rotates or revokes a key, verify the new full fingerprint
through official publisher sources, review the replacement key and pinned
fingerprint together, and run the signature regression tests. Installation
fails until a required signature verifies; it never automatically trusts a
replacement key. Bundled trust roots require maintenance: this verifier does
not discover new revocations or rotations from a remote service.
