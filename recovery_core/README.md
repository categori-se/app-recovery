# project-recovery-core

Alpha 0.1.0a3; Apache-2.0. Not for production use.

Python standard-library recovery mechanics: `filesystem.py` provides bounded regular-file reads, SHA-256, create-only evidence writes and cooperative POSIX locks; `checkpoints.py` validates chunks, immutable snapshots and observed head tokens; `git.py` runs isolated local Git operations and verifies reconstructed bare bundles.

Import from `recovery_core`. Python 3.11+ and local Git are required. Filesystem operations require POSIX support for `O_NOFOLLOW`, directory descriptors, hard links and `flock`.

Applications must control parent directories and supply authorized storage callbacks with create-only and atomic head comparison guarantees. Symlink checks and advisory locks do not exclude unrelated writers. Interrupted publication may leave unreferenced immutable objects; ambiguous responses require explicit reread and reconciliation. Reconstruction does not publish or overlay working files. Identity, credentials, permissions, content approval, custody policy and provider adapters remain application responsibilities.

Registry packages are not published by this source release. Node manifests retain `private: true` to guard against accidental npm publication. See the repository CI for offline test commands.
