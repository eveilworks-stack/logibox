"""Release identity shared by server, launcher and distribution builder."""
import hashlib
import os
from pathlib import Path

VERSION = '0.7.4'
DISPLAY_VERSION = '0.74'
SCHEMA_VERSION = 14
ROOT = Path(__file__).resolve().parent


def build_id(root=ROOT):
    digest = hashlib.sha256()
    paths = [p for p in root.iterdir() if p.is_file() and p.suffix in ('.py', '.ps1', '.vbs', '.bat', '.command', '.sql')]
    paths.extend(p for p in (root / 'static').rglob('*') if p.is_file())
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def path_id(path):
    return hashlib.sha256(os.path.normcase(str(Path(path).resolve())).encode()).hexdigest()


def runtime_identity(root=ROOT, data=None):
    data = data or os.environ.get('APP_DATA') or root / 'data'
    return dict(app='LOGIBOX', version=VERSION, build=build_id(root),
                source_id=path_id(root), data_id=path_id(data), schema=SCHEMA_VERSION)
