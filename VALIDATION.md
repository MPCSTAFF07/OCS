# v0.2 検証記録

2026-10-22（日本時間）

## 自動検証

`python -m pytest -q tests`：7件。

- 管理と視聴の認証分離、公開設定、配信キーのAPI応答への非表示、古い招待URLの拒否。
- FFmpegの実映像・音声をUDPで受信し、2つのWebRTC受信先へ送信。フレームと音声の到着、動く映像の画素更新、切断、停止後再開を確認。
- 入力がない場合のタイムアウト。
- LuaスクリプトのOBS APIスタブによる開始・停止・設定選択。
- 既存Nginx内の対象HTTPSブロックだけへの追記、既存設定の保持、再実行で重複しないこと。
- 公開接続受付のCORS、配信者認証、TURN一時認証のHMAC、招待URL更新、配信PCの切断を確認。
- 公開HTTP → 配信PCへの外向きWebSocket → ローカルAPIの接続受付を検証。実際のWebRTC映像・音声受信と退出まで確認。

JavaScriptの構文確認とPythonのコンパイル確認も実施。
テストでは実行環境のネットワーク機器一覧取得が制限されるため、WebRTCのICE接続候補をループバックに限定しています。
公開経由のテストはローカルに立てた2つのHTTPサーバーを使い、TURN部分は除外しています。TURN認証の生成は別途検証しています。

## まだ実機確認が必要なこと

- Windows版OBSのLua APIの動作と出力生成。
- Windows PowerShellとバッチファイル。
- Chrome / Edge / OBSドック内の画面描画と操作。
- Ubuntuへのパッケージ導入、systemd起動、Nginx組み込み。
- 実際の公開TURNサーバー、AWSファイアウォールと別回線間接続。
- 実測遅延、長時間安定性、本配信と同時運用した際のCPU・回線負荷。

公開サーバーの設置とGitHub Pagesの公開は、このZIPの作成時点では未実施です。
初回は2人で試験接続し、シーン切替・ゲーム音・Discord音声の返り・負荷を確認してください。

## 一次資料

- https://docs.obsproject.com/reference-outputs
- https://github.com/obsproject/obs-studio/blob/master/plugins/obs-ffmpeg/obs-ffmpeg-output.c
- https://aiortc.readthedocs.io/en/latest/helpers.html
- https://github.com/coturn/coturn/blob/master/examples/etc/turnserver.conf
- https://docs.aiohttp.org/en/stable/client_reference.html
- https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site

## AV settings update

Added API validation/persistence/restart, owner-only AV control through the public broker, Lua mixer-bit isolation/restoration/new-source exclusion/conflict shutdown, FPS pacing without duplication, and actual 1920x1080 VP8 WebRTC decoding with 60fps timestamps. Browser controller tests cover audio checkbox edits surviving polling and state-label refreshes. Real Windows OBS and deployed AWS checks remain manual; see AV_SETTINGS.md.
