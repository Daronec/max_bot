"""Compatibility entry point for the assistant API."""
if __name__ == '__main__':
    from assistant_app.operations.runtime import use_project_python
    use_project_python()

from assistant_app.core.server import app, run_server


if __name__ == '__main__':
    run_server()
