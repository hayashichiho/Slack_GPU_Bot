# Slack GPU Status Bot

SlackのチャンネルまたはApp Homeから、1台以上のLinuxサーバーに搭載されたNVIDIA GPUの使用状況を確認するBotです。

## 主な機能

- GPU番号、機種名、使用率、メモリ使用量を表示
- GPUを使用しているLinuxユーザー名を表示
- ローカルサーバーに加えて、SSH接続できる複数のGPUサーバーを一覧表示
- `状況`、`状況！`、`状況？`に反応
- Botへのメンションにも反応
- App Homeの「GPU状況を確認」ボタンから確認可能
- SlackのSocket Modeを使用するため、サーバーのポート公開は不要
- GPUの予約や割り当ては行わず、状態の可視化だけを行う

表示例：

```text
🖥 GPUサーバー状況　2026-07-21 14:20:44

―― archimedes ――

GPU 0 🔴　NVIDIA RTX A6000
使用率：96%　メモリ：6090 / 49140 MiB
利用者：user1

GPU 1 🟢　NVIDIA RTX A6000
使用率：0%　メモリ：3 / 49140 MiB
利用者：なし
```

## 構成

```text
Slackで「状況」と投稿 / App Homeのボタンを押す
                    ↓
             Socket Modeで受信
                    ↓
       ローカルまたはSSH経由で接続
                    ↓
          nvidia-smiとpsを実行
                    ↓
          GPU状況をSlackへ表示
```

## 前提条件

- Linuxサーバーへログインできる
- `nvidia-smi`が実行できる
- Python 3が利用できる
- リモートサーバーも表示する場合は、Bot実行ユーザーが公開鍵認証でSSH接続できる
- サーバーから`https://slack.com`へ通信できる
- Slackワークスペースへカスタムアプリを追加できる

システム全体へソフトウェアをインストールする管理者権限は不要です。Pythonパッケージは自分の仮想環境内に追加します。

## ファイル

| ファイル | 用途 |
| --- | --- |
| `gpu_bot.py` | Bot本体 |
| `requirements.txt` | Python依存パッケージ |
| `slack-manifest.yaml` | 新規Slack App作成用の任意ファイル |
| `.env.example` | 環境変数の設定例 |
| `.env` | 各自で作成する実際の設定。Git管理対象外 |

## 1. Slack Appを用意する

### 方法A：専用Appを新しく作る（推奨）

1. <https://api.slack.com/apps>を開きます。
2. `Create New App`を選択します。
3. `From scratch`を選び、App名とワークスペースを指定します。
4. 以降の「Slack Appを手動設定する」に進みます。

付属の`slack-manifest.yaml`を使って、`From an app manifest`から一括設定することもできます。YAMLは新規作成を簡単にするための任意ファイルであり、Python Botの実行には使用しません。

### 方法B：既存Appへ追加する

既存AppにもGPU機能を追加できます。ただし、先に既存機能への影響を確認してください。

- Incoming WebhookでGASなどから投稿するだけの場合は、通常は併用できます。
- 既存AppがEvents API、スラッシュコマンド、インタラクションを外部URLで受信している場合、Socket Modeへの変更が既存機能に影響する可能性があります。
- 不明な場合は専用Appを新しく作成してください。
- 既存AppのManifest全体を貼り替えると、既存の権限やWebhook設定を失う可能性があります。既存Appでは以下の項目を管理画面から手動で追加してください。

## 2. Slack Appを手動設定する

### Socket Mode

1. App管理画面の`Socket Mode`を開きます。
2. `Enable Socket Mode`を有効にします。
3. App-Level Tokenを生成します。
4. Token Nameには任意の名前（例：`socket`）を指定します。
5. Scopeに`connections:write`を追加します。
6. 生成された`xapp-`から始まるトークンを安全な場所に控えます。

### Bot Token Scopes

`OAuth & Permissions` → `Bot Token Scopes`へ、使用場所に応じて以下を追加します。

