"""Build the native physics baker (mmdbake.exe) and copy it into the add-on.

Steps:
  1. make sure the git submodules (saba, Bullet) are checked out
  2. apply our patches to them (skipped if already applied)
  3. configure + build with CMake / Ninja (MinGW-w64 g++)
  4. copy mmdbake.exe to chery_stage/mmd_bake/bin/

Usage:  python tools/build.py
"""

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NATIVE = os.path.join(ROOT, "native", "mmdbake")
BUILD = os.path.join(NATIVE, "build")
PATCHES = {
    "third_party/saba": "patches/saba-multithread.patch",
    "third_party/bullet3": "patches/bullet-mt-island-fix.patch",
}


def run(cmd, cwd=None, check=True, quiet=False):
    print("+", " ".join(cmd))
    res = subprocess.run(cmd, cwd=cwd, capture_output=quiet, text=True)
    if check and res.returncode != 0:
        if quiet:
            print(res.stdout, res.stderr)
        sys.exit(f"command failed: {' '.join(cmd)}")
    return res


def main():
    run(["git", "submodule", "update", "--init", "--depth", "1"], cwd=ROOT)

    for sub, patch in PATCHES.items():
        repo = os.path.join(NATIVE, sub)
        patch_path = os.path.join(NATIVE, patch)
        already = run(["git", "apply", "--reverse", "--check", patch_path], cwd=repo, check=False, quiet=True)
        if already.returncode == 0:
            print(f"  {patch}: already applied")
            continue
        run(["git", "apply", patch_path], cwd=repo)

    run(["cmake", "-S", NATIVE, "-B", BUILD, "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release"])
    run(["cmake", "--build", BUILD, "-j", str(os.cpu_count() or 4)])

    exe = os.path.join(BUILD, "mmdbake.exe")
    dst_dir = os.path.join(ROOT, "chery_stage", "mmd_bake", "bin")
    os.makedirs(dst_dir, exist_ok=True)
    shutil.copy2(exe, os.path.join(dst_dir, "mmdbake.exe"))
    print(f"OK -> {os.path.relpath(os.path.join(dst_dir, 'mmdbake.exe'), ROOT)}")


if __name__ == "__main__":
    main()
