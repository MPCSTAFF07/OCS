# GitHub Pagesの管理画面

管理画面： https://mpcstaff07.github.io/OCS/manage.html

配信PCの127.0.0.1へ直接アクセスせず、HTTPSの公開サーバーと、配信PCから接続済みのWebSocketを通して操作します。
配信PCでOCSとOBSを起動しておく必要があります。
視聴ページとは別の管理キーで認証します。配信キー・視聴招待URL・localhostの管理URLは、この画面の管理キーには使えません。
管理キーはページのJavaScript変数内だけで保持し、ブラウザ保存領域やURLには保存しません。再読み込み時は再入力します。

## 稼働中のAWSを更新

接続を一時的に切断するので、本番配信中を避けて行ってください。

```bash
cd ~/OCS
git pull --ff-only
sudo python3 deploy/update-management.py --pages-url https://mpcstaff07.github.io/OCS/
```

更新スクリプトは管理キーを追加し、接続受付のサービスだけを更新します。
既存のpublisher_key・TURNの設定・Nginxの設定は維持します。

管理キーを自分の画面で確認：

```bash
sudo python3 -c 'import json; print(json.load(open("/etc/obs-caster-share/config.json"))["admin_key"])'
```

管理キーはGitHubやチャットへ貼り付けず、管理画面のパスワード欄へ入力してください。

## 配信PCの更新

OCSをCtrl+Cで停止してから、現在使用中のOCSフォルダでpublisher.pyを最新版へ更新し、OCSを一度だけ起動します。
Pythonの認証局設定・Luaの接続設定は維持されます。

## 管理画面を開く

1. https://mpcstaff07.github.io/OCS/manage.html を開く。
2. 中継URLに `https://mpc-official.duckdns.org/caster` を入力。
3. AWSのadmin_keyを管理キー欄へ入力し、「接続」。
4. 共有開始・停止、招待URLコピー・更新、接続者確認・切断ができます。

GitHub Pagesが未設定なら Settings → Pages → main /docs を公開元に設定してください。
画面が404の場合はPagesのデプロイ完了を確認してください。

接続先URLや配信キーの変更は、従来どおり配信PCのローカル管理画面から行います。
公開管理画面には接続設定を変更する権限を持たせていません。

## 確認したこと

- ローカルで公開HTTP・配信PCへの外向きWebSocket・ローカル管理APIを通し、状態取得・開始・停止・URL更新・切断操作を検証。
- 視聴用トークンや配信キーでは公開管理APIに入れないことを確認。
- 不正オリジンと公開画面からの接続設定変更を拒否することを確認。
- 古い配信PCソフトの場合は更新が必要と返すことを確認。
- 管理画面のJavaScriptは模擬DOMで、ログイン、操作、認証失敗、配信PCの未接続、処理中のログアウトを検証。
- 実ブラウザでの描画、AWSでの更新と実際の遠隔操作は、導入後に確認が必要です。
