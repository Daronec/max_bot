"""Entry point for the standalone assistant worker."""
if __name__ == '__main__':
    from assistant_app.operations.runtime import use_project_python
    use_project_python()

from assistant_app.operations.worker import main


if __name__ == '__main__':
    main()
