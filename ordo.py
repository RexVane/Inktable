"""Cross-platform Python entry point. Node is needed only for frontend tests."""
import os
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parent
    venv = root / 'serverpy' / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if venv.exists() and Path(sys.executable).resolve() != venv.resolve():
        return subprocess.call([str(venv), str(Path(__file__).resolve()), *sys.argv[1:]], cwd=root)
    sys.path.insert(0, str(root / 'serverpy'))
    command = sys.argv[1] if len(sys.argv) > 1 else 'serve'
    if command == 'serve':
        from main import main as serve
        serve()
        return 0
    if command == 'seed':
        import asyncio
        from ordo.seed import seed
        asyncio.run(seed())
        return 0
    if command == 'test':
        return subprocess.call([sys.executable, '-m', 'pytest', str(root / 'serverpy/tests'), *sys.argv[2:]], cwd=root / 'serverpy')
    if command == 'check':
        import ast
        for file in (root / 'serverpy').rglob('*.py'):
            if '.venv' not in file.parts:
                ast.parse(file.read_text('utf-8'), filename=str(file))
        from ordo.app import create_app  # imports and dependency validation without opening live data
        print('Python syntax and backend imports passed')
        return 0
    raise SystemExit('Usage: python ordo.py [serve|seed|test|check]')


if __name__ == '__main__':
    raise SystemExit(main())