| 用途 | Scope |
| --- | --- |
| Slackへ返信 | `chat:write` |
| 公開チャンネルのメッセージを受信 | `channels:history` |
| Botへのメンションを受信 | `app_mentions:read` |
| 非公開チャンネルでも使用 | `groups:history` |
| BotとのDMでも使用 | `im:history` |
| グループDMでも使用 | `mpim:history` |

公開チャンネルだけで利用する場合、最低限必要なのは`chat:write`と`channels:history`です。

### Event Subscriptions

`Event Subscriptions`を有効にし、`Subscribe to bot events`へ追加します。

| 用途 | Bot Event |
| --- | --- |
| App Home | `app_home_opened` |
| 公開チャンネル | `message.channels` |
| Botへのメンション | `app_mention` |
| 非公開チャンネル | `message.groups` |
| BotとのDM | `message.im` |
| グループDM | `message.mpim` |

### App HomeとInteractivity

1. `App Home`を開き、`Home Tab`を有効にします。
2. `Interactivity & Shortcuts`を開き、Interactivityを有効にします。

Socket Modeを使用するため、Request URLの公開は不要です。付属の`slack-manifest.yaml`から新規作成した場合、これらは設定済みです。

### ワークスペースへインストール

`OAuth & Permissions`から`Install to Workspace`を実行します。既にインストール済みのAppへ権限を追加した場合は、`Reinstall to Workspace`を実行します。

既存AppでIncoming Webhookを使用しており、再インストール時に「Webhook用のチャンネル」を求められた場合は、既存Webhookが投稿しているチャンネルを選びます。再インストール後は既存機能も動作確認してください。

インストール後、`OAuth & Permissions`に表示される`xoxb-`から始まるBot User OAuth Tokenを安全な場所に控えます。

## 3. Botをチャンネルへ追加する

利用するチャンネルで、Botをメンション候補から選択して招待します。

```text
/invite @Bot名
```

権限がなく招待できない場合は、チャンネル管理者に追加を依頼します。Botは参加しているチャンネルのメッセージだけを確認します。

別のチャンネルでも使用する場合は、そのチャンネルにもBotを追加します。コードやトークンの変更、Botの再起動は不要です。

## 4. サーバーへ配置する

ファイル一式をサーバーの任意のディレクトリへ配置し、そのディレクトリへ移動します。

```bash
cd /path/to/slack_gpu_bot
```

Bot専用の仮想環境を作成します。

```bash
python3 -m venv .venv
```

仮想環境を有効化します。

```bash
source .venv/bin/activate
```

必要なパッケージを仮想環境内に追加します。

```bash
python -m pip install -r requirements.txt
```

`Could not find an activated virtualenv`と表示された場合は、`.venv`が有効になっているか確認してから再実行します。

## 5. トークンを設定する

Botのディレクトリで`.env`を作成します。

```bash
nano .env
```

最低限、以下の2行を記述します。

```bash
export SLACK_BOT_TOKEN='xoxb-実際のBotトークン'
export SLACK_APP_TOKEN='xapp-実際のAppトークン'
```

表示名を指定する場合は、次の行も追加します。省略時はホスト名が使われます。

```bash
export SERVER_NAME='archimedes'
```

`nano`では`Ctrl+O`、`Enter`、`Ctrl+X`の順に押すと保存して終了できます。

共有サーバー上で他の利用者から読まれないようにします。

```bash
chmod 600 .env
```

確認します。

```bash
stat -c '%a %n' .env
```

次の表示であれば適切です。

```text
600 .env
```

`.env`はGitへ登録しないでください。Gitを使用する場合は`.gitignore`へ追加します。

```text
.env
```

## 6. リモートGPUサーバーを追加する（任意）

Botを動かすサーバー以外も表示する場合、パスフレーズ入力なしで利用できるBot専用のSSH鍵を用意し、対象サーバーへ公開鍵を登録します。BotはSSH先で`nvidia-smi`と`ps`だけを実行します。

`.env`へ、`表示名=SSH接続先`の形式で追加します。複数台はカンマで区切ります。

```bash
export REMOTE_GPU_SERVERS='newton=hayashi@newton,gauss=hayashi@gauss'
export GPU_SSH_KEY='~/.ssh/gpu_bot_ed25519'
```

