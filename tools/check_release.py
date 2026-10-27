"""Validate the explicit public file inventory and optionally build an allowlisted ZIP.

This is a publication guard, not a comprehensive secret or security scanner.
Review content and licensing before publishing. Update FILES deliberately when
adding source, tests or documentation.
"""

import argparse
from pathlib import Path
import re
import subprocess
import shutil
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    ".gitattributes", ".gitignore", ".github/workflows/checks.yml",
    "LICENSE", "NOTICE.md", "README.md", "CONTRIBUTING.md",
    "Fusion_to_Quilter.cmd", "Quilter_to_Fusion.cmd", "Selftest_Roundtrip.cmd",
    "docs/index.html", "docs/render_assets.py", "docs/workflow.md",
    "docs/validation.md", "docs/publishing.md",
    "docs/assets/lab-wordmark.svg", "docs/assets/hero.svg",
    "docs/assets/workflow.svg", "docs/assets/social-card.png",
    "examples/roundtrip-demo/README.md", "examples/roundtrip-demo/generate.py",
    "examples/roundtrip-demo/roundtrip-demo.sch", "examples/roundtrip-demo/roundtrip-demo.brd",
    "tests/test_bridge.py", "tests/test_custom_pad_copper.py",
    "tools/eagle_brd_to_kicad.py", "tools/eagle_common.py", "tools/eagle_sch_to_kicad.py",
    "tools/fusion_to_quilter.py", "tools/kicad_pcb_reader.py", "tools/kicad_to_fusion.py",
    "tools/quilter_to_fusion.py", "tools/selftest_roundtrip.py", "tools/verify_forward.py",
    "tools/check_release.py", "tools/custom_pad_copper.py", "tools/browser_download.py",
)
RUNTIME = {"input", "from_quilter", "to_quilter", "to_fusion", "build", "dist",
           ".git", ".venv", ".vscode", "__pycache__", ".pytest_cache"}
SENSITIVE = (
    re.compile(r"(?:[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]|[/]home[/]|[/]Users[/])"),
    re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def check():
    errors = []
    expected = set(FILES)
    for rel in FILES:
        path = ROOT / rel
        if not path.is_file() or path.is_symlink() or ROOT not in path.resolve().parents:
            errors.append(f"Missing, linked or external release file: {rel}")
            continue
        data = path.read_bytes()
        if path.suffix == ".png":
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                errors.append(f"Invalid PNG: {rel}")
            elif (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")) != (1280, 640):
                errors.append(f"Social preview must be exactly 1280 x 640: {rel}")
            continue
        text = data.decode("utf-8")
        if any(p.search(text) for p in SENSITIVE):
            errors.append(f"Potential credential or personal absolute path: {rel}")
        if path.suffix in (".svg", ".sch", ".brd"):
            root = ET.fromstring(text)
            if path.suffix == ".svg":
                for item in root.iter():
                    if item.tag.rsplit("}", 1)[-1] in ("script", "foreignObject"):
                        errors.append(f"Active SVG content: {rel}")
                    if any(k.rsplit("}", 1)[-1] in ("href", "src") and
                           not v.startswith("#") for k, v in item.attrib.items()):
                        errors.append(f"External SVG resource: {rel}")
        if path.suffix == ".py":
            compile(text, str(path), "exec")
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if path.is_file() and not any(p in RUNTIME for p in rel.parts) and rel.as_posix() not in expected:
            errors.append(f"Unreviewed file outside runtime directories: {rel.as_posix()}")
    if (ROOT / ".git").exists():
        git = shutil.which("git")
        if not git:
            errors.append("Git repository detected but git is unavailable; cannot inspect tracked files.")
        else:
            result = subprocess.run([git, "-C", str(ROOT), "ls-files", "-z"],
                                    capture_output=True, check=True)
            tracked = set(result.stdout.decode("utf-8").rstrip("\0").split("\0")) - {""}
            for rel in sorted(tracked - expected):
                errors.append(f"Tracked file is not allowlisted (even if ignored): {rel}")
    if errors:
        for error in errors:
            print("FAIL  " + error)
        return False
    print(f"PASS  {len(FILES)} explicitly allowlisted public files; syntax, XML and local-path checks passed.")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--archive", action="store_true", help="write dist/fusion-quilter-bridge.zip")
    args = ap.parse_args()
    if not check():
        return 1
    if args.archive:
        out = ROOT / "dist" / "fusion-quilter-bridge.zip"
        out.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for rel in sorted(FILES):
                archive.write(ROOT / rel, "fusion-quilter-bridge/" + rel)
        print(f"Wrote {out.name}: {len(FILES)} files. Review ownership and branding before publishing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
