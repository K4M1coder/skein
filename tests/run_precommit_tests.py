import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUITES = (
    "tests.test_frontend_i18n",
    "tests.test_smoke",
    "tests.test_auth",
)


def main():
    for suite in SUITES:
        result = subprocess.run(
            [sys.executable, "-m", "unittest", suite, "-q"],
            cwd=ROOT,
            check=False,
        )
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
