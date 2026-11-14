# -*- coding: utf-8 -*-
# Pythonista 3 用 ファイルブラウザ
# ・起動時は Pythonista の Documents フォルダを表示
# ・左上「フォルダを開く」で「ファイル」アプリのピッカーを開き、
#   「このiPhone内」などの任意のフォルダを選んで中身を閲覧できます
# ・フォルダをタップで中へ移動、ファイルをタップで即プレビュー
# ・画像はプレビュー中に左右スワイプで同じフォルダの画像を行き来できます

import os
import datetime
import threading
import queue
import ctypes
import ui
from objc_util import ObjCClass, ObjCInstance, create_objc_class, on_main_thread, ns

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


def make_thumb(path):
    with open(path, 'rb') as f:
        data = f.read()
    img = ui.Image.from_data(data)
    if img is None:
        return None
    w, h = img.size
    if not w or not h:
        return None
    # 正方形に中央トリミング（アスペクトフィル）
    s = max(THUMB / w, THUMB / h)
    dw, dh = w * s, h * s
    with ui.ImageContext(THUMB, THUMB) as ctx:
        ui.Path.rounded_rect(0, 0, THUMB, THUMB, 6).add_clip()
        img.draw((THUMB - dw) / 2, (THUMB - dh) / 2, dw, dh)
        return ctx.get_image()


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

    def tableview_number_of_rows(self, tv, section):
        return max(1, len(self.entries))

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
            else:
                cell.text_label.text = '📄 ' + e['name']
            if thumb:
                cell.image_view.image = thumb
            cell.detail_text_label.text = fmt_size(e['size']) + '  ・  ' + fmt_time(e['mtime'])
        return cell

    # --- delegate ---
    def tableview_did_select(self, tv, section, row):
        if self.error or not self.entries:
            return
        e = self.entries[row]
        if e['isdir']:
            NAV.push_view(make_dir_view(e['path']))
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
    NAV.push_view(make_dir_view(path))


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
    NAV.present('fullscreen')


if __name__ == '__main__':
    main()
