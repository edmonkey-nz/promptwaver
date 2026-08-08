#!/usr/bin/env python3
"""PromptWaver entry point.

Examples
--------
    # preview in the browser, no hardware, local (offline) director (default)
    python run.py

    # drive the Helios, tuned for the CLUB RGB1000 rig with web interface
    python run.py --laser --pps 11000 --max-step 0.03 --invert-x

    # use Claude as the scene director
    ANTHROPIC_API_KEY=sk-... python run.py

    # run headless without web interface
    python run.py --headless

Then open http://localhost:8080 and type a keyword (e.g. "water flowing").
"""

from __future__ import annotations

import argparse
import logging
import os

from promptwaver.engine import Engine
from promptwaver.web import run as run_web
from promptwaver.director import local_scene

logging.getLogger("aiohttp").setLevel(logging.ERROR)

HERE = os.path.dirname(os.path.abspath(__file__))


def parse_args():
    ap = argparse.ArgumentParser(description="PromptWaver — immersive laser + synth instrument")
    ap.add_argument("--headless", action="store_true", help="run without the browser control surface")
    ap.add_argument("--web-port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--laser", action="store_true", help="enable Helios DAC output")
    ap.add_argument("--no-audio", action="store_true", help="disable the synth")
    ap.add_argument("--pps", type=int, default=11000, help="points per second to the DAC")
    ap.add_argument("--max-step", type=float, default=0.03, help="max stroke step (normalized)")
    ap.add_argument("--invert-x", action="store_true")
    ap.add_argument("--keystone-h", type=float, default=0.0)
    ap.add_argument("--keystone-v", type=float, default=0.0)
    ap.add_argument("--fps", type=int, default=45)
    ap.add_argument("--diag", action="store_true",
                    help="enable perf/audio diagnostics instrumentation at startup (off by "
                         "default; also toggleable live in Settings)")
    ap.add_argument("--midi", default=None, metavar="HINT",
                    help="MIDI input port to use, matched by substring (e.g. --midi MPK). "
                         "Without this the saved port, else the first non-loopback "
                         "device, is picked automatically; change it live in Settings.")
    ap.add_argument("--list-midi", action="store_true", help="list MIDI input ports and exit")
    ap.add_argument("--model", default=None, help="override director model id")
    ap.add_argument("--scene", default="water flowing", help="initial keyword")
    return ap.parse_args()


def main():
    args = parse_args()
    if args.list_midi:
        from promptwaver.midi import MidiInput
        names = MidiInput.list_ports()
        if not names:
            print("no MIDI input ports found"
                  if MidiInput.available()
                  else "mido not installed — pip install mido python-rtmidi")
        for n in names:
            print(n)
        return
    engine = Engine(
        library_dir=os.path.join(HERE, "scenes"),
        cache_dir=os.path.join(HERE, "scenes", "generated"),
        fps=args.fps, pps=args.pps, max_step=args.max_step,
        invert_x=args.invert_x, keystone_h=args.keystone_h,
        keystone_v=args.keystone_v, enable_laser=args.laser,
        enable_audio=not args.no_audio, model=args.model,
        enable_diagnostics=args.diag, midi_port=args.midi,
    )
    # start with something on screen immediately
    engine._install_spec(local_scene(args.scene))
    engine.start()
    print(f"[promptwaver] engine running — output={engine.output.name} "
          f"director={'claude' if engine.director.online else 'local'}")
    if not args.headless:
        print(f"[promptwaver] open http://localhost:{args.web_port}")
        try:
            run_web(engine, host=args.host, port=args.web_port)
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            engine.stop()
    else:
        print("[promptwaver] running headless; Ctrl-C to stop")
        try:
            import time
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            engine.stop()


if __name__ == "__main__":
    main()
