"""Package the add-on into an installable Blender extension zip: dist/CheryStage_v<version>.zip

Run tools/build.py first so chery_stage/mmd_bake/bin/mmdbake.exe exists.
Usage:  python tools/pack.py
"""

import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "chery_stage")
SKIP_DIRS = {"__pycache__", "icons_cache"}


def main():
    manifest = open(os.path.join(SRC, "blender_manifest.toml"), encoding="utf-8").read()
    version = re.search(r'^version\s*=\s*"([^"]+)"', manifest, re.M).group(1)
    exe = os.path.join(SRC, "mmd_bake", "bin", "mmdbake.exe")
    if not os.path.isfile(exe):
        sys.exit("mmdbake.exe missing: run `python tools/build.py` first")

    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    out = os.path.join(ROOT, "dist", f"CheryStage_v{version}.zip")
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for r, dirs, files in os.walk(SRC):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                if f.endswith(".pyc") or f == ".gitkeep":
                    continue
                p = os.path.join(r, f)
                z.write(p, os.path.relpath(p, SRC))
                count += 1
    print(f"OK -> {os.path.relpath(out, ROOT)} ({count} files)")


if __name__ == "__main__":
    main()
