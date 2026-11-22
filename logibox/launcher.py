"""Launch the local application without a console window."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import urllib.error
from app_version import VERSION, runtime_identity
import webbrowser

ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:8766/'


def ready():
    """Only reuse the identical release, installation and data directory."""
    try:
        with urllib.request.urlopen(URL + 'api/runtime', timeout=1) as response:
            identity = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError('ポート8766で旧版または別アプリが起動中です。LOGIBOXを終了するかPCを再起動してから、このフォルダの起動.vbsを開いてください。') from error
    except urllib.error.URLError as error:
        if not isinstance(error.reason, ConnectionRefusedError):
            raise RuntimeError('起動中アプリの確認に失敗しました。PCを再起動してから起動してください。') from error
        return False
    except (OSError, ValueError) as error:
        raise RuntimeError('起動中アプリを確認できません。PCを再起動してから起動してください。') from error
    if not isinstance(identity, dict):
        raise RuntimeError('別アプリがポート8766を使用中です。PCを再起動してください。')
    expected = runtime_identity()
    if identity != expected:
        raise RuntimeError(
            '別の版・フォルダ・データのアプリが起動中です。\n'
            f'起動予定: ver.{VERSION} / {ROOT}\n'
            f'稼働中: ver.{identity.get("version", "不明")}\n'
            '全体バックアップを取得し、LOGIBOXを終了するかPCを再起動してください。'
            'その後、このフォルダの起動.vbsを開いてください。旧プロセスは自動終了しません。')
    return True


def browser_url():
    return URL + '?v=' + VERSION + '&build=' + runtime_identity()['build']


def main():
    kernel = ctypes.windll.kernel32
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    mutex = kernel.CreateMutexW(None, False, 'Local\\SeikyuAppLauncher8766')
    if not mutex:
        raise OSError('起動ロックを作成できませんでした。')
    acquired = False
    try:
        if ctypes.windll.kernel32.WaitForSingleObject(mutex, 60000) not in (0, 128):
            raise RuntimeError('起動の準備中です。少し待ってから再度開いてください。')
        acquired = True
        if ready():
            webbrowser.open(browser_url())
            return
        data = Path(os.environ.get('APP_DATA') or ROOT / 'data')
        if (ROOT / 'UPDATE_ONLY.txt').exists() and not (data / 'app.db').is_file():
            raise RuntimeError('更新専用パッケージです。update.batで利用中のlogiboxフォルダを更新してください。新しいDBは作成しません。')
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
                webbrowser.open(browser_url())
                return
            time.sleep(.5)
        process.terminate()
        raise RuntimeError('起動の応答がありません。data/launcher.logを確認してください。')
    finally:
        if acquired:
            ctypes.windll.kernel32.ReleaseMutex(mutex)
        ctypes.windll.kernel32.CloseHandle(mutex)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        ctypes.windll.user32.MessageBoxW(None, str(error), 'LOGIBOX', 16)
