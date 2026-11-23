"""
The one read at a time, shared by the read-aloud helpers (edge-tts-stream,
openai-tts-stream) and deployed next to them.

A helper that reads puts itself in its own process group and writes that
group's id in PID_FILE. Whoever wants silence (a new read, `--stop`, Scribe)
kills the group named there.
"""
import os
import signal
import subprocess
import time

CONFIG_DIR = os.path.expanduser("~/.config/speak-selection")
PID_FILE = os.path.join(CONFIG_DIR, "speak.pid")

# What a process of a read has in its command line: a helper, its audio
# player, or the Mac's voice.
_READ_MARKS = ("tts-stream", "/bin/play ", "/usr/bin/say ")


def _is_a_read(pgid):
    """Whether the group holds a read. PID_FILE outlives a helper that was
    killed outright, and after a restart its number may belong to any other
    program, which must not be killed in a read's place."""
    try:
        out = subprocess.run(
            ["/bin/ps", "-axo", "pgid=,command="], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return True  # can't tell: a Stop that does nothing is worse
    for line in out.splitlines():
        group, _, command = line.strip().partition(" ")
        if group == str(pgid) and any(m in command for m in _READ_MARKS):
            return True
    return False


def _alive(pgid):
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def stop_existing():
    """Kill the read in progress, if any. SIGTERM first, SIGKILL if the
    group survives half a second."""
    try:
        with open(PID_FILE) as f:
            pgid = int(f.read().strip())
    except (OSError, ValueError):
        return
    # Never our own group: a PID file pointing at the caller's group would
    # otherwise take down the menubar app itself.
    if pgid > 1 and pgid != os.getpgrp() and _alive(pgid) and _is_a_read(pgid):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        else:
            deadline = time.monotonic() + 0.5
            while _alive(pgid):
                if time.monotonic() > deadline:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    break
                time.sleep(0.01)
    try:
        os.unlink(PID_FILE)
    except OSError:
        pass


def claim():
    """Become the read in progress. Must happen BEFORE reading stdin: a
    process wedged in stdin.read() that nothing names is unstoppable from the
    outside."""
    try:
        os.setpgrp()  # one signal then kills us and our player
    except OSError:
        pass  # already a group/session leader (spawned with start_new_session)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpgid(0)))


def release():
    """Remove PID_FILE if it still names us: a newer read may have replaced
    it, and deleting its entry would make that read unstoppable."""
    try:
        with open(PID_FILE) as f:
            if int(f.read().strip()) == os.getpgid(0):
                os.unlink(PID_FILE)
    except (OSError, ValueError):
        pass
