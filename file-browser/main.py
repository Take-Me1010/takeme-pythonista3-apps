# -*- coding: utf-8 -*-
# Pythonista 3 用 ファイルブラウザ
# ・起動時は Pythonista の Documents フォルダを表示
# ・左上「フォルダを開く」で「ファイル」アプリのピッカーを開き、
#   「このiPhone内」などの任意のフォルダを選んで中身を閲覧できます
# ・フォルダをタップで中へ移動、ファイルをタップで即プレビュー
# ・画像はプレビュー中に左右スワイプで同じフォルダの画像を行き来できます
# ・音声ファイルをタップすると、同じフォルダの音声をまとめたプレイヤーが開き、
#   ループ再生（なし / 全曲 / 1曲）・シャッフル再生ができます
#   プレイヤーは下から出るシート（半分 / 全画面、下スワイプで閉じる）で、
#   閉じても画面下のミニプレイヤーに残り、タップで再び引き出せます

import os
import time
import random
import datetime
import threading
import queue
import ctypes
import ui
import console
from objc_util import (ObjCClass, ObjCInstance, create_objc_class, on_main_thread, ns,
                       c, CGSize, CGRect, CGPoint)

NAV = None
_keep_alive = []      # デリゲート等がGCされないよう保持
_scoped_urls = []     # セキュリティスコープ付きURLを保持


# ---------- ユーティリティ ----------
def fmt_size(n):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if n < 1024:
            return ('%d %s' % (n, unit)) if unit == 'B' else ('%.1f %s' % (n, unit))
        n /= 1024.0
    return '%.1f TB' % n


def fmt_time(t):
    if not t:
        return '-'
    return datetime.datetime.fromtimestamp(t).strftime('%Y/%m/%d %H:%M')


def count_items(path):
    try:
        return len(os.listdir(path))
    except Exception:
        return None


# ---------- 画像サムネイル ----------
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.heic', '.heif',
              '.bmp', '.tif', '.tiff', '.webp'}
THUMB = 44                       # サムネイルの一辺（ポイント）
MAX_BYTES = 60 * 1024 * 1024     # これより大きい画像はサムネイル化しない

_thumb_cache = {}                # (path, mtime) -> ui.Image or None(失敗)
_pending = set()
_lock = threading.Lock()
_thumb_queue = queue.LifoQueue() # 後から要求された（＝今見えている）行を優先
_reload_scheduled = set()


def is_image(name):
    return os.path.splitext(name)[1].lower() in IMAGE_EXTS


UIImage = ObjCClass('UIImage')
UIBezierPath = ObjCClass('UIBezierPath')
c.UIGraphicsBeginImageContextWithOptions.argtypes = [CGSize, ctypes.c_bool, ctypes.c_double]
c.UIGraphicsBeginImageContextWithOptions.restype = None
c.UIGraphicsGetImageFromCurrentImageContext.restype = ctypes.c_void_p
c.UIGraphicsEndImageContext.restype = None


@on_main_thread
def make_thumb(path):
    src = UIImage.imageWithContentsOfFile_(path)
    if not src:
        return None
    sz = src.size()
    w, h = sz.width, sz.height
    if not w or not h:
        return None
    # 正方形に中央トリミング（アスペクトフィル）
    s = max(THUMB / w, THUMB / h)
    dw, dh = w * s, h * s
    box = CGRect(CGPoint(0, 0), CGSize(THUMB, THUMB))
    c.UIGraphicsBeginImageContextWithOptions(CGSize(THUMB, THUMB), False, 0.0)
    try:
        UIBezierPath.bezierPathWithRoundedRect_cornerRadius_(box, 6).addClip()
        src.drawInRect_(CGRect(CGPoint((THUMB - dw) / 2, (THUMB - dh) / 2),
                               CGSize(dw, dh)))
        return ObjCInstance(c.UIGraphicsGetImageFromCurrentImageContext())
    finally:
        c.UIGraphicsEndImageContext()


@on_main_thread
def _schedule_reload(tv):
    # サムネイルが次々できても、まとめて0.25秒ごとに再描画
    key = id(tv)
    if key in _reload_scheduled:
        return
    _reload_scheduled.add(key)

    def do():
        _reload_scheduled.discard(key)
        tv.reload()
    ui.delay(do, 0.25)


