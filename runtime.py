"""Use the project's modern TLS runtime when launching from the system Python."""
import os
from pathlib import Path
import sys


def use_project_python():
    environment = Path(__file__).resolve().parent / '.venv'
    interpreter = environment / 'bin' / 'python'
    if interpreter.is_file() and Path(sys.prefix).resolve() != environment.resolve():
        os.execv(str(interpreter), [str(interpreter), *sys.argv])
