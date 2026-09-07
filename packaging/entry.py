"""GUI entry point and opt-in packaged-runtime verification."""
import sys


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--self-test':
        from package_smoke import run
        sys.exit(run(sys.argv[2]))
    from app import App
    App().mainloop()