def _thumb_worker():
    while True:
        key, tv = _thumb_queue.get()
        try:
            thumb = make_thumb(key[0])
        except Exception:
            thumb = None
        with _lock:
            _thumb_cache[key] = thumb
            _pending.discard(key)
        _schedule_reload(tv)


threading.Thread(target=_thumb_worker, daemon=True).start()


def get_thumb(path, mtime, tv):
    key = (path, mtime)
    with _lock:
        if key in _thumb_cache:
            return _thumb_cache[key]
        if key not in _pending:
            _pending.add(key)
            _thumb_queue.put((key, tv))
    return None


# ---------- テーブルのデータソース ----------
class DirSource(object):
    def __init__(self, path):
        self.path = path
        self.entries = []
        self.error = None
        self.show_hidden = False
        self.reload()

    def reload(self):
        self.error = None
        try:
            names = os.listdir(self.path)
        except Exception as e:
            self.entries = []
            self.error = str(e)
            return
        entries = []
        for name in names:
            if not self.show_hidden and name.startswith('.'):
                continue
            p = os.path.join(self.path, name)
            isdir = os.path.isdir(p)
            try:
                st = os.stat(p)
                size, mtime = st.st_size, st.st_mtime
            except Exception:
                size, mtime = 0, 0
            entries.append({'name': name, 'path': p, 'isdir': isdir,
                            'size': size, 'mtime': mtime})
        # フォルダを先、名前順
        entries.sort(key=lambda e: (not e['isdir'], e['name'].lower()))
        self.entries = entries

    # --- data source ---
    def tableview_number_of_sections(self, tv):
        return 1

    def spacer_rows(self):
        # ミニプレイヤーに隠れないよう、再生中はリスト末尾に空行を足す
        return 2 if (PLAYER.paths and self.entries and not self.error) else 0

    def tableview_number_of_rows(self, tv, section):
        return max(1, len(self.entries)) + self.spacer_rows()

    def tableview_title_for_header(self, tv, section):
        return self.path

    def tableview_cell_for_row(self, tv, section, row):
        cell = ui.TableViewCell('subtitle')
        if self.error:
            cell.text_label.text = '⚠️ 読み込めません'
            cell.detail_text_label.text = self.error
            return cell
        if not self.entries:
            cell.text_label.text = '（空のフォルダ）'
            return cell
        if row >= len(self.entries):
            cell.selectable = False
            return cell
        e = self.entries[row]
        if e['isdir']:
            cell.text_label.text = '📁 ' + e['name']
            n = count_items(e['path'])
            info = ('%d 項目' % n) if n is not None else 'フォルダ'
            cell.detail_text_label.text = info + '  ・  ' + fmt_time(e['mtime'])
            cell.accessory_type = 'disclosure_indicator'
        else:
            thumb = None
            if is_image(e['name']) and e['size'] <= MAX_BYTES:
                thumb = get_thumb(e['path'], e['mtime'], tv)
                cell.text_label.text = e['name'] if thumb else '🖼 ' + e['name']
            elif is_audio(e['name']):
                playing = PLAYER.current_path() == e['path']
                cell.text_label.text = ('▶️ ' if playing else '🎵 ') + e['name']
            else:
                cell.text_label.text = '📄 ' + e['name']
            if thumb:
                ObjCInstance(cell.image_view).setImage_(thumb)
            cell.detail_text_label.text = fmt_size(e['size']) + '  ・  ' + fmt_time(e['mtime'])
        return cell

    # --- delegate ---
    def tableview_did_select(self, tv, section, row):
        if self.error or not self.entries or row >= len(self.entries):
            return
        e = self.entries[row]
        if e['isdir']:
            push_view(make_dir_view(e['path']))
            return
        if is_audio(e['name']):
            # 同じフォルダ内の音声ファイルをプレイリストにして再生
            group = [x for x in self.entries if not x['isdir'] and is_audio(x['name'])]
            PLAYER.load([x['path'] for x in group], group.index(e))
            tv.reload()
            MINI.update()
            open_player()
            return
        if is_image(e['name']):
            # 同じフォルダ内の画像だけを並べ、スワイプで行き来できるようにする
            group = [x for x in self.entries if not x['isdir'] and is_image(x['name'])]
        else:
            group = [e]
        show_preview([x['path'] for x in group], group.index(e))


