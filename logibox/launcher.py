"""Launch the local application without a console window."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:8766/'


def ready():
    try:
        with urllib.request.urlopen(URL + 'api/state', timeout=1) as response:
            state = json.load(response)
        with urllib.request.urlopen(URL, timeout=1) as response:
            html = response.read(4096).decode('utf-8-sig')
        return isinstance(state.get('setup'), bool) and any(title in html for title in ('<title>LOGIBOX</title>', '<title>請求書管理アプリ</title>'))
    except (OSError, ValueError):
        return False


def main():
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, 'Local\\SeikyuAppLauncher8766')
    if not mutex:
        raise OSError('起動ロックを作成できませんでした。')
    try:
        if ctypes.windll.kernel32.WaitForSingleObject(mutex, 60000) not in (0, 128):
            raise RuntimeError('起動の準備中です。少し待ってから再度開いてください。')
        if ready():
            webbrowser.open(URL)
            return
        data = Path(os.environ.get('APP_DATA') or ROOT / 'data')
        data.mkdir(parents=True, exist_ok=True)
        with (data / 'launcher.log').open('ab') as log:
            process = subprocess.Popen(
                [sys.executable, str(ROOT / 'server.py'), '--no-browser'],
                cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(120):
            if process.poll() is not None:
                raise RuntimeError('起動できませんでした。data/launcher.logを確認してください。別の版が起動中の場合は先に終了してください。')
            if ready():
                webbrowser.open(URL)
                return
            time.sleep(.5)
        process.terminate()
        raise RuntimeError('起動の応答がありません。data/launcher.logを確認してください。')
    finally:
        ctypes.windll.kernel32.ReleaseMutex(mutex)
        ctypes.windll.kernel32.CloseHandle(mutex)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        ctypes.windll.user32.MessageBoxW(None, str(error), 'LOGIBOX', 16)
