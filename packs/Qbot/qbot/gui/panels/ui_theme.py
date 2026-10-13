"""
轻量 UI 主题：配色 / 字体 / 控件工厂，供左侧参数面板统一观感使用。
不引入第三方依赖，仅基于 wx 标准控件。
"""
import wx


class COLORS:
    """统一配色（与右侧图表暗色工具栏呼应，整体浅色卡片风）"""
    BG          = wx.Colour(247, 248, 250)   # 页面背景
    CARD        = wx.Colour(255, 255, 255)   # 卡片背景
    CARD_LINE   = wx.Colour(226, 230, 235)   # 卡片描边
    PRIMARY     = wx.Colour(41, 98, 255)     # 主色蓝（按钮/强调条）
    PRIMARY_DK  = wx.Colour(28, 74, 200)     # 主色深（按下）
    PRIMARY_BG  = wx.Colour(234, 241, 255)   # 主色浅底（选中/提示）
    SUCCESS     = wx.Colour(38, 166, 154)    # 成功绿
    DANGER      = wx.Colour(229, 57, 53)     # 错误红
    WARN        = wx.Colour(245, 124, 0)     # 警示橙
    TEXT        = wx.Colour(40, 44, 52)      # 主文字
    TEXT_SUB    = wx.Colour(110, 116, 125)   # 次级文字
    TEXT_HINT   = wx.Colour(150, 156, 165)   # 提示文字
    IDLE        = wx.Colour(160, 166, 174)   # 状态点-空闲