# ---------- QuickLook プレビュー（スワイプ対応） ----------
ObjCClass('NSBundle').bundleWithPath_(
    '/System/Library/Frameworks/QuickLook.framework').load()
QLPreviewController = ObjCClass('QLPreviewController')
NSURL = ObjCClass('NSURL')
_preview_urls = []


def numberOfPreviewItemsInPreviewController_(_self, _cmd, _ctrl):
    return len(_preview_urls)
numberOfPreviewItemsInPreviewController_.restype = ctypes.c_long
numberOfPreviewItemsInPreviewController_.argtypes = [ctypes.c_void_p]


def previewController_previewItemAtIndex_(_self, _cmd, _ctrl, index):
    return _preview_urls[index].ptr
previewController_previewItemAtIndex_.restype = ctypes.c_void_p
previewController_previewItemAtIndex_.argtypes = [ctypes.c_void_p, ctypes.c_long]


PreviewDataSource = create_objc_class(
    'PyPreviewDataSource',
    methods=[numberOfPreviewItemsInPreviewController_,
             previewController_previewItemAtIndex_],
    protocols=['QLPreviewControllerDataSource'])


def present_vc(controller):
    app = ObjCClass('UIApplication').sharedApplication()
    vc = app.keyWindow().rootViewController()
    while vc.presentedViewController():
        vc = vc.presentedViewController()
    vc.presentViewController_animated_completion_(controller, True, None)


@on_main_thread
def show_preview(paths, index):
    global _preview_urls
    _preview_urls = [NSURL.fileURLWithPath_(p) for p in paths]
    ctrl = QLPreviewController.alloc().init()
    ds = PreviewDataSource.new()
    _keep_alive.append(ds)          # dataSource は弱参照なので保持しておく
    ctrl.setDataSource_(ds)
    ctrl.setCurrentPreviewItemIndex_(index)
    present_vc(ctrl)


# ---------- 音声プレイヤー（ループ・シャッフル対応） ----------
AUDIO_EXTS = {'.mp3', '.m4a', '.aac', '.wav', '.aif', '.aiff', '.aifc',
              '.caf', '.flac', '.m4b', '.mp4a'}

ObjCClass('NSBundle').bundleWithPath_(
    '/System/Library/Frameworks/AVFoundation.framework').load()
AVAudioPlayer = ObjCClass('AVAudioPlayer')
AVAudioSession = ObjCClass('AVAudioSession')

MINI_H = 60                      # ミニプレイヤーの高さ（ホームバー分は別）
HOME_BAR = 34                    # 画面下のホームインジケータ分の余白
LOOP_OFF, LOOP_ALL, LOOP_ONE = 'off', 'all', 'one'
LOOP_LABELS = {LOOP_OFF: '➡️ ループなし', LOOP_ALL: '🔁 全曲ループ', LOOP_ONE: '🔂 1曲ループ'}


def is_audio(name):
    return os.path.splitext(name)[1].lower() in AUDIO_EXTS


