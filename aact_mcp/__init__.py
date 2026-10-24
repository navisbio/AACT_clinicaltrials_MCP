import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


def main():
    """Package entry point — defers server import to avoid side effects."""
    from .server import main as _main
    _main()


if __name__ == "__main__":
    sys.exit(main())
