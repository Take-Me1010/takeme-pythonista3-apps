# Pythonista 3 で踏んだバグと知見

## サムネイル生成で `ui` を使うとクラッシュする（タプルの二重解放）

- 発生: `file-browser/main.py`、Pythonista 3.4 / iOS 27.2.1
- 症状: 画像が多いフォルダでファイルを何度も開いていると、トレースバックなしでアプリが落ちる

### クラッシュレポート（.ips）の特徴

```
"type"    : "EXC_BAD_ACCESS", "signal" : "SIGBUS"
"message" : "Thread stack size exceeded due to excessive recursion"
```

- 落ちるのはメインスレッドではなく、`threading.Thread` で起動したスレッド（`thread_run` → `pythread_wrapper`）
- スタックの一番上は `tuplehash` が延々と自分を呼び続け、その下は `dict_ass_sub`
  - スタックを 544KB → 8MB に広げても、再帰が 8,000 段 → 131,000 段に伸びるだけで落ちる
  - つまり再帰が深いのではなく無限ループ。`threading.stack_size` では直らない
- 該当行はサムネイルワーカーの `_thumb_cache[key] = thumb`（`key = (path, mtime)`）

### 原因

CPython は要素数の小さいタプルを解放するとき、捨てずにフリーリストへつなぐ。
このとき `ob_item[0]` を「次の空きタプル」へのリンクとして使う。

どこかの C 拡張が**要素2個のタプルを二重に解放**すると、次のようになる。

1. 1回目の解放で A がリストの先頭に入る
2. 2回目の解放で `A.ob_item[0] = A` となり、A が自分自身を指す
3. 以降、要素2個のタプルを作るたびに同じ A が返される

`get_thumb` では、ちょうど次の2行で要素2個のタプルを続けて作っていた。

```python
key = (path, mtime)            # A が返る
_thumb_queue.put((key, tv))    # また A が返る → A = (A, tv)
```

その結果 `key` が自分自身を含むタプルになる。ワーカー側では

- `make_thumb(key[0])` が TypeError になり、`except` に吸収されて静かに終わる
- 続く `_thumb_cache[key]` で `hash(key)` が無限再帰になり、スタックガードに当たって落ちる

純粋な Python でタプルが二重解放されることはないので、壊しているのはネイティブコード。
サムネイル生成を止めると安定し、サムネイルで使っていた `ui` の処理
（`ui.Image.from_data`、`img.size`、`ui.ImageContext`、`ui.Path`、`img.draw`、
`cell.image_view.image = ...`）を外すと再発しなくなった。
どの関数が犯人かまでは特定していない。

なお、`ui` の処理をメインスレッドへ移す（`@on_main_thread`）だけでは直らなかった。
スレッド安全性の問題ではなく、`ui` 側の参照カウントのバグと考えられる。

### 対処

画像の読み込み・縮小・表示を objc_util で UIKit を直接呼んで行い、`ui.Image` を使わない。

- `UIImage.imageWithContentsOfFile_` で読み込む
- `UIGraphicsBeginImageContextWithOptions` と `drawInRect_` で縮小・角丸
- 結果の UIImage を `ObjCInstance(cell.image_view).setImage_(...)` でセルに直接設定する

### 教訓

- `ui` で大量に画像を扱うとメモリが壊れることがある。画像処理は objc_util で UIKit を直接使う方が安全
- .ips で `tuplehash` などの `*hash` 関数が無限再帰していたら、オブジェクトの二重解放（use-after-free）を疑う
- トレースバックなしで落ちたら、「設定 → プライバシーとセキュリティ → 解析と改善 → 解析データ」から
  `Pythonista3-*.ips` を探す
  - `"triggered": true` が付いたスレッドの `frames` を見る
  - `imageIndex` は `usedImages` の何番目かを指す
  - メモリ不足で落ちた場合は `.ips` ではなく `JetsamEvent-*.ips` が出る
