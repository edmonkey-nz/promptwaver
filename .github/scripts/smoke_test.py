"""Start a freshly built PromptWaver executable and check it actually works.

Run by .github/workflows/build.yml after PyInstaller, on every platform, so a
binary that dies on startup or ships without part of itself fails the build
instead of reaching a release. Both have happened: the first packaged build
bundled no web UI at all, and every release up to 0.91.0 shipped without MIDI
because CI never installed it — neither was visible from "the build passed".

    python .github/scripts/smoke_test.py dist/promptwaver-windows-x86_64.exe

Checks, against the running binary:
  - the web UI, the GPU renderer's scripts and the About text are served
  - the engine reports its version, and MIDI is importable (mido + rtmidi)
  - the starter scene library is there, and once one of its scenes is loaded
    and started, its packed geometry arrives over the websocket (the GPU
    output path) followed by per-tick frames

  - sounddevice and its PortAudio library load (the engine reports an
    `audio_error` if not). Runners have no sound device, so the stream itself
    is not expected to open — only the library is checked. Audio is disabled
    through the engine's own mute gate before Start, so a machine that DOES
    have speakers stays silent.

No window. Exits non-zero with the binary's own output and error.txt on any
failure.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

import aiohttp

# Results must reach the CI log as they happen, not when (if) the process exits.
sys.stdout.reconfigure(line_buffering=True)

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"


def get(path: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.status, r.read()


def wait_up(proc, secs=90) -> bool:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if proc.poll() is not None:
            return False
        try:
            if get("/")[0] == 200:
                return True
        except Exception:
            time.sleep(1)
    return False


async def ws_checks() -> dict:
    out = {}
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"{BASE.replace('http', 'ws')}/ws?gpu=1", max_msg_size=0) as ws:
            end = time.monotonic() + 30
            while "state" not in out and time.monotonic() < end:
                m = await ws.receive(timeout=10)
                if m.type == aiohttp.WSMsgType.TEXT:
                    d = json.loads(m.data)
                    if d.get("type") == "state":
                        out["state"] = d["state"]
            await ws.send_str(json.dumps({"type": "set_audio_disabled", "value": True, "fade": 0}))
            # The app opens with NO scene (0.94), so load one from the seeded
            # library first — that also checks the starter library shipped.
            lib = out["state"].get("library") or []
            out["library"] = len(lib)
            if lib:
                await ws.send_str(json.dumps({"type": "scene_load", "name": lib[0]}))
                await asyncio.sleep(2.0)
            await ws.send_str(json.dumps({"type": "set_active", "value": True}))
            frames = 0
            end = time.monotonic() + 30
            while time.monotonic() < end and not (out.get("geometry") and frames >= 10):
                m = await ws.receive(timeout=10)
                if m.type == aiohttp.WSMsgType.BINARY:
                    out["geometry"] = len(m.data)
                elif m.type == aiohttp.WSMsgType.TEXT and json.loads(m.data).get("type") == "frame":
                    frames += 1
            out["frames"] = frames
            await ws.send_str(json.dumps({"type": "set_active", "value": False}))
    return out


def stop(proc):
    """Stop the binary AND everything it started.

    A one-file PyInstaller executable is a bootloader that unpacks the app and
    runs it as a CHILD process. On Linux/macOS the bootloader forwards the
    signal; on Windows `terminate()` kills only the bootloader and the child
    server keeps running — which is what hung the first Windows run of this
    test for an hour. So on Windows the whole tree goes, via taskkill /T."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass                                # never block the job on this


def main() -> int:
    exe = os.path.abspath(sys.argv[1])
    # The binary's output goes to a FILE, not a pipe: a pipe stays open for as
    # long as any process holding it lives, so reading it to EOF could block on
    # a stray child forever. A file can always be read back.
    log_path = os.path.join(os.path.dirname(exe), "smoke-output.log")
    log = open(log_path, "wb")
    proc = subprocess.Popen(
        [exe, "--web-port", str(PORT), "--no-gui", "--no-kiosk"],
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        cwd=os.path.dirname(exe))
    fails: list[str] = []
    out: dict = {}
    try:
        if not wait_up(proc):
            fails.append("the server never came up (process exited or timed out)")
        else:
            for path in ("/", "/output", "/static/renderer.js", "/static/scene-eval.js",
                         "/static/frame-clock.js", "/static/gpu-view.js"):
                code, body = get(path)
                if code != 200 or not body:
                    fails.append(f"{path}: HTTP {code}, {len(body)} bytes")
            about = json.loads(get("/about")[1]).get("text", "")
            if not about or about.startswith("# About\n\nCreate"):
                fails.append("About text missing — promptwaver/content was not bundled")
            r = asyncio.run(ws_checks())
            st = r.get("state") or {}
            midi = st.get("midi") or {}
            print(f"version {st.get('version')} | midi available {midi.get('available')} "
                  f"| geometry {r.get('geometry')} bytes | {r.get('frames')} frames")
            if not st.get("version"):
                fails.append("no engine state over the websocket")
            if st.get("audio_error"):
                fails.append(f"audio library failed to load: {st.get('audio_error')}")
            print(f"audio library ok: {not st.get('audio_error')} | stream open: {st.get('audio')}")
            if not midi.get("available"):
                fails.append("MIDI not available — mido / python-rtmidi missing from the bundle")
            out["ports"] = midi.get("ports")
            if not r.get("library"):
                fails.append("the scene library is empty — the starter scenes were not bundled")
            if not r.get("geometry"):
                fails.append("no scene geometry arrived (GPU output path)")
            if r.get("frames", 0) < 10:
                fails.append(f"only {r.get('frames')} frames arrived")
    except Exception as e:                       # noqa: BLE001 — report, don't hide
        fails.append(f"{type(e).__name__}: {e}")
    finally:
        stop(proc)
        log.close()
        with open(log_path, "rb") as f:
            output = f.read().decode("utf-8", "replace")

    # `available` only means `import mido` worked. mido loads its rtmidi
    # backend BY NAME when a port is listed, so PyInstaller misses it unless
    # told (--hidden-import mido.backends.rtmidi) and the bundle then logs
    # "No module named ..." here. A runner with no MIDI device at all (Linux CI
    # has no ALSA sequencer) is fine — that is a different error.
    midi_lines = [ln for ln in output.splitlines() if "[midi] backend unavailable" in ln]
    if any("No module named" in ln for ln in midi_lines):
        fails.append("MIDI backend missing from the bundle: " + midi_lines[0])
    print(f"midi ports: {out.get('ports')} | midi backend messages: {midi_lines or 'none'}")

    if fails:
        print("SMOKE TEST FAILED:\n  - " + "\n  - ".join(fails))
        print("\n--- binary output ---\n" + output[-4000:])
        err = os.path.join(os.path.dirname(exe), "error.txt")
        if os.path.exists(err):
            print("--- error.txt ---\n" + open(err, encoding="utf-8", errors="replace").read()[-4000:])
        return 1
    print("smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
