"""`python -m dasungctl` entry point: dispatch to the CLI."""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())

