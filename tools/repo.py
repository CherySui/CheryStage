"""Build the Blender extension repository (static files) for the current version, optionally publish it.

The repository is what Blender's "Get Extensions" talks to: an index.json listing the zip(s).
It is hosted on GitHub Pages from the gh-pages branch:

    https://cherysui.github.io/CheryStage/index.json

Blender then checks it for updates and installs / updates CheryStage in one click.

Usage:
    python tools/pack.py                    # dist/CheryStage_v<version>.zip
    python tools/repo.py                    # dist/repo/ (zip + index.json + index.html)
    python tools/repo.py --push             # ...and commit it to gh-pages and push

Blender is found via --blender, the BLENDER environment variable, or `blender` on PATH.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
REPO = os.path.join(DIST, "repo")
PAGES = os.path.join(DIST, "gh-pages")   # git worktree of the gh-pages branch
BRANCH = "gh-pages"


def run(cmd, **kw):
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, **kw)


def version():
    manifest = open(os.path.join(ROOT, "chery_stage", "blender_manifest.toml"), encoding="utf-8").read()
    return re.search(r'^version\s*=\s*"([^"]+)"', manifest, re.M).group(1)


def find_blender(arg):
    exe = arg or os.environ.get("BLENDER") or shutil.which("blender")
    if not exe or not os.path.isfile(exe):
        sys.exit("Blender not found: pass --blender C:/path/blender.exe or set BLENDER")
    return exe


def build(blender):
    zip_name = f"CheryStage_v{version()}.zip"
    src = os.path.join(DIST, zip_name)
    if not os.path.isfile(src):
        sys.exit(f"{zip_name} missing: run `python tools/pack.py` first")
    shutil.rmtree(REPO, ignore_errors=True)
    os.makedirs(REPO)
    shutil.copy2(src, REPO)   # only the latest version is offered
    run([blender, "--factory-startup", "--command", "extension", "server-generate",
         "--repo-dir", REPO, "--html"])
    for f in ("index.json", "index.html"):
        if not os.path.isfile(os.path.join(REPO, f)):
            sys.exit(f"server-generate did not write {f}")
    print(f"OK -> {os.path.relpath(REPO, ROOT)} ({zip_name})")
    return zip_name


def publish(zip_name):
    git = ["git", "-C", ROOT]
    if not os.path.isdir(PAGES):
        has_remote = subprocess.run(git + ["ls-remote", "--exit-code", "--heads", "origin", BRANCH],
                                    capture_output=True).returncode == 0
        if has_remote:
            run(git + ["fetch", "origin", f"{BRANCH}:{BRANCH}"])
            run(git + ["worktree", "add", PAGES, BRANCH])
        else:
            run(git + ["worktree", "add", "--orphan", "-b", BRANCH, PAGES])
    pg = ["git", "-C", PAGES]
    for f in os.listdir(PAGES):
        if f != ".git":
            p = os.path.join(PAGES, f)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    for f in os.listdir(REPO):
        shutil.copy2(os.path.join(REPO, f), PAGES)
    open(os.path.join(PAGES, ".nojekyll"), "w").close()
    run(pg + ["add", "-A"])
    if subprocess.run(pg + ["diff", "--cached", "--quiet"]).returncode == 0:
        print("gh-pages already up to date")
        return
    run(pg + ["commit", "-q", "-m", f"Extension repository: {zip_name}"])
    run(pg + ["push", "-q", "origin", BRANCH])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--blender")
    ap.add_argument("--push", action="store_true", help="commit dist/repo to gh-pages and push")
    a = ap.parse_args()
    zip_name = build(find_blender(a.blender))
    if a.push:
        publish(zip_name)


if __name__ == "__main__":
    main()
