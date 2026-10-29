#!/usr/bin/env python3
"""Compatibility entry point for the shared MAX task server."""
if __name__ == '__main__':
    from runtime import use_project_python
    use_project_python()

from server import app, run_server

main = run_server

if __name__ == '__main__':
    main()