`GPU_SSH_KEY`を省略した場合は`~/.ssh/gpu_bot_ed25519`を使用します。起動前に非対話モードで接続できることを確認してください。

```bash
ssh -i ~/.ssh/gpu_bot_ed25519 -o BatchMode=yes hayashi@newton nvidia-smi
```

リモートサーバーを使わない場合、`REMOTE_GPU_SERVERS`と`GPU_SSH_KEY`は不要です。

## 7. 通常起動で動作確認する

```bash
source .venv/bin/activate
```

```bash
set +x
source .env
```

```bash
python gpu_bot.py
```

次の表示が出ればSlackとの接続待機中です。

```text
GPU Botを起動します。終了するには Ctrl+C を押してください。
```

Slackの対象チャンネルで次のように投稿します。

```text
状況
```

GPU情報が返信されれば成功です。動作確認後、`Ctrl+C`で一度終了します。

App Homeを有効にした場合は、Slackの`Apps`からBotを開き、「GPU状況を確認」ボタンでも確認できます。

## 8. cronで再起動後に自動起動する

管理者権限がなくsystemdサービスを登録できない場合は、ユーザーのcrontabへ`@reboot`ジョブを登録できます。cronから起動するBotはSSHのログインセッションから独立して動作します。

crontabを開きます。

```bash
crontab -e
```

初回にエディタの選択画面が表示された場合は、`1`または`Enter`でnanoを選択します。末尾へ次の1行を追加し、`/path/to/slack_gpu_bot`は実際の絶対パスへ置き換えます。

```cron
@reboot sleep 30 && /bin/bash -lc 'cd /path/to/slack_gpu_bot && source .env && export PYTHONUNBUFFERED=1 && exec .venv/bin/python gpu_bot.py' >> /path/to/slack_gpu_bot/gpu-bot.log 2>&1
```

nanoでは`Ctrl+O`、`Enter`、`Ctrl+X`の順に押して保存します。登録内容を確認します。

```bash
crontab -l
```

この設定は、サーバー起動から30秒待ってネットワークの準備時間を確保し、`.env`を読み込んでBotを起動します。標準出力とエラーは`gpu-bot.log`へ保存されます。SSH接続を切ってもcrontabの設定と、cronが起動したBotは維持されます。

### 再起動せずに設定を確認する

`@reboot`の発火自体は再起動なしでは確認できませんが、cronと同等の最小環境でパス、環境変数、Pythonモジュールを確認できます。

```bash
env -i HOME="$HOME" PATH=/usr/bin:/bin \
  /bin/bash -lc '
    cd /path/to/slack_gpu_bot &&
    source .env &&
    test -n "$SLACK_BOT_TOKEN" &&
    test -n "$SLACK_APP_TOKEN" &&
    test -x .venv/bin/python &&
    .venv/bin/python -c "import gpu_bot; print(\"cron environment OK\")"
  '
```

`cron environment OK`と表示されれば、起動コマンドに必要な環境は読み込めています。

### 今すぐnohupで起動する

`@reboot`は登録直後には実行されません。現在手動で動かしているBotを停止した後、現在分だけ`nohup`で起動すると、次回再起動までSSH接続を切っても動作を維持できます。

最初に既存のBotを`Ctrl+C`で停止し、二重起動していないことを確認します。

```bash
pgrep -af gpu_bot.py
```

何も表示されなければ起動します。

```bash
nohup /bin/bash -lc \
  'cd /path/to/slack_gpu_bot && source .env && export PYTHONUNBUFFERED=1 && exec .venv/bin/python gpu_bot.py' \
  >> /path/to/slack_gpu_bot/gpu-bot.log 2>&1 < /dev/null &
```

起動状態とログを確認します。

```bash
pgrep -af gpu_bot.py
tail -n 100 /path/to/slack_gpu_bot/gpu-bot.log
```

現在から次回再起動までは`nohup`で起動したBotが動き、次回再起動後はcronが起動します。再起動後に手動コマンドから重ねて起動しないでください。

cronの`@reboot`はサーバー起動時に1回実行する設定です。起動後にBotだけが異常終了しても自動再起動しません。

