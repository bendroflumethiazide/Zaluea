"""Create a new ATLAS data folder (refuses to touch an existing one).

    python -m atlas.init_data --data C:/Users/you/.atlas/data
"""
import argparse

from .paths import initialize


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    a = ap.parse_args(argv)
    p = initialize(a.data)
    print(f"Created ATLAS data folder at {p.root}")
    print("It holds the ledger, limits, locks, audit log and the shadow risk manager's signing key.")


if __name__ == "__main__":
    main()
