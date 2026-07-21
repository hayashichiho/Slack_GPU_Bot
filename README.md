# Slack GPU Status Bot

Slackチャンネルに `状況` と投稿すると、Botを動かしているLinuxサーバーのNVIDIA GPU使用状況を返信します。

## 主な機能

- GPU番号、機種名、使用率、メモリ使用量を表示
- GPUを使用しているLinuxユーザー名を表示
- `状況`、`状況！`、`状況？`に反応
- Botへのメンションにも反応
- SlackのSocket Modeを使用するため、サーバーのポート公開は不要
- GPUの予約や割り当ては行わず、状態の可視化だけを行う

表示例：

```text
🖥 GPUサーバー状況　2026-07-21 14:20:44

GPU 0 🔴　NVIDIA RTX A6000
使用率：96%　メモリ：6090 / 49140 MiB
利用者：user1

GPU 1 🟢　NVIDIA RTX A6000
使用率：0%　メモリ：3 / 49140 MiB
利用者：なし
```

## 構成

```text
Slackで「状況」と投稿
        ↓
Socket ModeでBotが受信
        ↓
サーバー上でnvidia-smiを実行
        ↓
結果をSlackへ返信
```

## 前提条件

- Linuxサーバーへログインできる
- `nvidia-smi`が実行できる
- Python 3が利用できる
- サーバーから`https://slack.com`へ通信できる
- Slackワークスペースへカスタムアプリを追加できる

システム全体へソフトウェアをインストールする管理者権限は不要です。Pythonパッケージは自分の仮想環境内に追加します。

## ファイル

| ファイル | 用途 |
| --- | --- |
| `gpu_bot.py` | Bot本体 |
| `requirements.txt` | Python依存パッケージ |
| `slack-manifest.yaml` | 新規Slack App作成用の任意ファイル |
| `.env` | 各自で作成するトークン設定。配布物には含まれない |

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
| 公開チャンネル | `message.channels` |
| Botへのメンション | `app_mention` |
| 非公開チャンネル | `message.groups` |
| BotとのDM | `message.im` |
| グループDM | `message.mpim` |

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

以下の2行を記述します。

```bash
export SLACK_BOT_TOKEN='xoxb-実際のBotトークン'
export SLACK_APP_TOKEN='xapp-実際のAppトークン'
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

## 6. 通常起動で動作確認する

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

## 7. tmuxで継続稼働する

tmuxセッションを作成します。

```bash
tmux new -s gpu-bot
```

tmux内でBotを起動します。

```bash
cd /path/to/slack_gpu_bot
source .venv/bin/activate
set +x
source .env
python gpu_bot.py
```

Botを動かしたままtmuxから抜けるには、`Ctrl+B`を押して離し、その後`D`を押します。この操作をデタッチと呼びます。`Ctrl+C`はBotを終了させるため、デタッチ時には押さないでください。

tmuxの一覧を確認します。

```bash
tmux ls
```

後からBotの画面へ戻ります。

```bash
tmux attach -t gpu-bot
```

Botを終了する場合はtmuxへ戻り、`Ctrl+C`を押します。

サーバー自体が再起動するとtmuxも終了するため、Botを再度起動する必要があります。

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

Botと同じターミナルまたはtmux内で`.env`を読み込みます。

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

### 利用者が「なし」になる

別ユーザーのPID情報を参照できない環境や、`nvidia-smi`のCompute Appsに現れない処理では、利用者を取得できない場合があります。GPU使用率とメモリ量はそのまま表示されます。

### tmuxへ入ると以前の画面が表示される

tmuxは以前の画面を保存しています。`GNU nano`が表示された場合は`Ctrl+X`でnanoを閉じます。Botの起動メッセージが表示されていればBotは動作中です。tmuxから抜ける場合は`Ctrl+B`、`D`の順に操作します。

## 制限事項

- GPUの予約、排他制御、ジョブキュー管理は行いません。
- `nvidia-smi`に表示されない処理は検出できません。
- CPUのみを使う処理は表示しません。
- Botを停止している間はSlackへ回答できません。