## GPU表示の見方

| 表示 | 意味 |
| --- | --- |
| 🟢 | 使用率が低く、GPUメモリもほぼ空いている |
| 🟡 | 使用率は低いが、1 GiB以上のGPUメモリが使われている |
| 🔴 | プロセスが存在するか、GPU使用率が10%以上 |

例えば、使用率`0%`、メモリ`3 / 49140 MiB`、利用者`なし`であれば、通常は空いていると判断できます。ただし、本Botには予約機能がないため、実行までに別の利用者が使い始める可能性があります。

GPU番号1を指定してプログラムを起動する例：

```bash
CUDA_VISIBLE_DEVICES=1 python train.py
```

この場合、プログラム内では物理GPU 1が`cuda:0`として見えます。

## セキュリティ上の注意

- `xoxb-`と`xapp-`トークンはパスワードと同様に扱います。
- トークンをGitHub、Slack、メール、画面共有へ載せないでください。
- `.env`の権限は`600`にしてください。
- トークンが第三者に見えた可能性がある場合は、Slack App管理画面で再発行します。
- BotはLinuxユーザー名をSlackへ表示します。公開範囲について組織内で確認してください。
- プロセスのコマンドライン、ファイル名、引数はSlackへ送信しません。

## トラブルシューティング

### `ModuleNotFoundError: No module named 'slack_bolt'`

仮想環境を有効化してから依存パッケージを導入します。

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### `環境変数 ... が設定されていません`

手動起動ではBotと同じターミナルで`.env`を読み込みます。nohupとcronでは起動行に`source .env`が含まれているか確認します。

```bash
source .env
```

### `GNU nano`の画面から戻れない

`Ctrl+X`を押します。保存確認が出た場合、保存するなら`Y`と`Enter`、保存しないなら`N`を押します。

### Botが`状況`に反応しない

- Botを対象チャンネルへ追加したか確認します。
- `gpu_bot.py`が起動しているか確認します。
- Socket Modeが有効か確認します。
- `xapp-`トークンに`connections:write`があるか確認します。
- 公開チャンネルでは`channels:history`と`message.channels`を確認します。
- 非公開チャンネルでは`groups:history`と`message.groups`を確認します。
- 権限変更後に`Reinstall to Workspace`を行ったか確認します。

### App Homeにボタンが表示されない

- `App Home`で`Home Tab`が有効か確認します。
- `Event Subscriptions`に`app_home_opened`があるか確認します。
- `Interactivity & Shortcuts`が有効か確認します。
- Manifestや権限を変更した後は、Appを再インストールしてBotを再起動します。

### リモートサーバーのGPU情報を取得できない

- `.env`の`REMOTE_GPU_SERVERS`が`表示名=ユーザー@ホスト`形式か確認します。
- `GPU_SSH_KEY`のパスとファイル権限を確認します。
- Bot実行ユーザーで`ssh -o BatchMode=yes 接続先 nvidia-smi`が成功するか確認します。
- 初回接続時のホスト鍵確認が残っている場合は、事前に手動接続して確認します。

### 利用者が「なし」になる

別ユーザーのPID情報を参照できない環境や、`nvidia-smi`のCompute Appsに現れない処理では、利用者を取得できない場合があります。GPU使用率とメモリ量はそのまま表示されます。

### 再起動後にcronからBotが起動しない

- `crontab -l`に`@reboot`行が登録されているか確認します。
- crontab内のBotディレクトリが絶対パスになっているか確認します。
- `.env`と`.venv/bin/python`が存在するか確認します。
- `pgrep -af gpu_bot.py`でプロセスを確認します。
- `gpu-bot.log`に出力されたエラーを確認します。

## 制限事項

- GPUの予約、排他制御、ジョブキュー管理は行いません。
- `nvidia-smi`に表示されない処理は検出できません。
- CPUのみを使う処理は表示しません。
- SSH接続に失敗したサーバーは、GPU情報を取得できなかった旨だけを表示します。
- cronの`@reboot`だけでは、Botが異常終了した場合の自動再起動は行いません。
- Botを停止している間はSlackへ回答できません。
