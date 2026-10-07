import sys

from pixiv_bridge.deps import ensure_dependencies

try:
    ensure_dependencies()
except RuntimeError as exc:
    print(f"[deps] {exc}", file=sys.stderr)
    sys.exit(1)

from pixiv_bridge.cli import main

if __name__ == "__main__":
    main()