class FONTS:
    """统一字体（pt）"""
    @staticmethod
    def title():     return wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
    @staticmethod
    def body():      return wx.Font(9,  wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
    @staticmethod
    def small():     return wx.Font(8,  wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
    @staticmethod
    def button_big():return wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
    @staticmethod
    def button():    return wx.Font(9,  wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)


BTN_KIND = {
    # kind: (bg, fg, 边框)
    "primary": (COLORS.PRIMARY,  wx.Colour(255, 255, 255), COLORS.PRIMARY),
    "success": (COLORS.SUCCESS,  wx.Colour(255, 255, 255), COLORS.SUCCESS),
    "ghost":   (wx.Colour(255, 255, 255), COLORS.PRIMARY,  COLORS.CARD_LINE),
    "danger":  (COLORS.DANGER,   wx.Colour(255, 255, 255), COLORS.DANGER),
    "plain":   (wx.Colour(255, 255, 255), COLORS.TEXT,     COLORS.CARD_LINE),
}


# ==== wxMSW 截断 bug 规避 ====
# wx 控件标签含非 BMP 字符(emoji, U+10000+)时，MSW 后端会丢掉最后一个字符
# （"📥  行情数据" → "📥  行情数"）。所有 wx 控件标签必须经过 wxs() 清洗：
# 已知 emoji 映射为 BMP 安全符号；残余非 BMP 字符直接剔除。
_EMOJI_MAP = {
    "📥": "⬇", "🎯": "◎", "⚙️": "⚙", "🛡️": "✚", "📊": "▤", "🧩": "✚",
    "📋": "☰", "📄": "▦", "🔄": "↻", "📂": "▤", "🗑": "✖", "🧮": "∑",
    "🌐": "◍", "📦": "", "🔍": "◎", "📜": "☰", "⬇": "⬇",
}


def wxs(s):
    """清洗 wx 控件标签：替换/剔除非 BMP 字符，避免 MSW 截断丢字。"""
    if not s:
        return s
    for k, v in _EMOJI_MAP.items():
        s = s.replace(k, v)
    out = []
    for ch in s:
        if ord(ch) >= 0x10000 or (0xFE00 <= ord(ch) <= 0xFE0F) or ord(ch) == 0x200D:
            continue  # 非 BMP emoji / 变体选择符 / ZWJ
        out.append(ch)
    return "".join(out).replace("  ", " ").strip() if out else ""


def make_button(parent, label, kind="plain", height=30, big=False):
    """统一样式按钮"""
    b = wx.Button(parent, -1, wxs(label))
    bg, fg, _ = BTN_KIND.get(kind, BTN_KIND["plain"])
    b.SetBackgroundColour(bg)
    b.SetForegroundColour(fg)
    b.SetFont(FONTS.button_big() if big else FONTS.button())
    b.SetMinSize((-1, height))
    return b


def make_label(parent, text, size="body", color=None, wrap_width=None):
    """统一标签"""
    lb = wx.StaticText(parent, label=wxs(text))
    lb.SetFont(getattr(FONTS, size)())
    if color is not None:
        lb.SetForegroundColour(color)
    if wrap_width:
        lb.Wrap(wrap_width)
    return lb


class SectionCard(wx.Panel):
    """自绘折叠卡片：头部（主色竖条+标题+箭头）+ 可收展内容区。
    用法：
        card = SectionCard(parent, "行情数据", icon="📥")
        body = card.body           # 内容容器，直接往 card.body_sizer 里 Add
        card.set_expanded(False)    # 收起
    """

    HEADER_H_MIN = 30

    def __init__(self, parent, title, icon="", expanded=True, on_toggle=None):
        super().__init__(parent, style=wx.BORDER_NONE)
        self.SetBackgroundColour(COLORS.BG)
        # 收展回调：由宿主面板提供（如 _layout_full_chain），卡片自身不碰祖先窗口
        self._on_toggle = on_toggle

        self._expanded = expanded
        outer = wx.BoxSizer(wx.VERTICAL)

        # --- 卡片容器（白底）---
        self.card = wx.Panel(self)
        self.card.SetBackgroundColour(COLORS.CARD)
        card_sizer = wx.BoxSizer(wx.VERTICAL)
        self.card.SetSizer(card_sizer)

        title_text = wxs((icon + "  " if icon else "") + title)

        # --- 头部高度按文字实际像素自适应（中文字+emoji 在高 DPI 下比固定值高）---
        dc = wx.ClientDC(self)
        dc.SetFont(FONTS.title())
        _, text_h = dc.GetTextExtent(title_text)
        arrow_h = dc.GetTextExtent("▾")[1]
        header_h = max(self.HEADER_H_MIN,
                       text_h + 12,          # 标题上下各留 6px
                       arrow_h + 12)
        self.header = wx.Panel(self.card)
        self.header.SetMinSize((-1, header_h))
        self.header.SetBackgroundColour(COLORS.CARD)
        self.header.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        hs = wx.BoxSizer(wx.HORIZONTAL)
        self._accent = wx.Panel(self.header, size=(4, -1))
        self._accent.SetBackgroundColour(COLORS.PRIMARY)
        hs.Add(self._accent, 0, wx.EXPAND | wx.LEFT | wx.TOP | wx.BOTTOM, 7)

        self.title_lb = wx.StaticText(self.header, label=title_text)
        self.title_lb.SetFont(FONTS.title())
        self.title_lb.SetForegroundColour(COLORS.TEXT)
        hs.Add(self.title_lb, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)

        self.arrow_lb = wx.StaticText(self.header, label="▾" if expanded else "▸")
        self.arrow_lb.SetFont(FONTS.title())
        self.arrow_lb.SetForegroundColour(COLORS.TEXT_HINT)
        hs.Add(self.arrow_lb, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        self.header.SetSizer(hs)

        self.header.Bind(wx.EVT_LEFT_DOWN, lambda e: self.toggle())
        for w in (self.title_lb, self.arrow_lb):
            w.Bind(wx.EVT_LEFT_DOWN, lambda e: self.toggle())

        # --- 内容区 ---
        self.body = wx.Panel(self.card)
        self.body.SetBackgroundColour(COLORS.CARD)
        self.body_sizer = wx.BoxSizer(wx.VERTICAL)
        self.body.SetSizer(self.body_sizer)

        card_sizer.Add(self.header, 0, wx.EXPAND)
        card_sizer.Add(wx.StaticLine(self.card), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        card_sizer.Add(self.body, 0, wx.EXPAND | wx.ALL, 6)

        outer.Add(self.card, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        self.SetSizer(outer)

        self.set_expanded(expanded)

    # --- 公共 API ---
    @property
    def expanded(self):
        return self._expanded

    def toggle(self, event=None):
        self.set_expanded(not self._expanded)

    def set_expanded(self, show):
        self._expanded = show
        self.arrow_lb.SetLabel("▾" if show else "▸")
        self.body.Show(show)
        self.Layout()
        # 只通知宿主回调刷新（宿主自行决定如何重排滚动区），
        # 严禁遍历祖先调 FitInside——对非滚动窗口 FitInside 会改窗口尺寸，撑爆整体布局
        if self._on_toggle is not None:
            try:
                self._on_toggle()
            except Exception:
                pass
