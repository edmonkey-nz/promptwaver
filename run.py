#!/usr/bin/env python3
"""LaserFlow entry point.

Examples
--------
    # preview in the browser, no hardware, local (offline) director
    python run.py --web

    # drive the Helios, tuned for the CLUB RGB1000 rig
    python run.py --web --laser --pps 11000 --max-step 0.03 --invert-x

    # use Claude as the scene director
    ANTHROPIC_API_KEY=sk-... python run.py --web

Then open http://localhost:8080 and type a keyword (e.g. "water flowing").
"""

from __future__ import annotations

import argparse
import os

from laserflow.engine import Engine
from laserflow.web import run as run_web
from laserflow.director import local_scene

HERE = os.path.dirname(os.path.abspath(__file__))


def parse_args():
    ap = argparse.ArgumentParser(description="LaserFlow — immersive laser + synth instrument")
    ap.add_argument("--web", action="store_true", help="serve the browser control surface")
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
    ap.add_argument("--model", default=None, help="override director model id")
    ap.add_argument("--scene", default="water flowing", help="initial keyword")
    return ap.parse_args()


def main():
    args = parse_args()
    engine = Engine(
        library_dir=os.path.join(HERE, "scenes"),
        cache_dir=os.path.join(HERE, "scenes", "generated"),
        fps=args.fps, pps=args.pps, max_step=args.max_step,
        invert_x=args.invert_x, keystone_h=args.keystone_h,
        keystone_v=args.keystone_v, enable_laser=args.laser,
        enable_audio=not args.no_audio, model=args.model,
    )
    # start with something on screen immediately
    engine._install_spec(local_scene(args.scene))
    engine.start()
    print(f"[laserflow] engine running — output={engine.output.name} "
          f"director={'claude' if engine.director.online else 'local'}")
    if args.web:
        print(f"[laserflow] open http://localhost:{args.web_port}")
        try:
            run_web(engine, host=args.host, port=args.web_port)
        finally:
            engine.stop()
    else:
        print("[laserflow] running headless; Ctrl-C to stop")
        try:
            import time
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            engine.stop()


if __name__ == "__main__":
    main()
