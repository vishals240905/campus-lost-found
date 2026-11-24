"""Deploy a tested checkout on the existing private Learner Lab instance."""

import fcntl
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

SOURCE = Path(__file__).resolve().parent
TARGET = Path.home() / 'campus-lost-found'
FILES = ('app.py', 'dynamo_records.py')
DIRECTORIES = ('templates', 'static')
REQUIREMENTS = ('requirements.txt', 'requirements-aws.txt')


def workers(pid):
    path = Path(f'/proc/{pid}/task/{pid}/children')
    return set(path.read_text().split())


def healthy():
    try:
        with urlopen('http://127.0.0.1:8000/', timeout=3) as response:
            return response.status == 200 and b'Campus Lost' in response.read()
    except OSError:
        return False


def reload_and_check(pid):
    previous = workers(pid)
    os.kill(pid, signal.SIGHUP)
    for _ in range(20):
        current = workers(pid)
        if current and not previous.intersection(current) and healthy():
            return
        time.sleep(1)
    raise RuntimeError('Gunicorn did not reload to a healthy worker.')


def install_files(source, temporary):
    for name in FILES:
        staged = temporary / name
        shutil.copy2(source / name, staged)
        staged.replace(TARGET / name)
    for name in DIRECTORIES:
        staged = temporary / name
        shutil.copytree(source / name, staged, dirs_exist_ok=True)
        installed = TARGET / name
        if installed.exists():
            with tempfile.TemporaryDirectory(prefix='previous-', dir=temporary) as prior:
                installed.replace(Path(prior) / name)
                try:
                    staged.replace(installed)
                except OSError:
                    (Path(prior) / name).replace(installed)
                    raise
            continue
        staged.replace(TARGET / name)


def deploy():
    if SOURCE == TARGET:
        raise RuntimeError('Run deployment from the separate GitHub Actions checkout.')
    with (TARGET / '.deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if any((SOURCE / name).read_text(encoding='utf-8') != (TARGET / name).read_text(encoding='utf-8')
               for name in REQUIREMENTS):
            raise RuntimeError('Dependencies changed. Update the EC2 virtual environment before deploying.')
        pid = int((TARGET / 'gunicorn.pid').read_text().strip())
        proc = Path(f'/proc/{pid}')
        command = (proc / 'cmdline').read_bytes().split(b'\0')
        if (proc.stat().st_uid != os.getuid() or (proc / 'cwd').resolve() != TARGET
                or not (b'app:create_app()' in command and any(b'gunicorn' in part for part in command))):
            raise RuntimeError('gunicorn.pid does not identify this app’s Gunicorn process.')
        if not healthy():
            raise RuntimeError('The current site is not healthy. Start Gunicorn before deploying.')

        with tempfile.TemporaryDirectory(prefix='.release-', dir=TARGET) as directory:
            temporary = Path(directory)
            backup = temporary / 'backup'
            backup.mkdir()
            for name in FILES:
                shutil.copy2(TARGET / name, backup / name)
            for name in DIRECTORIES:
                shutil.copytree(TARGET / name, backup / name)
            subprocess.run([str(TARGET / '.venv/bin/gunicorn'), '--check-config', 'app:create_app()'],
                           cwd=SOURCE, env={**os.environ, 'CAMPUS_DATA_DIR': str(temporary / 'smoke-data'),
                                            'CAMPUS_PHOTO_BUCKET': '', 'CAMPUS_ACCOUNTS_TABLE': '',
                                            'CAMPUS_RECORDS_TABLE': ''}, check=True)
            try:
                install_files(SOURCE, temporary)
                reload_and_check(pid)
            except Exception:
                install_files(backup, temporary)
                reload_and_check(pid)
                raise
    print('Deployment verified. The EC2 website responded after Gunicorn reloaded.')


if __name__ == '__main__':
    deploy()
