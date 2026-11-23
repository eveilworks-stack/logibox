# LOGIBOX ver.0.67 起動・更新修正版

[修正版ZIPを保存](https://github.com/eveilworks-stack/logibox/raw/refs/heads/downloads/logibox-0.67-fix/downloads/LOGIBOX_UPDATE.zip)

Windowsで保存後、ZIPを右クリック→「すべて展開」→展開先のLOGIBOX_UPDATE内のUPDATE.batを開いてください。旧LOGIBOXフォルダと、OneDrive外のバックアップ先を選択します。

利用者のDB・データは含まれません。更新ツールは既存DB・設定・社印・PDFを置換せず、既存ロックを削除・書き換えません。OneDrive内の旧フォルダは「このデバイス上で常に保持する」にし、同期を一時停止してください。

修正版ソースと詳細手順はZIP内のlogiboxにあります。Windows実機・実OneDrive同期は未検証です。Linuxではunittest 198件成功、4件スキップ、失敗0件。HTTP APIとZIPからの更新・起動、既存データの保持を確認しています。