def fmt_dur(sec):
    sec = int(max(0, sec or 0))
    return '%d:%02d' % (sec // 60, sec % 60)


def audioPlayerDidFinishPlaying_successfully_(_self, _cmd, _player, _flag):
    ptr = ObjCInstance(_player).ptr
    # 次の曲の生成はコールバックの外で行う
    ui.delay(lambda: PLAYER.on_finished(ptr), 0)
audioPlayerDidFinishPlaying_successfully_.restype = None
audioPlayerDidFinishPlaying_successfully_.argtypes = [ctypes.c_void_p, ctypes.c_bool]


AudioDelegate = create_objc_class(
    'PyAudioPlayerDelegate',
    methods=[audioPlayerDidFinishPlaying_successfully_],
    protocols=['AVAudioPlayerDelegate'])


class AudioPlayer(object):
    """プレイリスト・ループ・シャッフルを管理する（アプリ全体で1つ）"""

    def __init__(self):
        self.paths = []        # フォルダ内の並び順
        self.order = []        # 再生順（paths のインデックス）
        self.pos = 0           # order 内の現在位置
        self.shuffle = False
        self.loop = LOOP_ALL
        self.av = None
        self.delegate = AudioDelegate.new()
        self._session_ready = False

    # --- 状態 ---
    def current_index(self):
        if not self.order:
            return None
        return self.order[self.pos]

    def current_path(self):
        i = self.current_index()
        return self.paths[i] if i is not None else None

    def is_playing(self):
        return bool(self.av and self.av.isPlaying())

    def current_time(self):
        return self.av.currentTime() if self.av else 0.0

    def duration(self):
        return self.av.duration() if self.av else 0.0

    # --- 再生順 ---
    def _build_order(self, first):
        n = len(self.paths)
        if self.shuffle:
            rest = [i for i in range(n) if i != first]
            random.shuffle(rest)
            self.order = [first] + rest
            self.pos = 0
        else:
            self.order = list(range(n))
            self.pos = first

    def _reshuffle_for_next_round(self):
        # 全曲ループ＋シャッフルで1周したら並びを作り直す（同じ曲が連続しないように）
        last = self.order[-1]
        order = list(range(len(self.paths)))
        random.shuffle(order)
        if len(order) > 1 and order[0] == last:
            order[0], order[-1] = order[-1], order[0]
        self.order = order

    def _advance(self, wrap):
        if self.pos + 1 < len(self.order):
            self.pos += 1
            return True
        if not wrap:
            return False
        if self.shuffle:
            self._reshuffle_for_next_round()
        self.pos = 0
        return True

    # --- 再生制御 ---
    def _activate_session(self):
        if self._session_ready:
            return
        session = AVAudioSession.sharedInstance()
        # Playback: マナーモードでも鳴る
        session.setCategory_error_('AVAudioSessionCategoryPlayback', None)
        session.setActive_error_(True, None)
        self._session_ready = True

    def _stop_av(self):
        if self.av:
            self.av.setDelegate_(None)
            self.av.stop()
            self.av = None

    def _apply_loop(self):
        if self.av:
            # 1曲ループは AVAudioPlayer 自体に無限ループさせる
            self.av.setNumberOfLoops_(-1 if self.loop == LOOP_ONE else 0)

    @on_main_thread
    def play_current(self):
        self._stop_av()
        if not self.order:
            return False
        self._activate_session()
        for _ in range(len(self.order)):
            path = self.paths[self.order[self.pos]]
            av = AVAudioPlayer.alloc().initWithContentsOfURL_error_(
                NSURL.fileURLWithPath_(path), None)
            if av:
                self.av = av
                av.setDelegate_(self.delegate)
                self._apply_loop()
                av.prepareToPlay()
                av.play()
                return True
            console.hud_alert('再生できません: ' + os.path.basename(path), 'error', 1.0)
            if not self._advance(wrap=True):
                break
        return False

    def load(self, paths, start):
        self.paths = list(paths)
        self._build_order(start)
        self.play_current()

    def play_index(self, i):
        if self.shuffle:
            self._build_order(i)
        else:
            self.pos = i
        self.play_current()

    def toggle_play(self):
        if not self.av:
            self.play_current()
        elif self.av.isPlaying():
            self.av.pause()
        else:
            self.av.play()

    def next(self):
        if self.order and self._advance(wrap=True):   # 手動の「次へ」は常に先頭へ回り込む
            self.play_current()

    def prev(self):
        if not self.order:
            return
        if self.av and self.av.currentTime() > 3:
            self.av.setCurrentTime_(0)                 # 3秒以上経っていれば曲の頭へ
            return
        self.pos = (self.pos - 1) % len(self.order)
        self.play_current()

    def seek(self, ratio):
        if self.av:
            self.av.setCurrentTime_(ratio * self.av.duration())

    def set_shuffle(self, on):
        self.shuffle = on
        cur = self.current_index()
        if cur is not None:
            self._build_order(cur)    # 今の曲はそのまま、以降の順番だけ変える

    def cycle_loop(self):
        modes = [LOOP_OFF, LOOP_ALL, LOOP_ONE]
        self.loop = modes[(modes.index(self.loop) + 1) % len(modes)]
        self._apply_loop()

    def stop(self):
        self._stop_av()

    @on_main_thread
    def on_finished(self, ptr):
        if not self.av or self.av.ptr != ptr:
            return                      # 古いプレイヤーからの通知は無視
        if self._advance(wrap=(self.loop == LOOP_ALL)):
            self.play_current()
        # ループなしで最後まで来たら停止（▶で最後の曲を頭から再生）


PLAYER = AudioPlayer()
_player_view = None


class TrackListSource(object):
    def tableview_number_of_sections(self, tv):
        return 1

    def tableview_number_of_rows(self, tv, section):
        return len(PLAYER.paths)

    def tableview_cell_for_row(self, tv, section, row):
        cell = ui.TableViewCell()
        name = os.path.basename(PLAYER.paths[row])
        if row == PLAYER.current_index():
            cell.text_label.text = '▶️ ' + name
            cell.text_label.text_color = '#007aff'
        else:
            cell.text_label.text = '🎵 ' + name
        return cell

    def tableview_did_select(self, tv, section, row):
        PLAYER.play_index(row)
        tv.reload()


class PlayerView(ui.View):
    def __init__(self):
        self.name = 'プレイヤー'
        self.background_color = 'white'
        self._last_index = -1

        self.title_label = ui.Label(font=('<system-bold>', 18), alignment=ui.ALIGN_CENTER,
                                    number_of_lines=2)
        self.count_label = ui.Label(font=('<system>', 13), alignment=ui.ALIGN_CENTER,
                                    text_color='gray')
        self.slider = ui.Slider(action=lambda s: PLAYER.seek(s.value))
        self.time_label = ui.Label(font=('<system>', 12), text_color='gray')
        self.dur_label = ui.Label(font=('<system>', 12), text_color='gray',
                                  alignment=ui.ALIGN_RIGHT)

        def btn(title, action, size=17):
            b = ui.Button(title=title, action=action, font=('<system>', size))
            self.add_subview(b)
            return b

        self.prev_btn = btn('⏮', lambda s: self._do(PLAYER.prev), 30)
        self.play_btn = btn('▶️', lambda s: self._do(PLAYER.toggle_play), 40)
        self.next_btn = btn('⏭', lambda s: self._do(PLAYER.next), 30)
        self.shuffle_btn = btn('', self.toggle_shuffle)
        self.loop_btn = btn('', self.toggle_loop)

        self.list_src = TrackListSource()
        self.track_list = ui.TableView(data_source=self.list_src, delegate=self.list_src,
                                       row_height=44)
        for v in (self.title_label, self.count_label, self.slider,
                  self.time_label, self.dur_label, self.track_list):
            self.add_subview(v)
        self.flex = 'WH'
        self.update_interval = 0.5   # 表示中だけ Pythonista が update() を呼んでくれる
        self.update()

    def _do(self, fn):
        fn()
        self.update()

    def toggle_shuffle(self, sender):
        PLAYER.set_shuffle(not PLAYER.shuffle)
        self.update()

    def toggle_loop(self, sender):
        PLAYER.cycle_loop()
        self.update()

    def layout(self):
        w, h = self.width, self.height
        m = 16
        y = 22                      # シート上端のつまみ（グラバー）の分だけ下げる
        self.title_label.frame = (m, y, w - 2 * m, 50)
        self.count_label.frame = (m, y + 50, w - 2 * m, 20)
        self.slider.frame = (m, y + 78, w - 2 * m, 34)
        self.time_label.frame = (m, y + 110, 80, 18)
        self.dur_label.frame = (w - m - 80, y + 110, 80, 18)
        cx = w / 2
        self.prev_btn.frame = (cx - 120, y + 136, 70, 60)
        self.play_btn.frame = (cx - 40, y + 131, 80, 70)
        self.next_btn.frame = (cx + 50, y + 136, 70, 60)
        half = (w - 3 * m) / 2
        self.shuffle_btn.frame = (m, y + 208, half, 40)
        self.loop_btn.frame = (2 * m + half, y + 208, half, 40)
        for b in (self.shuffle_btn, self.loop_btn):
            b.corner_radius = 8
            b.border_width = 1
        top = y + 260
        self.track_list.frame = (0, top, w, max(0, h - top))

    def update(self):
        idx = PLAYER.current_index()
        path = PLAYER.current_path()
        self.title_label.text = os.path.basename(path) if path else '（再生中の曲はありません）'
        self.count_label.text = ('%d / %d 曲' % (PLAYER.pos + 1, len(PLAYER.order))
                                 if path else '')
        dur = PLAYER.duration()
        cur = PLAYER.current_time()
        self.slider.value = (cur / dur) if dur else 0.0
        self.time_label.text = fmt_dur(cur)
        self.dur_label.text = fmt_dur(dur)
        self.play_btn.title = '⏸' if PLAYER.is_playing() else '▶️'

        on = PLAYER.shuffle
        self.shuffle_btn.title = '🔀 シャッフル: ' + ('オン' if on else 'オフ')
        self.shuffle_btn.tint_color = '#007aff' if on else 'gray'
        self.shuffle_btn.border_color = self.shuffle_btn.tint_color
        self.loop_btn.title = LOOP_LABELS[PLAYER.loop]
        self.loop_btn.tint_color = 'gray' if PLAYER.loop == LOOP_OFF else '#007aff'
        self.loop_btn.border_color = self.loop_btn.tint_color

        key = (id(PLAYER.paths), idx)
        if key != self._last_index:      # 曲やプレイリストが変わったらリストを更新
            self._last_index = key
            self.track_list.reload()


# --- 下から出るシート（UISheetPresentationController, iOS 15+） ---
UIViewController = ObjCClass('UIViewController')
UIColor = ObjCClass('UIColor')
_sheet_vc = None
DETENT_MEDIUM = 'com.apple.UIKit.medium'


def sheet_is_open():
    # ObjC を呼ばずに判定する（タイマー等から objc_util を呼ぶと競合でエラーになるため）
    return _player_view is not None and _player_view.on_screen


@on_main_thread
def open_player(sender=None):
    global _player_view, _sheet_vc
    if not PLAYER.paths:
        console.hud_alert('音声ファイルをタップすると再生できます', 'error', 1.5)
        return
    if sheet_is_open():
        _player_view.update()       # 既に出ていれば中身だけ更新
        return

    _player_view = PlayerView()
    vc = UIViewController.new()
    root = vc.view()
    root.setBackgroundColor_(UIColor.whiteColor())
    pv = ObjCInstance(_player_view)
    pv.setFrame_(root.bounds())
    pv.setAutoresizingMask_(2 | 16)          # 幅・高さを親に追従
    root.addSubview_(pv)

    sheet = vc.sheetPresentationController()
    if sheet:
        Detent = ObjCClass('UISheetPresentationControllerDetent')
        sheet.setDetents_(ns([Detent.mediumDetent(), Detent.largeDetent()]))
        sheet.setPrefersGrabberVisible_(True)
        # 半分の高さのときは背後のファイル一覧も操作できるようにする
        sheet.setLargestUndimmedDetentIdentifier_(DETENT_MEDIUM)
        sheet.setPrefersScrollingExpandsWhenScrolledToEdge_(False)

    _sheet_vc = vc
    present_vc(vc)


# --- 画面下に常駐するミニプレイヤー（Pythonista の ui だけで作る） ---
class MiniPlayer(ui.View):
    def __init__(self):
        self.background_color = '#f2f2f7'
        self.flex = 'WT'                    # 幅は追従、常に画面下に貼り付く
        self.hidden = True
        self.border = ui.View(background_color='#c6c6c8')
        self.progress = ui.View(background_color='#007aff')
        self.title_label = ui.Label(font=('<system-bold>', 15))
        self.sub_label = ui.Label(font=('<system>', 12), text_color='gray')
        # 曲名の部分をタップするとプレイヤーのシートを開く
        self.open_btn = ui.Button(action=open_player)

        def btn(title, fn, size):
            return ui.Button(title=title, font=('<system>', size),
                             action=lambda s: (fn(), self.update()))

        self.prev_btn = btn('⏮', PLAYER.prev, 24)
        self.play_btn = btn('▶️', PLAYER.toggle_play, 30)
        self.next_btn = btn('⏭', PLAYER.next, 24)
        for v in (self.border, self.progress, self.title_label, self.sub_label,
                  self.open_btn, self.prev_btn, self.play_btn, self.next_btn):
            self.add_subview(v)
        self.update_interval = 0.5

    def layout(self):
        w = self.width
        self.border.frame = (0, 0, w, 0.5)
        btn_w = 48
        x = w - 8 - btn_w * 3
        self.prev_btn.frame = (x, 6, btn_w, 48)
        self.play_btn.frame = (x + btn_w, 6, btn_w, 48)
        self.next_btn.frame = (x + btn_w * 2, 6, btn_w, 48)
        self.title_label.frame = (16, 10, x - 24, 22)
        self.sub_label.frame = (16, 32, x - 24, 18)
        self.open_btn.frame = (0, 0, x, MINI_H)

    def update(self):
        path = PLAYER.current_path()
        self.hidden = path is None
        if path is None:
            return
        self.title_label.text = os.path.basename(path)
        dur = PLAYER.duration()
        cur = PLAYER.current_time()
        self.sub_label.text = '%s / %s   %s%s' % (
            fmt_dur(cur), fmt_dur(dur),
            '🔀 ' if PLAYER.shuffle else '',
            {LOOP_OFF: '', LOOP_ALL: '🔁', LOOP_ONE: '🔂'}[PLAYER.loop])
        self.progress.frame = (0, 0, self.width * (cur / dur if dur else 0), 2)
        self.play_btn.title = '⏸' if PLAYER.is_playing() else '▶️'


MINI = None


def push_view(view):
    NAV.push_view(view)
    if MINI is not None:
        MINI.bring_to_front()               # 新しい画面より手前に出しておく


@on_main_thread
def close_player():
    if sheet_is_open():
        _sheet_vc.dismissViewControllerAnimated_completion_(True, None)


def _stop_when_closed():
    # ファイルブラウザ自体を閉じたら再生も止める
    while not NAV.on_screen:
        time.sleep(0.2)
    while NAV.on_screen:
        time.sleep(1.0)
    close_player()
    on_main_thread(PLAYER.stop)()


# ---------- フォルダ画面 ----------
def make_dir_view(path):
    tv = ui.TableView()
    tv.name = os.path.basename(path.rstrip('/')) or path
    tv.row_height = 60
    src = DirSource(path)
    tv.data_source = src
    tv.delegate = src

    def do_reload(sender):
        src.reload()
        tv.reload()

    def toggle_hidden(sender):
        src.show_hidden = not src.show_hidden
        sender.title = '隠し:表示' if src.show_hidden else '隠し:非表示'
        do_reload(sender)

    tv.right_button_items = [
        ui.ButtonItem(title='↻', action=do_reload),
        ui.ButtonItem(title='隠し:非表示', action=toggle_hidden),
        ui.ButtonItem(title='♪', action=open_player),
    ]
    return tv


# ---------- 「ファイル」アプリのフォルダピッカー ----------
def documentPicker_didPickDocumentsAtURLs_(_self, _cmd, _picker, _urls):
    urls = ObjCInstance(_urls)
    if urls.count() == 0:
        return
    url = urls.objectAtIndex_(0)
    url.startAccessingSecurityScopedResource()   # サンドボックス外へのアクセス許可
    _scoped_urls.append(url)
    path = str(url.path())
    push_view(make_dir_view(path))


def documentPickerWasCancelled_(_self, _cmd, _picker):
    pass


PickerDelegate = create_objc_class(
    'PyFolderPickerDelegate',
    methods=[documentPicker_didPickDocumentsAtURLs_, documentPickerWasCancelled_],
    protocols=['UIDocumentPickerDelegate'])


@on_main_thread
def pick_folder(sender=None):
    UIDocumentPickerViewController = ObjCClass('UIDocumentPickerViewController')
    # mode 1 = Open（コピーせずに元の場所を開く）
    picker = UIDocumentPickerViewController.alloc().initWithDocumentTypes_inMode_(
        ns(['public.folder']), 1)
    delegate = PickerDelegate.new()
    _keep_alive.append(delegate)
    picker.setDelegate_(delegate)
    present_vc(picker)


# ---------- 起動 ----------
def main():
    global NAV
    root = make_dir_view(os.path.expanduser('~/Documents'))
    root.name = 'Pythonista'
    root.left_button_items = [ui.ButtonItem(title='フォルダを開く', action=pick_folder)]
    NAV = ui.NavigationView(root)
    NAV.name = 'ファイルブラウザ'
    global MINI
    NAV.frame = (0, 0, 375, 667)            # present 前の仮サイズ（flex で追従）
    MINI = MiniPlayer()
    MINI.frame = (0, NAV.height - MINI_H - HOME_BAR, NAV.width, MINI_H + HOME_BAR)
    NAV.add_subview(MINI)
    NAV.present('fullscreen')
    threading.Thread(target=_stop_when_closed, daemon=True).start()


if __name__ == '__main__':
    main()
