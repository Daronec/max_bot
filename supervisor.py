"""Stable entry point for the installed macOS LaunchAgent and the local launcher."""
if __name__ == '__main__':
    from assistant_app.operations.runtime import use_project_python
    use_project_python()

from assistant_app.operations.supervisor import main


if __name__ == '__main__':
    main()
