"""Bounded synthetic terminal checks for the community guide (CI only)."""

import errno
import os
from pathlib import Path
import pty
import re
import select
import subprocess
import time


def terminal(command, replies, *, env, cwd):
    master, slave = pty.openpty()
    child = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                             env=env, cwd=cwd, start_new_session=True)
    os.close(slave)
    output = b""
    cursor = 0
    deadline = time.monotonic() + 45
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                try:
                    chunk = os.read(master, 4096)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                output += chunk
                if replies and replies[0][0] in output[cursor:]:
                    _, value = replies.pop(0)
                    cursor = len(output)
                    os.write(master, value + b"\n")
        assert not replies, "Interactive guide prompt was not observed"
        assert child.wait(timeout=5) == 0
        return output
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        os.close(master)


def hash_file(caddy_image, destination, *, env, cwd):
    password = b"community-fixture-password"
    name = "guide-hash-" + destination.parent.name
    try:
        output = terminal(["docker", "run", "--rm", "--name", name, "-it", "--network", "none", caddy_image,
                           "caddy", "hash-password", "--algorithm", "bcrypt"],
                          [(b"Enter password:", password), (b"Confirm password:", password)],
                          env=env, cwd=cwd)
    finally:
        subprocess.run(["docker", "rm", "-f", name], env=env, cwd=cwd,
                       capture_output=True, timeout=15, check=False)
    assert password not in output, "Password echoed in PTY"
    value, = re.findall(rb"\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}", output)
    # Execute the guide's protected-file snippet, changing only its fixture path.
    guide = (Path(cwd) / "deploy/community/README.md").read_text()
    code = guide.split("python3 - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
    code = code.replace("/srv/library-owner.hash", str(destination))
    previous = os.umask(0o077)
    try:
        transcript = terminal(["python3", "-c", code], [(b"Paste only the bcrypt hash:", value)],
                              env=env, cwd=cwd)
    finally:
        os.umask(previous)
    assert value not in transcript
    assert destination.read_bytes() == value + b"\n"
    assert destination.stat().st_mode & 0o777 == 0o600
    print("Guide interactive bcrypt prompts and protected hash paste passed")
