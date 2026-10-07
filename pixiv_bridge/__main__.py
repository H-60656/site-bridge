import sys

from .deps import ensure_dependencies

try:
    ensure_dependencies()
except RuntimeError as exc:
    print(f"[deps] {exc}", file=sys.stderr)
    sys.exit(1)

from .cli import main

if __name__ == "__main__":
    main()
