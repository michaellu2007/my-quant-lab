"""
「📦 版本与回退」面板：
  - 顶部：当前版本 + 改动说明输入 + 创建新版本
  - 中部：历史版本列表（版本/时间/说明/大小）
  - 操作：回退到选中版本（自动先做安全快照，可再回退回来）
  - 底部：CHANGELOG 预览
"""
import os

import wx

from qbot.common import versioning as V
from qbot.gui.panels.ui_theme import COLORS, FONTS, make_button, make_label

try:
    from qbot.common.logging.logger import LOGGER as logger
except Exception:
    import logging
    logger = logging.getLogger("qbot.versions")


class PanelVersions(wx.Panel):
    def __init__(self, parent=None, id=-1, displaySize=(1600, 900)):
        super().__init__(parent, id)
        self._build_ui()
        self.refresh_all()

    # ---------------- UI ----------------
    def _build_ui(self):
        sizer = wx.BoxSizer(wx.HORIZONTAL)

        # ===== 左：操作区 =====
        left = wx.Panel(self)
        left.SetBackgroundColour(COLORS.BG)
        ls = wx.BoxSizer(wx.VERTICAL)
        left.SetSizer(ls)

        # 当前版本卡片
        card = wx.Panel(left)
        card.SetBackgroundColour(COLORS.CARD)
        cs = wx.BoxSizer(wx.VERTICAL)
        card.SetSizer(cs)
        self.cur_label = make_label(card, "当前版本: -", size="title", color=COLORS.PRIMARY)
        cs.Add(self.cur_label, 0, wx.ALL, 8)

        cs.Add(make_label(card, "本次改动说明（写清楚改了什么，方便以后回溯）：",
                          size="small", color=COLORS.TEXT_SUB), 0, wx.LEFT | wx.RIGHT, 8)
        self.note_input = wx.TextCtrl(card, -1, "", style=wx.TE_PROCESS_ENTER)
        self.note_input.Bind(wx.EVT_TEXT_ENTER, lambda e: self.on_create(None))
        cs.Add(self.note_input, 0, wx.EXPAND | wx.ALL, 8)

        row = wx.BoxSizer(wx.HORIZONTAL)
        self.create_but = make_button(card, "📦 创建新版本", kind="primary", height=34)
        self.create_but.Bind(wx.EVT_BUTTON, self.on_create)
        row.Add(self.create_but, 1, wx.RIGHT, 6)
        self.refresh_but = make_button(card, "🔄 刷新", kind="ghost", height=34)
        self.refresh_but.Bind(wx.EVT_BUTTON, lambda e: self.refresh_all())
        row.Add(self.refresh_but, 1)
        cs.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        ls.Add(card, 0, wx.EXPAND | wx.ALL, 6)

        # 历史版本列表
        ls.Add(make_label(left, "历史版本（选中后可回退）:", size="body",
                          color=COLORS.TEXT), 0, wx.LEFT | wx.BOTTOM, 8)
        self.release_list = wx.ListCtrl(left, -1, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.release_list.SetFont(FONTS.body())
        for i, (col, w) in enumerate([("版本", 80), ("时间", 130), ("说明", 220),
                                      ("文件数", 60), ("大小MB", 70)]):
            self.release_list.InsertColumn(i, col, width=w)
        ls.Add(self.release_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        rb = wx.BoxSizer(wx.HORIZONTAL)
        self.rollback_but = make_button(left, "⏪ 回退到选中版本", kind="danger", height=34)
        self.rollback_but.Bind(wx.EVT_BUTTON, self.on_rollback)
        rb.Add(self.rollback_but, 1, wx.RIGHT, 6)
        self.open_dir_but = make_button(left, "📂 打开快照目录", kind="ghost", height=34)
        self.open_dir_but.Bind(wx.EVT_BUTTON, self.on_open_dir)
        rb.Add(self.open_dir_but, 1)
        ls.Add(rb, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        safety_hint = make_label(
            left,
            "回退前会自动把当前状态另存到 updates/rollback_safety/，\n"
            "如回退错了还能再退回来。回退后请重启程序生效。",
            size="small", color=COLORS.TEXT_HINT)
        ls.Add(safety_hint, 0, wx.ALL, 8)

        # ===== 右：更新日志 =====
        right = wx.Panel(self)
        right.SetBackgroundColour(COLORS.CARD)
        rs = wx.BoxSizer(wx.VERTICAL)
        right.SetSizer(rs)
        rs.Add(make_label(right, "📜 更新日志 (CHANGELOG.md)", size="title",
                          color=COLORS.TEXT), 0, wx.ALL, 8)
        self.changelog = wx.TextCtrl(right, -1, style=wx.TE_MULTILINE | wx.TE_READONLY |
                                     wx.TE_DONTWRAP)
        self.changelog.SetBackgroundColour(COLORS.CARD)
        self.changelog.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE,
                                       wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        rs.Add(self.changelog, 1, wx.EXPAND | wx.ALL, 6)

        sizer.Add(left, 0, wx.EXPAND | wx.ALL, 4)
        sizer.Add(right, 1, wx.EXPAND | wx.ALL, 4)
        self.SetSizer(sizer)

    # ---------------- 数据 ----------------
    def refresh_all(self):
        self.cur_label.SetLabel(f"当前版本: v{V.current_version()}")
        # 版本列表
        self.release_list.DeleteAllItems()
        for i, e in enumerate(V.list_releases()):
            idx = self.release_list.InsertItem(i, f"v{e['version']}")
            self.release_list.SetItem(idx, 1, e.get("date", ""))
            note = e.get("note", "") or "（无说明）"
            self.release_list.SetItem(idx, 2, note[:60])
            self.release_list.SetItem(idx, 3, str(e.get("files", "")))
            self.release_list.SetItem(idx, 4, str(e.get("size_mb", "")))
            self.release_list.SetItemData(idx, i)
        self.changelog.SetValue(V.read_changelog())
        self.Layout()

    def _selected_entry(self):
        idx = self.release_list.GetFirstSelected()
        if idx == -1:
            return None
        entries = V.list_releases()
        return entries[idx] if idx < len(entries) else None

    # ---------------- 操作 ----------------
    def on_create(self, event):
        note = self.note_input.GetValue().strip()
        if not note:
            wx.MessageBox("请先填写本次改动说明（改了什么、为什么改）",
                          "提示", wx.OK | wx.ICON_INFORMATION)
            return
        confirm = wx.MessageBox(
            f"将把当前全部源码打包为新版本快照，并写入更新日志。\n\n说明: {note}\n\n继续？",
            "创建版本", wx.YES_NO | wx.ICON_QUESTION)
        if confirm != wx.ID_YES:
            return
        try:
            entry = V.create_release(note)
        except Exception as e:
            logger.exception("创建版本失败")
            wx.MessageBox(f"创建版本失败: {e}", "错误", wx.OK | wx.ICON_ERROR)
            return
        self.note_input.SetValue("")
        self.refresh_all()
        wx.MessageBox(f"版本 v{entry['version']} 已创建\n"
                      f"快照: {entry['file']}\n"
                      f"（{entry['files']} 文件, {entry['size_mb']} MB）",
                      "完成", wx.OK | wx.ICON_INFORMATION)

    def on_rollback(self, event):
        entry = self._selected_entry()
        if entry is None:
            wx.MessageBox("请先在列表中选中要回退到的版本", "提示",
                          wx.OK | wx.ICON_INFORMATION)
            return
        cur = V.current_version()
        confirm = wx.MessageBox(
            f"确定回退？\n\n  当前版本: v{cur}\n  回退到:   v{entry['version']}\n\n"
            f"回退前会自动把当前状态另存（updates/rollback_safety/），\n"
            f"回退后需重启程序生效。继续？",
            "确认回退", wx.YES_NO | wx.ICON_WARNING)
        if confirm != wx.ID_YES:
            return
        try:
            result = V.rollback(entry["version"])
        except Exception as e:
            logger.exception("回退失败")
            wx.MessageBox(f"回退失败: {e}", "错误", wx.OK | wx.ICON_ERROR)
            return
        if result["success"]:
            self.refresh_all()
            wx.MessageBox(
                f"{result['message']}\n\n"
                f"回退前状态已保存为:\n{result['safety']}\n\n请重启程序使回退生效。",
                "回退完成", wx.OK | wx.ICON_INFORMATION)
        else:
            wx.MessageBox(result["message"], "错误", wx.OK | wx.ICON_ERROR)

    def on_open_dir(self, event):
        import subprocess
        os.makedirs(V.RELEASES_DIR, exist_ok=True)
        try:
            os.startfile(V.RELEASES_DIR)
        except Exception:
            subprocess.Popen(["explorer", os.path.normpath(V.RELEASES_DIR)])
