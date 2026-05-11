"""Support `python -m jack` as an alternative to the installed `jack` script."""
from jack.main import main

if __name__ == "__main__":
    raise SystemExit(main())
