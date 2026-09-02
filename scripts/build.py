"""Builds the Lambda deployment package.

One package for every handler: the code is small, and a single artefact means the eight
functions can never drift to different versions of the domain rules.
"""

import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
PACKAGE = ROOT / "dist" / "lambda.zip"


def main() -> int:
    """
    Produces dist/lambda.zip containing src/ and its third-party dependencies.

    Returns: 0 on success. boto3 is deliberately excluded — the Lambda runtime provides it,
             and bundling it roughly triples the package size and the cold start.
    """
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)
    PACKAGE.parent.mkdir(exist_ok=True)

    # uv rather than pip: the project's venv is uv-managed and has no pip. The explicit
    # platform and version target the Lambda runtime rather than the build machine, so a
    # macOS laptop and CI produce the same artefact.
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "httpx",
            "--target",
            str(BUILD),
            "--python-platform",
            "x86_64-manylinux2014",
            "--python-version",
            "3.12",
            "--quiet",
        ],
        check=True,
    )
    shutil.copytree(ROOT / "src", BUILD / "src")

    if PACKAGE.exists():
        PACKAGE.unlink()

    with zipfile.ZipFile(PACKAGE, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in BUILD.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(BUILD))

    print(f"built {PACKAGE} ({PACKAGE.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
