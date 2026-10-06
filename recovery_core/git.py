"""Portable local Git bundle mechanics; application custody policy stays outside."""
import os
import subprocess


class GitRecoveryError(RuntimeError):
    """A local Git command or recovered reference check failed."""


def git(root, *args, **kwargs):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(["git", "--no-optional-locks", "-c", "core.hooksPath=/dev/null",
                             "-c", "core.fsmonitor=false", "-c", "pack.threads=1", "-c", "protocol.allow=never",
                             "-c", "protocol.file.allow=always", "-C", str(root), *args],
                            env=env, stderr=subprocess.PIPE, **kwargs)
    if result.returncode:
        # Git diagnostics can contain remote URLs or private file contents.
        raise GitRecoveryError(f"Git {args[0]} failed; no backup was completed")
    return result.stdout


def capture(root):
    refs = git(root, "for-each-ref", "--format=%(refname) %(objectname)", stdout=subprocess.PIPE)
    result = dict(line.split(" ", 1) for line in refs.decode().splitlines())
    try:
        head = git(root, "rev-parse", "--verify", "HEAD", stdout=subprocess.PIPE).decode().strip()
    except GitRecoveryError as error:
        raise GitRecoveryError("Repository has no resolvable HEAD commit; use file sync for uncommitted projects") from error
    branch = git(root, "rev-parse", "--symbolic-full-name", "HEAD", stdout=subprocess.PIPE).decode().strip()
    return {"refs": result, "head": head, "head_ref": branch if branch.startswith("refs/") else None}


def materialize(bundle, destination, manifest):
    git(destination.parent, "init", "--bare", "--template=", "--object-format=" + manifest["object_format"], str(destination), stdout=subprocess.PIPE)
    git(destination, "bundle", "verify", str(bundle), stdout=subprocess.PIPE)
    git(destination, "fetch", "--no-tags", str(bundle), "+refs/*:refs/*", "HEAD", stdout=subprocess.PIPE)
    if manifest["head_ref"]:
        git(destination, "symbolic-ref", "HEAD", manifest["head_ref"], stdout=subprocess.PIPE)
    else:
        git(destination, "update-ref", "--no-deref", "HEAD", manifest["head"], stdout=subprocess.PIPE)
    git(destination, "fsck", "--full", "--strict", stdout=subprocess.PIPE)
    if capture(destination) != {key: manifest[key] for key in ("refs", "head", "head_ref")}:
        raise GitRecoveryError("Recovered Git references do not match the backup")
