"""Use the project's modern TLS runtime when launching from the system Python."""
import os
from pathlib import Path
import sys

from assistant_app.paths import PROJECT_ROOT


def use_project_python():
    environment = PROJECT_ROOT / '.venv'
    interpreter = environment / 'bin' / 'python'
    if interpreter.is_file() and Path(sys.prefix).resolve() != environment.resolve():
        os.execv(str(interpreter), [str(interpreter), *sys.argv])
