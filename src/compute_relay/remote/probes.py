"""Bounded fixed software probes shared by catalog and environment discovery."""

import os
import selectors
import signal
import subprocess
import time


def software_command(argv, *, extra_env=None, timeout=8, max_output_bytes=65536):
    """Bound both live output and time for fixed, operator-enabled probes.

    HOME/config/plugin isolation avoids personal shell startup and Conda registry
    writes. This never invokes a discovered Python or a project/Poetry command.
    """
    if type(max_output_bytes) is not int or not 1 <= max_output_bytes <= 262144:
        raise ValueError("Software probe output limit is invalid")
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C",
           "PYTHONNOUSERSITE": "1", "CONDA_NO_PLUGINS": "true", "CONDARC": "/dev/null"}
    env.update(extra_env or {})
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, cwd="/", env=env, start_new_session=True)
    streams = {process.stdout: bytearray(), process.stderr: bytearray()}
    truncated = False
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout
    try:
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                truncated = True
                break
            for key, _ in selector.select(min(remaining, 0.2)):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = streams[key.fileobj]
                available = max_output_bytes - len(target)
                target.extend(chunk[:available])
                if len(target) >= max_output_bytes:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    return {"returncode": process.returncode, "stdout": bytes(streams[process.stdout]).decode("utf-8", "replace"),
            "stderr": bytes(streams[process.stderr]).decode("utf-8", "replace"), "truncated": truncated}
