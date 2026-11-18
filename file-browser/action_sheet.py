# -*- coding: utf-8 -*-
# Pythonista 3 用 アクションシート風メニュー（ui だけで作る）
# ・下からスライドして出て、背景タップかキャンセルで閉じます
# ・UIAlertController は使いません。UIAlertAction のハンドラ（ObjCBlock）を UIKit が
#   呼ぶときにクラッシュするため（knowledge/bugs.md 参照）
#
# 使い方:
#   import action_sheet as sheet
#   sheet.show(parent_view, 'タイトル', '補足', [
#       ('削除', sheet.ACTION_DESTRUCTIVE, on_delete),
#       ('キャンセル', sheet.ACTION_CANCEL, None),
#   ])
# ボタンの関数はシートが閉じ終わってから（メインスレッドで）呼ばれます。

import ui
from objc_util import on_main_thread

ACTION_DEFAULT, ACTION_CANCEL, ACTION_DESTRUCTIVE = 0, 1, 2
SHEET_ROW = 56          # ボタン1行の高さ
SHEET_HEAD = 64         # タイトル部分の高さ
SAFE_BOTTOM = 34        # ホームインジケータ分の余白
_current = None         # 表示中のシート（同時に1つだけ）


class ActionSheet(ui.View):
    def __init__(self, title, message, actions):
        """actions: [(ボタン名, ACTION_*, 押したときの関数 or None), ...]"""
        self.flex = 'WH'
        self.background_color = (0, 0, 0, 0)
        self._shown = False
        self._closing = False
        # 背景をタップしたら閉じる
        self.dim = ui.Button(action=lambda s: self.close())
        self.dim.flex = 'WH'
        self.add_subview(self.dim)

        self.card = ui.View(background_color='white', corner_radius=14)
        self.cancel_card = ui.View(background_color='white', corner_radius=14)
        self.title_label = ui.Label(text=title, font=('<system-bold>', 14),
                                    text_color='#8e8e93', alignment=ui.ALIGN_CENTER)
        self.msg_label = ui.Label(text=message, font=('<system>', 12),
                                  text_color='#8e8e93', alignment=ui.ALIGN_CENTER)
        self.card.add_subview(self.title_label)
        self.card.add_subview(self.msg_label)

        self.rows = []
        for label, kind, fn in actions:
            cancel = kind == ACTION_CANCEL
            b = ui.Button(title=label,
                          font=('<system-bold>' if cancel else '<system>', 19),
                          tint_color='#ff3b30' if kind == ACTION_DESTRUCTIVE else '#007aff',
                          action=(lambda fn: lambda s: self.close(fn))(fn))
            line = ui.View(background_color='#d1d1d6')
            parent = self.cancel_card if cancel else self.card
            parent.add_subview(line)
            parent.add_subview(b)
            self.rows.append((cancel, b, line))
        self.has_cancel = any(r[0] for r in self.rows)
        self.add_subview(self.card)
        self.add_subview(self.cancel_card)

    def layout(self):
        w, h = self.width, self.height
        m = 8
        cw = w - 2 * m
        self.title_label.frame = (16, 12, cw - 32, 22)
        self.msg_label.frame = (16, 34, cw - 32, 18)
        y = SHEET_HEAD
        for cancel, b, line in self.rows:
            if cancel:
                line.hidden = True
                b.frame = (0, 0, cw, SHEET_ROW)
            else:
                line.frame = (0, y, cw, 0.5)
                b.frame = (0, y, cw, SHEET_ROW)
                y += SHEET_ROW
        extra = (m + SHEET_ROW) if self.has_cancel else 0
        top = (h - y - extra - SAFE_BOTTOM) if self._shown else h   # 閉じているときは画面外
        self.card.frame = (m, top, cw, y)
        self.cancel_card.frame = (m, top + y + m, cw, SHEET_ROW)
        self.cancel_card.hidden = not self.has_cancel
        self.background_color = (0, 0, 0, 0.3 if self._shown else 0)

    def show(self, parent):
        self.frame = parent.bounds
        parent.add_subview(self)
        self.layout()
        self._shown = True
        ui.animate(self.layout, 0.25)

    def close(self, then=None):
        if self._closing:
            return
        self._closing = True
        self._shown = False

        def done():
            global _current
            if self.superview:
                self.superview.remove_subview(self)
            if _current is self:
                _current = None
            if then:
                then()
        ui.animate(self.layout, 0.2, completion=done)


def is_open():
    return _current is not None


@on_main_thread
def show(parent, title, message, actions):
    """parent（ui.View）の上にシートを出す。既に出ていれば何もせず None を返す"""
    global _current
    if _current is not None:
        return None
    _current = ActionSheet(title, message, actions)
    _current.show(parent)
    return _current
