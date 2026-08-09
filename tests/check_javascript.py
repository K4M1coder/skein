import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main():
    scripts = sorted((ROOT / "static").glob("*.js"))
    for script in scripts:
        result = subprocess.run(["node", "--check", str(script)], cwd=ROOT, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
