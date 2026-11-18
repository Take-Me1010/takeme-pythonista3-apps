# -*- coding: utf-8 -*-
# Pythonista 3 用 ファイルブラウザ — 音声プレイヤー部分
# ・同じフォルダの音声をまとめたプレイリストを再生
#   ループ再生（なし / 全曲 / 1曲）・シャッフル再生に対応
# ・プレイヤーは下から出るシート（半分 / 全画面、下スワイプで閉じる）
# ・閉じても画面下のミニプレイヤーに残り、タップで再び引き出せます
#
# main.py からは主に次を使います:
#   is_audio(name)            音声ファイルかどうか
#   PLAYER                    再生状態（current_path() など）
#   play_files(paths, index)  プレイリストを読み込んで再生し、シートを開く
#   open_player(sender=None)  プレイヤーのシートを開く（ButtonItem の action にも使える）
#   attach_mini_player(nav)   NavigationView にミニプレイヤーを付け、終了時の停止を仕込む
#   bring_mini_to_front()     画面遷移後にミニプレイヤーを手前に出す
#   forget_paths(paths)       削除したファイルをプレイリストから外す

import os
import time
import random
import threading
import ctypes
import ui
import console
from objc_util import ObjCClass, ObjCInstance, create_objc_class, on_main_thread, ns

NSURL = ObjCClass('NSURL')


def present_vc(controller):
    app = ObjCClass('UIApplication').sharedApplication()
    vc = app.keyWindow().rootViewController()
    while vc.presentedViewController():
        vc = vc.presentedViewController()
    vc.presentViewController_animated_completion_(controller, True, None)


# ---------- 音声プレイヤー本体（ループ・シャッフル対応） ----------
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


def bring_mini_to_front():
    if MINI is not None:
        MINI.bring_to_front()               # 新しい画面より手前に出しておく


@on_main_thread
def close_player():
    if sheet_is_open():
        _sheet_vc.dismissViewControllerAnimated_completion_(True, None)


def _stop_when_closed(nav):
    # ファイルブラウザ自体を閉じたら再生も止める
    while not nav.on_screen:
        time.sleep(0.2)
    while nav.on_screen:
        time.sleep(1.0)
    close_player()
    on_main_thread(PLAYER.stop)()


# ---------- main.py から使う窓口 ----------
def has_playlist():
    return bool(PLAYER.paths)


def play_files(paths, index):
    """プレイリストを読み込んで index の曲から再生し、プレイヤーを開く"""
    PLAYER.load(paths, index)
    if MINI is not None:
        MINI.update()
    open_player()


@on_main_thread
def forget_paths(removed):
    """削除したファイル（フォルダならその中身も）をプレイリストから外す"""
    removed = set(removed)
    prefixes = tuple(r.rstrip('/') + '/' for r in removed)

    def gone(p):
        return p in removed or p.startswith(prefixes)

    P = PLAYER
    if not any(gone(p) for p in P.paths):
        return
    cur = P.current_index()
    cur_gone = cur is not None and gone(P.paths[cur])
    was_playing = P.is_playing()

    # 残る曲だけで paths を作り直し、再生順（order）もそのまま詰める
    remap, paths = {}, []
    for i, p in enumerate(P.paths):
        if not gone(p):
            remap[i] = len(paths)
            paths.append(p)
    order = [remap[i] for i in P.order if i in remap]
    if cur_gone:
        # 今の曲が消えたら、次に流れるはずだった曲の位置へ
        pos = sum(1 for i in P.order[:P.pos] if i in remap)
        pos = pos if pos < len(order) else 0
    else:
        pos = order.index(remap[cur]) if cur is not None else 0

    if cur_gone:
        P._stop_av()
    P.paths, P.order, P.pos = paths, order, pos
    if not paths:
        close_player()
    elif cur_gone and was_playing:
        P.play_current()

    if MINI is not None:
        MINI.update()
    if sheet_is_open():
        _player_view.update()


def attach_mini_player(nav):
    """nav（ui.NavigationView）の下端にミニプレイヤーを貼り付ける。present の前に呼ぶ"""
    global MINI
    MINI = MiniPlayer()
    MINI.frame = (0, nav.height - MINI_H - HOME_BAR, nav.width, MINI_H + HOME_BAR)
    nav.add_subview(MINI)
    threading.Thread(target=_stop_when_closed, args=(nav,), daemon=True).start()
    return MINI
