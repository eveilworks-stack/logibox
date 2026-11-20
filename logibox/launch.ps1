param([switch]$ShortcutOnly)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
try {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'LOGIBOX.lnk'))
    $link.TargetPath = Join-Path $env:SystemRoot 'System32/wscript.exe'
    $link.Arguments = '"' + (Join-Path $PSScriptRoot '起動.vbs') + '"'
    $link.WorkingDirectory = $PSScriptRoot
    $link.IconLocation = (Join-Path $PSScriptRoot 'logi-tomos.ico') + ',0'
    $link.Save()
    if ($ShortcutOnly) {
        [System.Windows.Forms.MessageBox]::Show('デスクトップに起動アイコンを作成しました。','LOGIBOX') | Out-Null
        exit
    }
    Set-Location -LiteralPath $PSScriptRoot
    $env:PYTHONIOENCODING = 'utf-8'
    $runtime = $null
    foreach ($candidate in @('py','python')) {
        if (!(Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
        $found = & $candidate -c "import sys; print(sys.executable if sys.version_info >= (3,10) else '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $found -and (Test-Path -LiteralPath "$found")) { $runtime = "$found"; break }
    }
    if (!$runtime) { throw 'Python 3.10以降をインストールしてから、もう一度アイコンを開いてください。' }
    & $runtime -c "import pymupdf, reportlab, PIL" 2>$null
    if ($LASTEXITCODE -ne 0) {
        [System.Windows.Forms.MessageBox]::Show('初回の準備を行います。インターネットに接続した状態でお待ちください。','LOGIBOX') | Out-Null
        & $runtime -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt') *> (Join-Path $PSScriptRoot 'setup.log')
        if ($LASTEXITCODE -ne 0) { throw '初回準備が完了しませんでした。setup.logを確認してください。' }
    }
    $pythonw = Join-Path (Split-Path $runtime) 'pythonw.exe'
    if (!(Test-Path -LiteralPath $pythonw)) { $pythonw = $runtime }
    Start-Process -FilePath $pythonw -ArgumentList ('"' + (Join-Path $PSScriptRoot 'launcher.py') + '"') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
} catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message,'LOGIBOX',0,16) | Out-Null
    exit 1
}

