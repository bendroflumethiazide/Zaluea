"""Create a new ATLAS data folder (refuses to touch an existing one).

    python -m atlas.init_data --data C:/Users/you/.atlas/data
"""
import argparse
import sys

from .paths import initialize


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    a = ap.parse_args(argv)
    try:
        p = initialize(a.data)
    except FileExistsError:
        print(f"{a.data} is already an ATLAS data folder. Nothing was changed: you can use it as it is.")
        print("(ATLAS never creates a new folder over an existing one, so a real ledger can't be replaced.)")
        return 0
    print(f"Created ATLAS data folder at {p.root}")
    print("It holds the ledger, limits, locks, audit log and the shadow risk manager's signing key.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
