"""Build the single-file zipapp `meshcore-bus.pyz` reproducibly, with the standard library only.

The archive holds the `meshcore_bus` package and a `__main__.py`. Entries are sorted and carry a
fixed date and fixed permissions, so the same sources give the same bytes (with the same zlib).
The installer builds it inside the container; `make pyz` builds it locally.

    python3 tools/build_pyz.py --output dist/meshcore-bus.pyz [--interpreter "/usr/bin/python3 -I"]
"""

from __future__ import annotations

import argparse
import stat
import zipfile
from pathlib import Path

FIXED_DATE = (1980, 1, 1, 0, 0, 0)
MAIN = b"from meshcore_bus.cli import main\n\nraise SystemExit(main())\n"
PACKAGE = "meshcore_bus"


def package_files(source: Path) -> list[Path]:
    root = source / PACKAGE
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix in {".py", ".typed"}
    )


def build(source: Path, output: Path, interpreter: str) -> Path:
    files = package_files(source)
    if not files:
        raise SystemExit(f"no package found below {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".partial")
    with partial.open("wb") as handle:
        handle.write(f"#!{interpreter}\n".encode())
        with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            _add(archive, "__main__.py", MAIN)
            for path in files:
                _add(archive, path.relative_to(source).as_posix(), path.read_bytes())
    partial.chmod(0o755)
    partial.replace(output)
    return output


def _add(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, FIXED_DATE)
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--source", type=Path, default=Path(__file__).resolve().parent.parent / "src"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interpreter", default="/usr/bin/env python3")
    args = parser.parse_args(argv)
    print(build(args.source, args.output, args.interpreter))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
