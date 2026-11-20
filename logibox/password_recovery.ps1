param()
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
try {
    Set-Location -LiteralPath $PSScriptRoot
    $runtime = $null
    foreach ($candidate in @('py', 'python')) {
        if (!(Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
        $found = & $candidate -c "import sys; print(sys.executable if sys.version_info >= (3,10) else '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $found -and (Test-Path -LiteralPath "$found")) { $runtime = "$found"; break }
    }
    if (!$runtime) { throw 'LOGIBOXで使用しているPython 3.10以降が必要です。Codexは不要です。Pythonを利用できるWindowsアカウントで実行してください。' }
    & $runtime -c "import tkinter, sqlite3" 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Pythonのtkinterとsqlite3が必要です。Python公式インストーラーの標準構成をインストールしてください。' }
    $data = Join-Path $PSScriptRoot 'data'
    if ($env:APP_DATA) { $data = [IO.Path]::GetFullPath($env:APP_DATA) }
    if (!(Test-Path -LiteralPath (Join-Path $data 'app.db') -PathType Leaf)) {
        throw '対象のdata/app.dbがありません。現在使用中のLOGIBOXフォルダへ修正ファイルを配置してください。空のDBの作成は行いません。'
    }
    # Resolve Python + APP_DATA before elevation: another administrator may sign in at UAC.
    $pythonw = Join-Path (Split-Path $runtime) 'pythonw.exe'
    if (!(Test-Path -LiteralPath $pythonw)) { $pythonw = $runtime }
    $arguments = '"' + (Join-Path $PSScriptRoot 'password_recovery.py') + '" --data "' + $data + '"'
    Start-Process -FilePath $pythonw -ArgumentList $arguments -WorkingDirectory $PSScriptRoot -Verb RunAs -WindowStyle Hidden -Wait
} catch {
    [System.Windows.Forms.MessageBox]::Show(('再設定を開始しませんでした。データは変更していません。' + [Environment]::NewLine + $_.Exception.Message), 'LOGIBOX パスワード再設定', 0, 16) | Out-Null
    exit 1
}
