# -*- coding: utf-8 -*-
"""
书页朗读助手
功能: 导入图片或PDF文件, 在画面上拖拽选择区域, 自动OCR识别文字并使用edge-tts朗读
"""


def _patch_edge_tts_403():
    """微软 2026-10 把 TTS 合成服务迁到新域名，旧域名一律 403；官方发版后本补丁自动失效。"""
    try:
        import email.utils
        import edge_tts.constants
        import edge_tts.communicate
    except Exception:
        return
    if "speech.platform.bing.com" not in edge_tts.communicate.WSS_URL:
        return
    _url = (
        "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
        f"?Ocp-Apim-Subscription-Key={edge_tts.constants.TRUSTED_CLIENT_TOKEN}"
    )
    edge_tts.constants.WSS_URL = edge_tts.communicate.WSS_URL = _url
    try:  # 中文 locale 下 403 后时钟补偿重试解析 Date 头会失败，一并修复
        import edge_tts.drm

        def _parse_date(date):
            try:
                parsed = email.utils.parsedate_to_datetime(date)
            except (TypeError, ValueError, IndexError):
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=edge_tts.drm.tz.utc)
            return parsed.timestamp()

        edge_tts.drm.DRM.parse_rfc2616_date = staticmethod(_parse_date)
    except Exception:
        pass

_patch_edge_tts_403()

import sys
import os
import io
import asyncio
import tempfile
import traceback
import faulthandler
import re
import shutil
import sqlite3
from pathlib import Path
import numpy as np

# 英文空格恢复库: rapidocr 对英文间歇性丢空格, 需要在我们这层恢复
try:
    import wordninja as _wordninja
except ImportError:
    _wordninja = None


def app_dir() -> str:
    """返回应用资源目录.

    - 开发模式 (直接 python main.py): 返回源码所在目录
    - PyInstaller onedir 打包: 返回 _internal 目录 (打包资源都在这里)
    - PyInstaller onefile 打包: 返回 sys._MEIPASS 解压目录
    """
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后: onedir 的 _internal / onefile 的临时解压
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def app_user_dir() -> str:
    """返回用户可写目录 (history/crash/debug 日志).

    商店版 (MSIX) 安装目录只读, 统一写到 %LOCALAPPDATA%\\书页朗读助手;
    旧版把 history.json 放在 exe 同级, 首次运行时迁移过来.
    """
    base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                        "书页朗读助手")
    os.makedirs(base, exist_ok=True)
    legacy_dir = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
                  else os.path.dirname(os.path.abspath(__file__)))
    old_history = os.path.join(legacy_dir, "history.json")
    new_history = os.path.join(base, "history.json")
    if os.path.isfile(old_history) and not os.path.isfile(new_history):
        try:
            shutil.copyfile(old_history, new_history)
        except OSError:
            pass
    return base


# 崩溃日志: 记录原生崩溃的调用栈
_crash_log = open(os.path.join(app_user_dir(), "crash.log"), "w", encoding="utf-8")
faulthandler.enable(_crash_log)
faulthandler.enable(_crash_log, all_threads=True)

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QSlider, QCheckBox, QScrollArea,
    QTextEdit, QListWidget, QListWidgetItem, QFileDialog, QStatusBar,
    QGroupBox, QSizePolicy, QSplitter, QFrame, QLineEdit, QMessageBox,
    QMenu, QTableWidget, QTableWidgetItem, QHeaderView
)
from PyQt6.QtCore import (
    Qt, QRect, QPoint, QSize, pyqtSignal, QThread, QTimer, QUrl, QBuffer,
    QSettings, QIODevice, QMutex, QWaitCondition
)
from PyQt6.QtGui import (
    QPixmap, QImage, QPainter, QColor, QPen, QFont, QIcon, QKeySequence,
    QShortcut, QRegion, QImageReader, QCursor
)

# 导入可能较重的模块(延迟到实际使用时再报错)
import pymupdf as fitz  # PyMuPDF (用 pymupdf 替代已弃用的 fitz)
import edge_tts
from rapidocr_onnxruntime import RapidOCR
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from about_dialog import show_about


# ============================================================
# 常量与配置
# ============================================================

APP_TITLE = "书页朗读助手"

# 缺省发音人: 美式英语 (点读场景以英文课本为主; 用户手选后自动记住)
DEFAULT_VOICE_ID = "en-US-AriaNeural"

# edge-tts 发音人列表 (默认回退, 启动后会动态从 Azure 拉取最新可用列表)
VOICES_FALLBACK = [
    ("Aria (女·美式)",     "en-US-AriaNeural"),
    ("晓晓 (女·大陆)",   "zh-CN-XiaoxiaoNeural"),
    ("晓伊 (女·大陆)",   "zh-CN-XiaoyiNeural"),
    ("云希 (男·大陆)",   "zh-CN-YunxiNeural"),
    ("云扬 (男·大陆)",   "zh-CN-YunyangNeural"),
    ("云健 (男·大陆)",   "zh-CN-YunjianNeural"),
    ("HsiaoChen (女·台湾)", "zh-TW-HsiaoChenNeural"),
    ("YunJhe (男·台湾)",    "zh-TW-YunJheNeural"),
    ("Jenny (女·美式)",    "en-US-JennyNeural"),
    ("Guy (男·美式)",      "en-US-GuyNeural"),
]

# 中文 voice 名字映射 (ShortName 里的英文名 → 中文昵称)
_VOICE_NAME_MAP = {
    "Xiaoxiao": "晓晓", "Xiaoyi": "晓伊", "Xiaobei": "晓北",
    "Yunjian": "云健", "Yunxi": "云希", "Yunxia": "云夏", "Yunyang": "云扬",
    "Xiaoni": "晓妮",
    "HsiaoChen": "晓辰", "HsiaoYu": "晓雨", "YunJhe": "云哲",
}

# 英文 voice 名字映射 (保持英文 Friendly)
_VOICE_EN_MAP = {
    "Jenny": "Jenny", "Guy": "Guy", "Aria": "Aria",
    "Emma": "Emma", "Ava": "Ava", "Brian": "Brian", "Eric": "Eric",
    "Christopher": "Christopher", "Michelle": "Michelle", "Roger": "Roger",
    "Andrew": "Andrew", "Ana": "Ana", "Steffan": "Steffan",
}


def _load_voices_from_azure() -> list:
    """动态从 Azure 拉取最新可用的 zh-CN/zh-TW/en-US voice 列表

    返回 [(显示名, ShortName), ...] 或失败/超时(6秒)时返回 VOICES_FALLBACK
    """
    async def _fetch():
        return await asyncio.wait_for(edge_tts.list_voices(), timeout=6)

    try:
        voices = asyncio.run(_fetch())
    except Exception:
        return list(VOICES_FALLBACK)

    result = []
    for v in voices:
        sn = v["ShortName"]  # 如 zh-CN-XiaoxiaoNeural
        locale = v["Locale"]  # 如 zh-CN
        gender = v["Gender"]  # Male / Female
        if locale not in ("zh-CN", "zh-TW", "en-US"):
            continue

        # 从 ShortName 里提取 voice 名 (去掉语言前缀和 Neural 后缀)
        parts = sn.split("-")
        raw_name = parts[-1].replace("Neural", "")

        # 生成友好名
        if locale.startswith("zh"):
            cn_name = _VOICE_NAME_MAP.get(raw_name, raw_name)
            region = "大陆" if locale == "zh-CN" else "台湾"
            g = "女" if gender == "Female" else "男"
            label = f"{cn_name} ({g}·{region})"
        else:
            en_name = _VOICE_EN_MAP.get(raw_name, raw_name)
            g = "女" if gender == "Female" else "男"
            label = f"{en_name} ({g}·美式)"

        result.append((label, sn))

    # 按 locale + gender 分组排序: 大陆女 → 大陆男 → 台湾女 → 台湾男 → 英文女 → 英文男
    locale_order = {"zh-CN": 0, "zh-TW": 1, "en-US": 2}
    gender_order = {"Female": 0, "Male": 1}
    def sort_key(item):
        sn = item[1]
        locale = sn.split("-")[0] + "-" + sn.split("-")[1] if sn.count("-") >= 2 else sn
        g = "Female" if "Neural" in sn else "Male"
        # 从完整 voice 信息再取一次, 简化: 直接 sn 前缀判断 locale
        if sn.startswith("zh-CN"):
            lkey = 0
        elif sn.startswith("zh-TW"):
            lkey = 1
        else:
            lkey = 2
        # 性别从 item 里再查一次
        for v in voices:
            if v["ShortName"] == sn:
                gkey = 0 if v["Gender"] == "Female" else 1
                return (lkey, gkey, item[0])
        return (lkey, 0, item[0])

    result.sort(key=sort_key)
    return result if result else list(VOICES_FALLBACK)

# 提示文字样式
TIP_FONT = QFont("Microsoft YaHei", 10)
TIP_FONT.setBold(True)


# ============================================================
# OCR 引擎单例 (避免每次选区都重新加载 ONNX 模型)
# ============================================================
_ocr_engine = None

def get_ocr():
    """全局唯一的 RapidOCR 实例, 首次调用时加载模型, 后续复用"""
    global _ocr_engine
    if _ocr_engine is None:
        _ocr_engine = RapidOCR()
    return _ocr_engine


def qimage_to_bgr(img: QImage) -> np.ndarray:
    """将 QImage 转为 rapidocr/OpenCV 需要的 BGR numpy 数组(内存自持, 线程安全)"""
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    w, h = img.width(), img.height()
    stride = img.bytesPerLine()  # QImage 每行有 4 字节对齐的填充, 不一定等于 w*3
    ptr = img.bits()
    ptr.setsize(stride * h)
    raw = np.frombuffer(ptr, dtype=np.uint8).reshape(h, stride)
    # 切掉每行末尾的填充字节
    arr = raw[:, :w * 3].reshape(h, w, 3)
    return np.ascontiguousarray(arr[:, :, ::-1])  # RGB -> BGR


def _is_cjk_char(ch: str) -> bool:
    """中日韩字符判断 (文本智能拼接用)"""
    return ("\u4e00" <= ch <= "\u9fff"
            or "\u3040" <= ch <= "\u30ff"
            or "\uac00" <= ch <= "\ud7af")


_PUNCT_END = set("。！？.,;:!?)]}>」』）】》]")
_PUNCT_START = set("，。！？、,.!?;:([{「『（【《")

def _join_text_pair(a: str, b: str) -> str:
    """智能拼接两段 OCR 文本: CJK 之间不加空格, 英文单词间加空格, 标点后不加"""
    if not a or not b:
        return a + b
    if a[-1] in _PUNCT_END or b[0] in _PUNCT_START:
        return a + b
    if _is_cjk_char(a[-1]) and _is_cjk_char(b[0]):
        return a + b
    return a + " " + b


def _split_text_half(text: str) -> tuple[str, str]:
    """把文本在中间附近按分隔符切成两半 (拆分框时分配文本用)"""
    text = text.strip()
    n = len(text)
    if n < 2:
        return text, ""
    mid = n // 2
    win = max(4, n // 5)
    best, bestd = -1, 10 ** 9
    for i in range(max(1, mid - win), min(n, mid + win + 1)):
        # i 为切点: 前段=text[:i], 后段=text[i:]
        if text[i - 1] in " \t,.;:!?、，。；：！？)]}」』）】》]" or text[i] in "，。！？、,.!?;:([{「『（【《":
            d = abs(i - mid)
            if d < bestd:
                bestd, best = d, i
    if best < 0:
        best = mid
    return text[:best].strip(), text[best:].strip()


# ============================================================
# 离线英汉词典 (ECDICT)
# ============================================================
DICT_DB_PATH = os.path.join(app_dir(), "ecdict.db")

# 简单词形还原规则 (供查词回退使用)
def _lemma_variants(word: str) -> list[str]:
    """生成可能的原型形式, 用于词典未命中时回退查询"""
    w = word.lower()
    variants = [w]
    if len(w) > 3:
        if w.endswith("ies"):
            variants.append(w[:-3] + "y")
        if w.endswith("es"):
            variants.append(w[:-2])
            variants.append(w[:-1])
        if w.endswith("s") and not w.endswith("ss"):
            variants.append(w[:-1])
        if w.endswith("ing"):
            variants.append(w[:-3])
            if len(w) > 5 and w[-4] == w[-5]:
                variants.append(w[:-4])  # running -> run
        if w.endswith("ed"):
            variants.append(w[:-2])
            variants.append(w[:-1])
            if len(w) > 4 and w[-3] == w[-4]:
                variants.append(w[:-3])  # stopped -> stop
        if w.endswith("er"):
            variants.append(w[:-2])
        if w.endswith("est"):
            variants.append(w[:-3])
        if w.endswith("ly"):
            variants.append(w[:-2])
    # 去重并保持顺序
    seen = set()
    out = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


CEDICT_DB_PATH = os.path.join(app_dir(), "cedict.db")
IDIOM_DB_PATH = os.path.join(app_dir(), "idiom.db")

# 简单词形还原规则 (供查词回退使用)
def _lemma_variants(word: str) -> list[str]:
    """生成可能的原型形式, 用于词典未命中时回退查询"""
    w = word.lower()
    variants = [w]
    if len(w) > 3:
        if w.endswith("ies"):
            variants.append(w[:-3] + "y")
        if w.endswith("es"):
            variants.append(w[:-2])
            variants.append(w[:-1])
        if w.endswith("s") and not w.endswith("ss"):
            variants.append(w[:-1])
        if w.endswith("ing"):
            variants.append(w[:-3])
            if len(w) > 5 and w[-4] == w[-5]:
                variants.append(w[:-4])  # running -> run
        if w.endswith("ed"):
            variants.append(w[:-2])
            variants.append(w[:-1])
            if len(w) > 4 and w[-3] == w[-4]:
                variants.append(w[:-3])  # stopped -> stop
        if w.endswith("er"):
            variants.append(w[:-2])
        if w.endswith("est"):
            variants.append(w[:-3])
        if w.endswith("ly"):
            variants.append(w[:-2])
    # 去重并保持顺序
    seen = set()
    out = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


class DictLookup:
    """多词典联合查询: ECDICT(英汉) + cedict(汉英) + idiom(成语) + pypinyin"""

    def __init__(self):
        self._en_conn = None   # ECDICT 英汉
        self._zh_conn = None   # cedict 汉英
        self._idiom_conn = None # idiom 成语
        self._available = False
        self._zh_available = False
        self._idiom_available = False
        self._connect()

    def _connect(self):
        # ECDICT 英汉
        if os.path.exists(DICT_DB_PATH):
            try:
                self._en_conn = sqlite3.connect(DICT_DB_PATH, check_same_thread=False)
                self._available = True
            except Exception:
                self._available = False
        # cedict 汉英
        if os.path.exists(CEDICT_DB_PATH):
            try:
                self._zh_conn = sqlite3.connect(CEDICT_DB_PATH, check_same_thread=False)
                self._zh_available = True
            except Exception:
                self._zh_available = False
        # idiom 成语
        if os.path.exists(IDIOM_DB_PATH):
            try:
                self._idiom_conn = sqlite3.connect(IDIOM_DB_PATH, check_same_thread=False)
                self._idiom_available = True
            except Exception:
                self._idiom_available = False

    @property
    def available(self) -> bool:
        return self._available or self._zh_available or self._idiom_available

    # ---------- 英文查词 (ECDICT) ----------
    def lookup_en(self, word: str) -> dict | None:
        """查单个英文单词, 返回 {word, phonetic, translation} 或 None"""
        if not self._available:
            return None
        word = word.strip()
        if not word:
            return None
        for candidate in _lemma_variants(word):
            row = self._en_conn.execute(
                "SELECT word, phonetic, translation FROM stardict WHERE word=?",
                (candidate,),
            ).fetchone()
            if row and row[2]:
                return {"word": row[0], "phonetic": row[1] or "", "translation": row[2]}
        return None

    # ---------- 中文查词 (cedict) ----------
    def lookup_zh(self, word: str) -> dict | None:
        """查中文词语, 返回 {word, pinyin, trad, defn} 或 None"""
        if not self._zh_available:
            return None
        word = word.strip()
        if not word:
            return None
        row = self._zh_conn.execute(
            "SELECT word, pinyin, trad, defn FROM cedict WHERE word=?",
            (word,),
        ).fetchone()
        if row and row[3]:
            return {"word": row[0], "pinyin": row[1] or "", "trad": row[2] or "", "defn": row[3]}
        return None

    # ---------- 成语查词 ----------
    def lookup_idiom(self, word: str) -> dict | None:
        """查成语, 返回 {word, pinyin, explanation, example, derivation} 或 None"""
        if not self._idiom_available:
            return None
        word = word.strip()
        if not word:
            return None
        row = self._idiom_conn.execute(
            "SELECT word, pinyin, explanation, example, derivation FROM idiom WHERE word=?",
            (word,),
        ).fetchone()
        if row and row[2]:
            return {
                "word": row[0], "pinyin": row[1] or "",
                "explanation": row[2] or "", "example": row[3] or "", "derivation": row[4] or ""
            }
        return None

    # ---------- 综合查词 ----------
    def lookup_text(self, text: str, max_items: int = 40) -> dict:
        """从文本中提取中英文并分别查词, 返回 {'en': [...], 'zh': [...]}"""
        import re
        results_en = []
        results_zh = []
        seen_en = set()
        seen_zh = set()

        # 1. 提取英文单词 (2个字母以上)
        en_words = re.findall(r"[A-Za-z][A-Za-z\-']{1,}", text)
        for w in en_words:
            key = w.lower()
            if key in seen_en:
                continue
            seen_en.add(key)
            item = self.lookup_en(w)
            if item:
                results_en.append(item)
            if len(results_en) >= max_items:
                break

        # 2. 提取中文词语 (2字以上连续中文)
        zh_phrases = re.findall(r"[\u4e00-\u9fff]{2,}", text)
        for phrase in zh_phrases:
            if phrase in seen_zh:
                continue
            seen_zh.add(phrase)
            # 先查成语 (精确匹配)
            idiom = self.lookup_idiom(phrase)
            if idiom:
                results_zh.append({"type": "idiom", **idiom})
                continue
            # 再查 cedict (优先长词)
            found = False
            for i in range(len(phrase), 1, -1):
                for j in range(0, len(phrase) - i + 1):
                    sub = phrase[j:j + i]
                    if sub in seen_zh:
                        continue
                    seen_zh.add(sub)
                    item = self.lookup_zh(sub)
                    if item:
                        results_zh.append({"type": "cedict", **item})
                        found = True
                        break
                if found:
                    break
            if len(results_zh) + len(results_en) >= max_items:
                break

        return {"en": results_en, "zh": results_zh}


# 全局词典实例 (懒加载)
_dict_lookup = None

def get_dict() -> DictLookup:
    global _dict_lookup
    if _dict_lookup is None:
        _dict_lookup = DictLookup()
    return _dict_lookup


def _fix_english_spaces(text: str) -> str:
    """rapidocr 英文模型间歇性丢空格, 在 token 内部恢复空格.

    策略:
      1. 把文本按非字母字符切分, 保留原有标点和已有空格
      2. 对每个纯字母段, 如果长度 > 3 用 wordninja 拆分粘连单词
         (短单词 <= 3 如 "I" "the" "Moai" 等不拆, 避免误拆)
      3. 在标点后补空格(如果后面紧跟字母)
      4. 清理多余空格
    """
    if not _wordninja or not text:
        return text
    # 按非字母字符切分, 捕获组保留分隔符
    parts = re.split(r'([^a-zA-Z]+)', text)
    result = []
    for part in parts:
        if not part:
            continue
        if re.match(r'^[a-zA-Z]+$', part):
            # 全字母段, 长度 > 3 才拆 (短单词不碰)
            if len(part) > 3:
                split = _wordninja.split(part)
                result.append(' '.join(split))
            else:
                result.append(part)
        else:
            result.append(part)
    out = ''.join(result)
    # 标点后补空格: 覆盖中英文标点
    _all_punct = r'([.!?,;:。！？、，；：!?;:])'
    out = re.sub(_all_punct + r'([A-Za-z])', r'\1 \2', out)
    out = re.sub(_all_punct + r'([\u4e00-\u9fff])', r'\1 \2', out)
    # 清理多余空格
    out = re.sub(r' +', ' ', out).strip()
    return out


# ============================================================
# 后台线程: OCR 识别
# ============================================================
class OCRWorker(QThread):
    """在后台线程中执行OCR, 避免阻塞界面"""
    finished = pyqtSignal(str)   # 识别到的文字
    error = pyqtSignal(str)      # 错误信息

    def __init__(self, image_arr: np.ndarray):
        super().__init__()
        # 在主线程已完成 QImage->BGR numpy 转换, 数组自持内存, 线程安全
        self._image_arr = np.ascontiguousarray(image_arr)

    def run(self):
        try:
            arr = self._image_arr
            # 防 native crash: rapidocr 对非法输入会直接 onnxruntime 崩溃
            if arr is None or arr.size == 0:
                self.error.emit("识别区域为空")
                return
            if arr.ndim != 3 or arr.shape[2] != 3:
                self.error.emit(f"图片格式异常: shape={arr.shape}")
                return
            h, w = arr.shape[:2]
            if h < 5 or w < 5:
                self.error.emit(f"识别区域太小 ({w}x{h}), 请框选更大的范围")
                return
            if h > 4000 or w > 4000:
                self.error.emit(f"识别区域过大 ({w}x{h})")
                return

            ocr = get_ocr()  # 复用单例, 不重复加载模型
            # use_cls=False 跳过方向分类, 对常规横排文档可节省约30%推理时间
            # 记录入参尺寸到 crash.log (native crash 时能知道是啥输入崩的)
            _crash_log.write(f"[OCRWorker] input shape={arr.shape} dtype={arr.dtype}\n")
            _crash_log.flush()
            result, _ = ocr(arr, use_cls=False)
            if not result:
                self.finished.emit("")
                return
            # result: [[box, text, score], ...]
            # ===== DEBUG: 打印 rapidocr 原始 tokens =====
            try:
                with open(os.path.join(app_user_dir(), "debug.log"), "a", encoding="utf-8") as _dbg:
                    _dbg.write(f"\n==== OCR raw tokens ({len(result)}) ====\n")
                    for _box, _txt, _sc in result:
                        _fixed = _fix_english_spaces(_txt.strip())
                        _dbg.write(f"  score={_sc:.2f} raw=|{_txt.strip()}| => fixed=|{_fixed}|\n")
            except Exception:
                pass
            # ============================================
            # 先按 y_center 粗排
            items = []
            for box, text, score in result:
                if score <= 0.3 or not text.strip():
                    continue
                # ===== 修复: rapidocr 英文空格丢失 =====
                text = _fix_english_spaces(text.strip())
                ys = [p[1] for p in box]
                xs = [p[0] for p in box]
                yc = sum(ys) / len(ys)
                xc = sum(xs) / len(xs)
                h = max(ys) - min(ys)
                items.append((box, text, score, xc, yc, h))
            if not items:
                self.finished.emit("")
                return
            items.sort(key=lambda it: (it[4], it[3]))  # y_center, x_center

            # 行聚类: 相邻块 y 差 < 平均行高的一半 → 同一行
            avg_h = sum(it[5] for it in items) / len(items)
            row_threshold = max(avg_h * 0.5, 8)  # 至少 8px 防止超小字体误判
            rows = []       # [[item, item, ...], ...]
            cur_row = [items[0]]
            for it in items[1:]:
                if it[4] - cur_row[-1][4] < row_threshold:
                    cur_row.append(it)
                else:
                    rows.append(cur_row)
                    cur_row = [it]
            rows.append(cur_row)
            # 每行内再按 x_center 排序
            for row in rows:
                row.sort(key=lambda it: it[3])

            # 拼接成连贯文本 (跨行间加换行)
            text = ""
            for row in rows:
                row_text = ""
                for it in row:
                    row_text = _join_text_pair(row_text, it[1])
                if text:
                    text += "\n" + row_text
                else:
                    text = row_text
            self.finished.emit(text.strip())
        except Exception as e:
            import traceback
            self.error.emit(f"OCR识别失败: {e}\n{traceback.format_exc()}")


# ============================================================

class _PageOcrWorker(QThread):
    """整页OCR: 返回原始词级框+文本 (供「填充空框」按中心点归属分配文本)"""
    finished = pyqtSignal(list)   # [(QRect, str), ...] 按阅读顺序排好
    error = pyqtSignal(str)

    def __init__(self, image_arr: np.ndarray):
        super().__init__()
        self._image_arr = np.ascontiguousarray(image_arr)

    def run(self):
        try:
            arr = self._image_arr
            if arr is None or arr.size == 0 or arr.ndim != 3 or arr.shape[2] != 3:
                self.error.emit("整页识别: 图片数据异常")
                return
            h, w = arr.shape[:2]
            if h > 6000 or w > 6000:
                self.error.emit(f"图片过大 ({w}x{h})")
                return
            ocr = get_ocr()
            result, _ = ocr(arr, use_cls=False)
            toks = []
            for box, text, score in (result or []):
                if score <= 0.3 or not text.strip():
                    continue
                text = _fix_english_spaces(text.strip())
                if not text:
                    continue
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                x0, y0 = max(0, int(min(xs))), max(0, int(min(ys)))
                x1 = min(w - 1, int(max(xs)))
                y1 = min(h - 1, int(max(ys)))
                if x1 <= x0 or y1 <= y0:
                    continue
                toks.append((QRect(x0, y0, x1 - x0 + 1, y1 - y0 + 1), text))
            # 阅读顺序: y 中心排序, 行内按 x (归属分配时直接顺序拼接)
            toks.sort(key=lambda t: ((t[0].top() + t[0].bottom()) / 2, t[0].center().x()))
            self.finished.emit(toks)
        except Exception as e:
            import traceback
            self.error.emit(f"整页识别失败: {e}\n{traceback.format_exc()}")


# ============================================================
# 后台线程: edge-tts 语音合成
# ============================================================
class TTSWorker(QThread):
    """后台线程: edge-tts 真·流式合成 (AI对话式秒出声).

    一次调用合成整篇文本, 每从 Azure 收到一块音频立即通过 piece_ready 吐出,
    主界面边写入播放器边播放 (边合成边播放); 全部收完后 all_done(True)
    通知主界面把完整 mp3 写入缓存复用.
    """
    piece_ready = pyqtSignal(bytes)   # 一块 mp3 音频数据
    all_done = pyqtSignal(bool)       # True=正常完成  False=用户取消
    error = pyqtSignal(str)
    progress = pyqtSignal(str)        # 重试等提示

    def __init__(self, text: str, voice: str, rate: str, max_retries: int = 1):
        super().__init__()
        self._text = text
        self._voice = voice
        self._rate = rate
        self._cancel = False
        self._max_retries = max_retries

    def cancel(self):
        self._cancel = True

    @staticmethod
    def _preprocess_for_tts(text: str) -> str:
        """朗读文本预处理: 智能去掉不必要的换行, 避免 edge-tts 卡顿.

        规则:
          前一行末尾是句末标点 (.!?。！？!?等) → 保留换行
          前一行末尾不是句末标点 → 去掉换行拼接
            - 末尾是 CJK (中文) → 直接拼, 不加空格 (中文无空格, 避免卡顿)
            - 末尾是字母/英文 → 加空格拼 (英文单词间需要空格)
        """
        if not text:
            return text
        # 统一空白控制符
        text = re.sub(r'[\r\t]+', '', text)
        text = re.sub(r'[ \f\v]+', ' ', text)

        END_PUNCT = set(".!?。！？!?；;：:")

        def _is_cjk(ch: str) -> bool:
            return ("\u4e00" <= ch <= "\u9fff"
                    or "\u3040" <= ch <= "\u30ff"
                    or "\uac00" <= ch <= "\ud7af")

        lines = text.split("\n")
        result: list[str] = []
        for raw_line in lines:
            line = re.sub(r" +", " ", raw_line.strip())
            if not line:
                result.append("")
                continue
            if result:
                prev = result[-1]
                if prev and prev[-1] not in END_PUNCT:
                    # 前一行末尾不是标点 → 决定是否加空格
                    sep = "" if _is_cjk(prev[-1]) else " "
                    result[-1] = prev + sep + line
                    continue
            result.append(line)

        while result and result[-1] == "":
            result.pop()
        return "\n".join(result).strip()

    def run(self):
        # 清洗朗读文本: 智能处理换行, 避免 edge-tts 在换行处卡顿
        text = self._text
        if text:
            text = self._preprocess_for_tts(text)
        self._text = text

        if not self._text:
            self.error.emit("没有可朗读的文字")
            return

        last_error = None
        for attempt in range(self._max_retries + 1):
            if self._cancel:
                self.all_done.emit(False)
                return
            if attempt > 0:
                self.progress.emit("合成失败, 正在重试…")
                # 重试前等 1 秒让 Azure 服务端抖动恢复
                import time; time.sleep(1)

            try:
                communicate = edge_tts.Communicate(
                    self._text, self._voice, rate=self._rate
                )
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self._generate(communicate))
                finally:
                    loop.close()

                if self._cancel:
                    self.all_done.emit(False)
                    return
                self.all_done.emit(True)
                return
            except asyncio.CancelledError:
                # 用户点停止, 立即终止
                self.all_done.emit(False)
                return
            except Exception as e:
                last_error = e
                _crash_log.write(
                    f"\n[TTSWorker] attempt={attempt} voice={self._voice} "
                    f"rate={self._rate} err={e}\n"
                )
                _crash_log.flush()

        self.error.emit(f"语音合成失败: {last_error}")

    async def _generate(self, communicate):
        """边收边吐: 每收到一块音频立即发 piece_ready, 同时检查取消标志"""
        sent = 0
        async for piece in communicate.stream():
            if self._cancel:
                raise asyncio.CancelledError()
            if piece["type"] == "audio":
                data = piece["data"]
                sent += len(data)
                self.piece_ready.emit(data)
        if sent < 100:
            raise RuntimeError("Azure 未返回有效音频数据")


# ============================================================
# 流式音频源: TTSWorker 边吐数据边写入, QMediaPlayer 顺序读取播放
# ============================================================
class _TtsStreamDevice(QIODevice):
    """喂给 QMediaPlayer 的顺序只读设备.

    readData 在媒体解码线程中被调用: 缓冲区没数据时阻塞等待 (绝不能返回空,
    否则会被当作文件结尾提前结束播放); 合成正常结束且数据取完 → 返回空 = EOF.
    """
    def __init__(self):
        super().__init__()
        self._buf = bytearray()
        self._mtx = QMutex()
        self._cond = QWaitCondition()
        self._fin = False      # 合成线程已正常收完
        self._aborted = False  # 用户停止

    def write_data(self, data: bytes):
        self._mtx.lock()
        self._buf += data
        self._cond.wakeAll()
        self._mtx.unlock()

    def finish(self):
        """合成结束 (正常收完或出错), 缓冲播完后自然 EOF"""
        self._mtx.lock()
        self._fin = True
        self._cond.wakeAll()
        self._mtx.unlock()

    def abort(self):
        """用户停止, 立即唤醒等待并结束"""
        self._mtx.lock()
        self._aborted = True
        self._cond.wakeAll()
        self._mtx.unlock()

    def isSequential(self):
        return True

    def bytesAvailable(self):
        self._mtx.lock()
        n = len(self._buf)
        self._mtx.unlock()
        return n + super().bytesAvailable()

    def readData(self, maxlen: int):
        self._mtx.lock()
        while not self._buf and not self._fin and not self._aborted:
            if not self._cond.wait(self._mtx, 3000):
                break  # 超时也跳出, 避免死等 (下一轮会再进来)
        if self._aborted or not self._buf:
            # aborted=用户停止; 缓冲空且 fin=正常播完
            self._mtx.unlock()
            return b""
        out = bytes(self._buf[:maxlen])
        del self._buf[:maxlen]
        self._mtx.unlock()
        return out

    def atEnd(self):
        self._mtx.lock()
        ended = not self._buf and (self._fin or self._aborted)
        self._mtx.unlock()
        return ended


# ============================================================
# 后台线程: 拉取 Azure 实时 voice 列表 (不阻塞窗口显示)
# ============================================================
class _VoiceListThread(QThread):
    done = pyqtSignal(list)

    def run(self):
        self.done.emit(_load_voices_from_azure())


# ============================================================
# 后台预热线程: 启动时加载OCR模型, 不阻塞界面
# ============================================================
class _WarmerThread(QThread):
    done = pyqtSignal()

    def run(self):
        try:
            get_ocr()
        except Exception:
            pass
        self.done.emit()


# ============================================================
# 画布组件: 显示图片 + 拖拽选区
# ============================================================
class ImageCanvas(QWidget):
    """显示图片编辑画布: 拖拽框选/移动/缩放/右键删除点读框, 显示编号徽章

    全屏点读为独立窗口 (FullscreenReader), 画布始终为编辑态。
    """
    regionSelected = pyqtSignal(QImage, QRect)   # 裁剪出的原图区域 + 原图坐标 rect
    selectionChanged = pyqtSignal(bool)  # 是否有选区
    autoFrameClicked = pyqtSignal(int)   # 点读: 点中了第几个自动框 (index 对应 _auto_frames)
    framesChanged = pyqtSignal()         # 自动框被移动/缩放/删除

    def __init__(self, parent=None):
        super().__init__(parent)
        self._orig: QPixmap | None = None   # 原始(高分辨率)图片
        self._scaled: QPixmap | None = None # 缩放后用于显示的图片
        self._zoom = 1.0
        self._sel_orig_p1: QPoint | None = None  # 选区起点 (原图坐标)
        self._sel_orig_p2: QPoint | None = None  # 选区终点 (原图坐标)
        self._dragging = False
        self._loading = False  # 是否正在识别(用于显示等待提示)
        self._loading_text = "正在识别文字…"
        self._resizing = False  # 程序内部缩放时屏蔽 resizeEvent 的自动适配
        # ---- 点读框图层 ----
        self._auto_frames: list[dict] = []  # [{"rect": QRect(原图坐标), "text": str}, ...]
        self._active_frame = -1             # 当前选中框 index (-1 无)
        self._frame_drag: dict | None = None  # {"mode","idx","start_orig","start_rect","moved","corner","press_canvas"}

        self.setMouseTracking(True)
        self.setMinimumSize(200, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAutoFillBackground(True)
        bg = self.palette()
        bg.setColor(self.backgroundRole(), QColor("#3a3a3a"))
        self.setPalette(bg)

    # ---------- 外部调用 ----------
    def setImage(self, pixmap: QPixmap):
        self._orig = pixmap
        self._sel_orig_p1 = self._sel_orig_p2 = None
        self._dragging = False
        # 注意: 点读框不在这里清除 — 框归属 (文件,页), 由
        # _load_file_history 统一保存/恢复 (翻页先换框再换图)
        self._zoom = -1.0  # 哨兵值: 标记需要适配窗口
        self._min_zoom = 0.0  # 适应窗口的最小缩放 (0=未设置)
        # 立即尝试一次, 然后在事件循环空闲时再重试 (确保布局完成)
        self._fit_to_viewport()
        QTimer.singleShot(0, self._deferred_fit)
        QTimer.singleShot(150, self._deferred_fit)
        QTimer.singleShot(500, self._deferred_fit)
        self.selectionChanged.emit(False)
        self.update()

    def _deferred_fit(self):
        """延迟适配: 视口尺寸就绪后执行 fit"""
        if self._zoom >= 0:
            return  # 已完成适配, 不再重试
        self._fit_to_viewport()

    def clearImage(self):
        self._orig = None
        self._scaled = None
        self._sel_orig_p1 = self._sel_orig_p2 = None
        self.update()

    def zoomIn(self):
        self._set_zoom(self._zoom * 1.25)

    def zoomOut(self):
        self._set_zoom(self._zoom / 1.25)

    def _get_scroll_area(self) -> QScrollArea | None:
        """向上遍历父级链找到 QScrollArea (self.parent() 是 viewport, 不是 QScrollArea)"""
        p = self.parent()
        while p:
            if isinstance(p, QScrollArea):
                return p
            p = p.parent()
        return None

    def _get_viewport_size(self) -> QSize:
        """获取滚动区视口尺寸 (排除滚动条)"""
        sa = self._get_scroll_area()
        if sa:
            return sa.viewport().size()
        # 没有 scroll area 时, parent 可能是 viewport widget
        p = self.parent()
        if p:
            return p.size()
        return self.size()

    def zoomFit(self):
        self._fit_to_viewport()

    def clear_highlight(self):
        """清除当前选区线 (切图/清空时用)"""
        if self._sel_orig_p1 is not None or self._sel_orig_p2 is not None:
            self._sel_orig_p1 = self._sel_orig_p2 = None
            self.selectionChanged.emit(False)
            self.update()

    def setLoading(self, loading: bool, text: str = "正在识别文字…"):
        """设置识别等待状态, 绘制半透明遮罩提示 (text 可自定义提示文案)"""
        self._loading = loading
        self._loading_text = text
        self.update()

    # ---------- 点读框图层 ----------
    def set_auto_frames(self, frames: list):
        """设置点读框 frames: [(QRect(原图坐标), text), ...]"""
        self._auto_frames = [{"rect": QRect(r), "text": t} for r, t in frames]
        self._active_frame = -1
        self._frame_drag = None
        self.update()

    def clear_auto_frames(self):
        if self._auto_frames:
            self._auto_frames = []
            self._active_frame = -1
            self._frame_drag = None
            self.framesChanged.emit()
            self.update()

    def set_active_frame(self, idx: int):
        """外部设置当前高亮框 (点读框列表点击时联动画布)"""
        if 0 <= idx < len(self._auto_frames):
            self._active_frame = idx
            self.update()

    def _frame_at(self, orig_pt: QPoint, pad: bool = False) -> int:
        """点读 hit-test: 返回包含该点的框 index, 重叠时取面积最小者; 无则 -1

        pad=True: 判定时框四周外扩 1/4 行高 (全屏点读容错, 轻微点偏也能命中)
        """
        hits = []
        for i, fr in enumerate(self._auto_frames):
            r = fr["rect"]
            if pad:
                m = max(6, r.height() // 4)
                r = r.adjusted(-m, -m, m, m)
            if r.contains(orig_pt):
                hits.append(i)
        if not hits:
            return -1
        hits.sort(key=lambda i: (self._auto_frames[i]["rect"].width()
                                 * self._auto_frames[i]["rect"].height()))
        return hits[0]

    def _resize_handle_at(self, canvas_pt: QPoint):
        """检测是否点中 active 框的 8 向缩放柄 (四角+四边中点); 返回 (idx, corner) 或 None

        corner: 'tl'/'tr'/'bl'/'br'/'n'/'s'/'e'/'w'
        """
        if not (0 <= self._active_frame < len(self._auto_frames)):
            return None
        crect = self._orig_rect_to_canvas(self._auto_frames[self._active_frame]["rect"])
        if crect is None:
            return None
        s = 9  # 画布像素命中半径
        cx = (crect.left() + crect.right()) // 2
        cy = (crect.top() + crect.bottom()) // 2
        handles = {
            "tl": crect.topLeft(), "tr": crect.topRight(),
            "bl": crect.bottomLeft(), "br": crect.bottomRight(),
            "n": QPoint(cx, crect.top()), "s": QPoint(cx, crect.bottom()),
            "w": QPoint(crect.left(), cy), "e": QPoint(crect.right(), cy),
        }
        for name, pt in handles.items():
            if abs(canvas_pt.x() - pt.x()) <= s and abs(canvas_pt.y() - pt.y()) <= s:
                return self._active_frame, name
        return None

    def _orig_rect_to_canvas(self, rect: QRect) -> QRect | None:
        """原图坐标矩形 → 画布坐标矩形"""
        if not self._orig or self._zoom <= 0:
            return None
        img_rect = self._image_rect()
        x = img_rect.x() + int(rect.x() * self._zoom)
        y = img_rect.y() + int(rect.y() * self._zoom)
        w = int(rect.width() * self._zoom)
        h = int(rect.height() * self._zoom)
        if w < 1 or h < 1:
            return None
        return QRect(x, y, w, h)

    def wheelEvent(self, event):
        """鼠标滚轮缩放, 以光标位置为中心"""
        if not self._orig or self._scaled is None:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        old_zoom = self._zoom
        factor = 1.15 if delta > 0 else 1 / 1.15
        new_zoom = max(0.1, min(8.0, old_zoom * factor))
        if abs(new_zoom - old_zoom) < 1e-6:
            return

        sa = self._get_scroll_area()
        if sa:
            # 光标对应 canvas 内容坐标 = 视口位置 + 滚动偏移
            viewport_pos = event.position().toPoint()
            hb = sa.horizontalScrollBar()
            vb = sa.verticalScrollBar()
            canvas_x = viewport_pos.x() + hb.value()
            canvas_y = viewport_pos.y() + vb.value()
            # 图片在 canvas 上的居中偏移
            img_off_x = (self.width() - self._scaled.width()) // 2
            img_off_y = (self.height() - self._scaled.height()) // 2
            # 换算为原图坐标 (canvas坐标 - 图片偏移) / 缩放
            fx = (canvas_x - img_off_x) / old_zoom
            fy = (canvas_y - img_off_y) / old_zoom
            self._set_zoom(new_zoom)
            # 缩放后图片新的居中偏移
            new_off_x = (self.width() - self._scaled.width()) // 2
            new_off_y = (self.height() - self._scaled.height()) // 2
            # 让原图同一像素点仍位于光标之下
            hb.setValue(int(fx * new_zoom + new_off_x - viewport_pos.x()))
            vb.setValue(int(fy * new_zoom + new_off_y - viewport_pos.y()))
        else:
            self._set_zoom(new_zoom)
        event.accept()

    # ---------- 缩放 ----------
    def _fit_to_viewport(self):
        if not self._orig:
            return
        # 取滚动区视口尺寸 (排除滚动条/边框)
        vp = self._get_viewport_size()
        # 视口尚未就绪时跳过, 等延迟重试
        if vp.width() < 50 or vp.height() < 50:
            return
        sw = vp.width() / self._orig.width()
        sh = vp.height() / self._orig.height()
        # 适应窗口: 取较小比例完整显示 (高度或宽度与视口一致)
        z = min(sw, sh)
        self._zoom = max(0.0, z)  # 清除哨兵值 -1
        self._min_zoom = z  # 记录适应窗口的缩放值, 作为缩小下限
        self._set_zoom(z)

    def _set_zoom(self, z: float):
        if not self._orig:
            return
        if self._min_zoom > 0:
            z = max(z, self._min_zoom)
        z = max(0.1, min(z, 8.0))
        self._zoom = z
        new_w = int(self._orig.width() * z)
        new_h = int(self._orig.height() * z)
        if new_w < 1 or new_h < 1:
            return
        self._scaled = self._orig.scaled(
            new_w, new_h, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        # 动态切换 resizable: 图小→自动填满视口; 图大→手动设尺寸出滚动条
        sa = self._get_scroll_area()
        vp = self._get_viewport_size()
        if new_w <= vp.width() and new_h <= vp.height():
            if sa:
                sa.setWidgetResizable(True)
        else:
            if sa:
                sa.setWidgetResizable(False)
            self._resizing = True
            self.resize(new_w, new_h)
            self._resizing = False
        # ★ 选区存的是原图坐标, 缩放不需要任何转换! 这里只 update 就行
        self._dragging = False
        self.update()

    def sizeHint(self):
        # 返回实际 canvas 尺寸 (不是仅图片尺寸), 确保 scroll area 布局正确
        if self.width() > 0 and self.height() > 0:
            return QSize(self.width(), self.height())
        if self._scaled:
            return self._scaled.size()
        return QSize(600, 400)

    # ---------- 绘制 ----------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        # 背景
        p.fillRect(self.rect(), QColor("#3a3a3a"))
        if not self._scaled:
            if not self._orig:
                p.setPen(QColor("#888"))
                p.setFont(QFont("Microsoft YaHei", 12))
                p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                           "请点击上方按钮导入图片或PDF文件\n\n在画面上拖拽鼠标, 框选要朗读的区域")
            return
        x = (self.width() - self._scaled.width()) // 2
        y = (self.height() - self._scaled.height()) // 2
        p.drawPixmap(x, y, self._scaled)

        # 绘制选区 (仅边框, 不遮挡选区外内容)
        rect = self._selection_rect()
        if rect and rect.width() > 2 and rect.height() > 2:
            # 选区边框
            pen = QPen(QColor("#00aaff"), 2)
            p.setPen(pen)
            p.drawRect(rect)
            # 四角标记
            self._draw_handles(p, rect)

        # ---- 点读框图层: 青绿边框 + 编号徽章 + active框手柄 (编辑态) ----
        if self._auto_frames and self._zoom > 0:
            # 徽章随缩放自适应: 缩小时变小, 限制在 10~15px
            bh = max(10, min(15, int(round(14 * self._zoom))))
            badge_font = QFont()
            badge_font.setPixelSize(max(7, bh - 4))
            badge_font.setBold(True)
            for i, fr in enumerate(self._auto_frames):
                crect = self._orig_rect_to_canvas(fr["rect"])
                if crect is None:
                    continue
                active = (i == self._active_frame)
                if active:
                    p.fillRect(crect, QColor(22, 119, 255, 30))
                    p.setPen(QPen(QColor("#1677ff"), 2))
                    p.drawRect(crect)
                    # 8向缩放柄 (四角大柄 + 四边中点小柄)
                    p.setBrush(QColor("#1677ff"))
                    p.setPen(Qt.PenStyle.NoPen)
                    s = 7
                    for cx, cy in [(crect.left(), crect.top()), (crect.right(), crect.top()),
                                   (crect.left(), crect.bottom()), (crect.right(), crect.bottom())]:
                        p.drawRect(cx - s // 2, cy - s // 2, s, s)
                    s2 = 5
                    cx = (crect.left() + crect.right()) // 2
                    cy = (crect.top() + crect.bottom()) // 2
                    for cx2, cy2 in [(cx, crect.top()), (cx, crect.bottom()),
                                     (crect.left(), cy), (crect.right(), cy)]:
                        p.drawRect(cx2 - s2 // 2, cy2 - s2 // 2, s2, s2)
                    p.setBrush(Qt.BrushStyle.NoBrush)
                else:
                    p.fillRect(crect, QColor(19, 194, 194, 16))
                    p.setPen(QPen(QColor(19, 194, 194, 170), 1))
                    p.drawRect(crect)
                # 序号徽章 (左上角, 小号不遮挡内容)
                label = str(i + 1)
                p.setFont(badge_font)
                fm = p.fontMetrics()
                bw = fm.horizontalAdvance(label) + 4
                by = max(0, crect.top() - bh)
                p.fillRect(crect.left(), by, bw, bh,
                           QColor("#1677ff") if active else QColor("#0e9e9e"))
                p.setPen(QColor("#ffffff"))
                p.drawText(QRect(crect.left(), by, bw, bh),
                           Qt.AlignmentFlag.AlignCenter, label)

        # 识别等待提示 (只覆盖图片区域, 不覆盖画布空白区)
        if self._loading:
            img_rect = self._image_rect()
            p.fillRect(img_rect, QColor(0, 0, 0, 140))
            p.setPen(QColor("#ffffff"))
            f = QFont("Microsoft YaHei", 16)
            f.setBold(True)
            p.setFont(f)
            p.drawText(img_rect, Qt.AlignmentFlag.AlignCenter, self._loading_text)

    def _draw_handles(self, p: QPainter, r: QRect):
        p.setBrush(QColor("#00aaff"))
        p.setPen(Qt.PenStyle.NoPen)
        s = 6
        for cx, cy in [
            (r.left(), r.top()), (r.right(), r.top()),
            (r.left(), r.bottom()), (r.right(), r.bottom())
        ]:
            p.drawRect(cx - s // 2, cy - s // 2, s, s)
        # 恢复状态: drawRect 会用当前 brush 填充, 不恢复会导致
        # 后续高亮框/活动点读框被实心蓝涂满而盖住文字
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _selection_rect(self) -> QRect | None:
        """原图坐标选区 → 画布坐标 (paintEvent 直接用来画)"""
        if not self._sel_orig_p1 or not self._sel_orig_p2 or self._zoom <= 0:
            return None
        p1 = self._sel_orig_p1
        p2 = self._sel_orig_p2
        ox = min(p1.x(), p2.x())
        oy = min(p1.y(), p2.y())
        ow = abs(p2.x() - p1.x())
        oh = abs(p2.y() - p1.y())
        if ow < 2 or oh < 2:
            return None
        img_rect = self._image_rect()
        x = img_rect.x() + int(ox * self._zoom)
        y = img_rect.y() + int(oy * self._zoom)
        w = int(ow * self._zoom)
        h = int(oh * self._zoom)
        return QRect(x, y, w, h)

    def _canvas_to_orig(self, canvas_pt: QPoint) -> QPoint:
        """画布坐标 → 原图坐标"""
        img_rect = self._image_rect()
        cx = max(0, canvas_pt.x() - img_rect.x())
        cy = max(0, canvas_pt.y() - img_rect.y())
        ox = max(0, int(cx / self._zoom))
        oy = max(0, int(cy / self._zoom))
        ox = min(ox, self._orig.width() - 1)
        oy = min(oy, self._orig.height() - 1)
        return QPoint(ox, oy)

    # ---------- 鼠标事件 ----------
    def mousePressEvent(self, ev):
        # ---- 右键 → 自动框右键菜单 (删除此框/清除全部) ----
        if ev.button() == Qt.MouseButton.RightButton and self._auto_frames and self._scaled:
            self._show_frame_menu(ev.globalPosition().toPoint(),
                                  self._canvas_to_orig(ev.position().toPoint()))
            return

        if ev.button() != Qt.MouseButton.LeftButton or not self._scaled:
            return
        # 判断点击是否在图片范围内
        img_rect = self._image_rect()
        if not img_rect.contains(ev.position().toPoint()):
            return

        orig_pt = self._canvas_to_orig(ev.position().toPoint())
        press_canvas = ev.position().toPoint()

        # === 自动框交互, 优先级最高 ===
        # 1) active 框四角缩放柄 → 进入缩放拖拽
        handle = self._resize_handle_at(press_canvas)
        if handle:
            idx, corner = handle
            self._frame_drag = {"mode": "resize", "idx": idx, "corner": corner,
                                "start_orig": orig_pt,
                                "start_rect": QRect(self._auto_frames[idx]["rect"]),
                                "moved": True, "press_canvas": press_canvas}
            return
        # 2) 点中某个自动框 → 记录, 松开时按是否移动区分为"点读"或"移框"
        fidx = self._frame_at(orig_pt)
        if fidx >= 0:
            self._active_frame = fidx
            self._frame_drag = {"mode": "move", "idx": fidx,
                                "start_orig": orig_pt,
                                "start_rect": QRect(self._auto_frames[fidx]["rect"]),
                                "moved": False, "press_canvas": press_canvas}
            self.update()
            return
        # 3) 点空白 → 取消 active, 拖拽新建选区框
        self._active_frame = -1

        # === 发起新的拖拽选区 ===
        self._sel_orig_p1 = orig_pt
        self._sel_orig_p2 = orig_pt
        self._dragging = True
        self.update()

    def _show_frame_menu(self, global_pos: QPoint, orig_pt: QPoint):
        """自动框右键菜单: 删除点中的框 / 清除全部框图"""
        menu = QMenu(self)
        idx = self._frame_at(orig_pt)
        act_del = menu.addAction(f"删除此框 (第 {idx + 1} 框)") if idx >= 0 else None
        act_clear = menu.addAction("清除全部框图")
        action = menu.exec(global_pos)
        if action is None:
            return
        if act_del is not None and action == act_del:
            self._auto_frames.pop(idx)
            if self._active_frame == idx:
                self._active_frame = -1
            elif self._active_frame > idx:
                self._active_frame -= 1
            self.framesChanged.emit()
            self.update()
        elif action == act_clear:
            self.clear_auto_frames()

    def mouseMoveEvent(self, ev):
        # ---- 自动框移动/缩放拖拽 ----
        if self._frame_drag:
            d = self._frame_drag
            cur = self._canvas_to_orig(ev.position().toPoint())
            full = QRect(0, 0, self._orig.width(), self._orig.height())
            fr = self._auto_frames[d["idx"]]
            if d["mode"] == "move":
                dist = (ev.position().toPoint() - d["press_canvas"]).manhattanLength()
                if dist > 4:
                    d["moved"] = True
                if d["moved"]:
                    nr = QRect(d["start_rect"]).translated(
                        cur.x() - d["start_orig"].x(), cur.y() - d["start_orig"].y())
                    if nr.left() < 0:
                        nr.moveLeft(0)
                    if nr.top() < 0:
                        nr.moveTop(0)
                    if nr.right() >= full.width():
                        nr.moveRight(full.width() - 1)
                    if nr.bottom() >= full.height():
                        nr.moveBottom(full.height() - 1)
                    fr["rect"] = nr
                    self.update()
            else:  # resize: 按柄拖动对应边/角 (四角+四边中点, 最小 20x14)
                r = QRect(d["start_rect"])
                c = d["corner"]
                if "l" in c or c == "w":
                    r.setLeft(min(cur.x(), r.right() - 20))
                if "r" in c or c == "e":
                    r.setRight(max(cur.x(), r.left() + 20))
                if "t" in c or c == "n":
                    r.setTop(min(cur.y(), r.bottom() - 14))
                if "b" in c or c == "s":
                    r.setBottom(max(cur.y(), r.top() + 14))
                fr["rect"] = r.intersected(full)
                self.update()
            return
        # ---- 悬停光标反馈: 缩放柄/可点框 ----
        if not self._dragging and self._auto_frames and self._scaled:
            h = self._resize_handle_at(ev.position().toPoint())
            if h:
                cursors = {
                    "tl": Qt.CursorShape.SizeFDiagCursor,
                    "br": Qt.CursorShape.SizeFDiagCursor,
                    "tr": Qt.CursorShape.SizeBDiagCursor,
                    "bl": Qt.CursorShape.SizeBDiagCursor,
                    "n": Qt.CursorShape.SizeVerCursor,
                    "s": Qt.CursorShape.SizeVerCursor,
                    "e": Qt.CursorShape.SizeHorCursor,
                    "w": Qt.CursorShape.SizeHorCursor,
                }
                self.setCursor(cursors[h[1]])
            elif self._frame_at(self._canvas_to_orig(ev.position().toPoint())) >= 0:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
        if not self._dragging:
            return
        pt = ev.position().toPoint()
        img_rect = self._image_rect()
        # 限制在图片范围内
        pt.setX(max(img_rect.left(), min(pt.x(), img_rect.right())))
        pt.setY(max(img_rect.top(), min(pt.y(), img_rect.bottom())))
        self._sel_orig_p2 = self._canvas_to_orig(pt)
        self.update()

    def mouseReleaseEvent(self, ev):
        # ---- 自动框拖拽结束 → 未移动则视为点读 ----
        if ev.button() == Qt.MouseButton.LeftButton and self._frame_drag:
            d = self._frame_drag
            self._frame_drag = None
            if d["mode"] == "move" and not d["moved"]:
                self.update()
                self.autoFrameClicked.emit(d["idx"])   # 单击 = 点读该框
            else:
                self.framesChanged.emit()               # 框被移动/缩放
            return
        if ev.button() != Qt.MouseButton.LeftButton or not self._dragging:
            return
        self._dragging = False
        if not self._sel_orig_p1 or not self._sel_orig_p2:
            return
        p1 = self._sel_orig_p1
        p2 = self._sel_orig_p2
        ox1 = min(p1.x(), p2.x())
        oy1 = min(p1.y(), p2.y())
        ow = abs(p2.x() - p1.x())
        oh = abs(p2.y() - p1.y())
        if ow < 1 or oh < 1:
            self._sel_orig_p1 = self._sel_orig_p2 = None
            self.selectionChanged.emit(False)
            self.update()
            return
        # 画布坐标的最小阈值 (防手抖)
        canvas_rect = self._selection_rect()
        if not canvas_rect or canvas_rect.width() < 5 or canvas_rect.height() < 5:
            self._sel_orig_p1 = self._sel_orig_p2 = None
            self.selectionChanged.emit(False)
            self.update()
            return
        orig_rect = QRect(ox1, oy1, ow, oh)
        self.selectionChanged.emit(True)
        self.update()
        # 直接用原图坐标裁剪
        try:
            cropped = self._orig.toImage().copy(ox1, oy1, ow, oh)
            if not cropped.isNull():
                self.regionSelected.emit(cropped, orig_rect)
        except Exception:
            pass

    def _image_rect(self) -> QRect:
        if not self._scaled:
            return QRect(0, 0, 0, 0)
        x = (self.width() - self._scaled.width()) // 2
        y = (self.height() - self._scaled.height()) // 2
        return QRect(x, y, self._scaled.width(), self._scaled.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 程序内部缩放时跳过, 避免与 _set_zoom 的 resize 形成递归
        if self._resizing or self._dragging:
            return
        # 窗口尺寸变化且处于适应模式时重新适配
        if self._orig and self._zoom < 0:
            self._fit_to_viewport()


class DeepLearnDialog(QWidget):
    """深度学习弹窗: 左边原文, 右边上词典释义 + 下整段翻译 同时显示"""

    def __init__(self, parent=None, original_text: str = "", dict_html: str = "",
                 voice: str = "", rate: str = "+0%"):
        super().__init__(parent)
        self.setWindowTitle("深度学习")
        self.resize(960, 680)
        self.setWindowFlag(Qt.WindowType.Window, True)  # 作为独立窗口
        self._voice = voice or (VOICES[0][1] if VOICES else "")
        self._rate = rate
        self._src_text = original_text or ""

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # 顶部说明
        tip = QLabel("深度学习模式: 左侧原文, 上方逐词词典释义, 下方整段翻译 (自动)")
        tip.setStyleSheet("color:#666; font-size:12px;")
        root.addWidget(tip)

        # 左右分栏
        spl = QSplitter(Qt.Orientation.Horizontal)
        spl.setChildrenCollapsible(False)

        # 左: 原文
        left_box = QGroupBox("原文")
        ll = QVBoxLayout(left_box)
        self.txt_src = QTextEdit()
        self.txt_src.setReadOnly(True)
        self.txt_src.setPlainText(self._src_text or "(空)")
        self.txt_src.setStyleSheet("background:#fafafa; border:1px solid #ddd;")
        self.txt_src.setFont(QFont("Microsoft YaHei", 13))
        ll.addWidget(self.txt_src)
        spl.addWidget(left_box)

        # 右: 上下两个面板 (纵向 splitter)
        right_vspl = QSplitter(Qt.Orientation.Vertical)
        right_vspl.setChildrenCollapsible(False)

        # 右上: 词典释义
        dict_box = QGroupBox("📖 词典释义")
        dl = QVBoxLayout(dict_box)
        self.txt_dict = QTextEdit()
        self.txt_dict.setReadOnly(True)
        self.txt_dict.setHtml(dict_html or '<span style="color:#999;">未查询到释义</span>')
        self.txt_dict.setStyleSheet("background:#fffdf5; border:1px solid #ddd;")
        dl.addWidget(self.txt_dict)
        right_vspl.addWidget(dict_box)

        # 右下: 整段翻译 (自动加载, 先显示 loading)
        trans_box = QGroupBox("🔄 整段翻译")
        tl = QVBoxLayout(trans_box)
        self.txt_translate = QTextEdit()
        self.txt_translate.setReadOnly(True)
        self.txt_translate.setHtml(
            '<div style="color:#1677ff; font-size:15px; padding:20px; text-align:center;">'
            '翻译中, 请稍候…</div>'
        )
        self.txt_translate.setStyleSheet("background:#f9f0ff; border:1px solid #ddd;")
        tl.addWidget(self.txt_translate)
        right_vspl.addWidget(trans_box)

        right_vspl.setStretchFactor(0, 3)
        right_vspl.setStretchFactor(1, 2)
        right_vspl.setSizes([300, 220])
        spl.addWidget(right_vspl)

        spl.setStretchFactor(0, 1)
        spl.setStretchFactor(1, 2)
        spl.setSizes([400, 560])
        root.addWidget(spl, 1)

        # 底部按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_read = QPushButton("▶ 朗读原文")
        self.btn_close = QPushButton("关闭")
        self.btn_close.setDefault(True)
        btn_row.addWidget(self.btn_read)
        btn_row.addWidget(self.btn_close)
        root.addLayout(btn_row)

        self.btn_close.clicked.connect(self.close)
        self.btn_read.clicked.connect(self._on_read)

        # worker 引用 (防止被 GC)
        self._tts_worker = None
        self._player = None
        self._audio_out = None
        self._translate_worker = None
        self._playing_stream = False
        self._dead_workers: list = []       # 已取消的旧 worker, 防止线程未结束被回收

        # 自动发起整段翻译 (如果有原文)
        if self._src_text.strip():
            self._start_translate()

    def _start_translate(self):
        """自动发起整段翻译"""
        self._translate_worker = TranslateWorker(self._src_text)
        self._translate_worker.done.connect(self._on_translate_done)
        self._translate_worker.error.connect(self._on_translate_error)
        self._translate_worker.start()

    def _on_read(self):
        text = self.txt_src.toPlainText().strip()
        if not text:
            return
        # 停掉上一次的播放/合成
        self._stop_read()
        # 真流式: 一次调用整篇文本, 音频数据边到边播 (单一播放器, 零切换)
        self._audio_bytes = bytearray()
        self._player_started = False
        self._playing_stream = True
        self.btn_read.setEnabled(False)
        self._tts_worker = TTSWorker(text, self._voice, self._rate)
        self._tts_worker.piece_ready.connect(self._on_piece)
        self._tts_worker.all_done.connect(self._on_read_done)
        self._tts_worker.error.connect(self._on_read_error)
        self._tts_worker.start()

    def _stop_read(self):
        self._playing_stream = False
        dev = getattr(self, "tts_device", None)
        if dev is not None:
            dev.abort()
            self.tts_device = None
        if self._audio_out is not None:
            try:
                self._audio_out.deleteLater()
            except RuntimeError:
                pass
        if self._player is not None:
            try:
                self._player.stop()
                self._player.deleteLater()
            except RuntimeError:
                pass
        self._player = None
        self._audio_out = None
        if self._tts_worker is not None:
            if self._tts_worker.isRunning():
                self._tts_worker.cancel()
                for sig in (self._tts_worker.piece_ready, self._tts_worker.all_done,
                            self._tts_worker.error):
                    try:
                        sig.disconnect()
                    except TypeError:
                        pass
                self._dead_workers.append(self._tts_worker)
            self._tts_worker = None

    def _on_piece(self, data: bytes):
        if not self._playing_stream:
            return
        self._audio_bytes += data
        if not self._player_started:
            worker_done = (self._tts_worker is None or not self._tts_worker.isRunning())
            if len(self._audio_bytes) >= 12288 or worker_done:
                self._start_stream_play()
        else:
            self.tts_device.write_data(data)

    def _start_stream_play(self):
        if self._player_started or not self._playing_stream:
            return
        self._player_started = True
        self.tts_device = _TtsStreamDevice()
        self.tts_device.write_data(bytes(self._audio_bytes))
        if self._audio_out is not None:
            try:
                self._audio_out.deleteLater()
            except RuntimeError:
                pass
        if self._player is not None:
            try:
                self._player.stop()
                self._player.deleteLater()
            except RuntimeError:
                pass
        self._player = QMediaPlayer()
        self._audio_out = QAudioOutput()
        self._player.setAudioOutput(self._audio_out)
        self._audio_out.setVolume(0.9)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.setSourceDevice(self.tts_device)
        self._player.play()

    def _on_read_done(self, ok: bool):
        if not self._playing_stream:
            return
        if getattr(self, "tts_device", None) is not None:
            self.tts_device.finish()
        if not self._player_started and self._audio_bytes:
            self._start_stream_play()

    def _on_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia and self._playing_stream:
            self._playing_stream = False
            self.btn_read.setEnabled(True)

    def _on_read_error(self, msg: str):
        if getattr(self, "tts_device", None) is not None:
            self.tts_device.finish()
        if not self._player_started:
            self._playing_stream = False
            self.btn_read.setEnabled(True)

    def closeEvent(self, ev):
        self._stop_read()

    def _on_translate_done(self, html: str):
        """翻译完成, 直接显示到下方翻译框"""
        self.txt_translate.setHtml(html)

    def _on_translate_error(self, msg: str):
        self.txt_translate.setHtml(
            f'<div style="color:#f5222d; font-size:13px; padding:10px;">⚠ {msg}<br><br>'
            f'请检查网络连接。翻译功能需要联网。</div>'
        )


class TranslateWorker(QThread):
    """后台线程调用翻译 API 做整段翻译 (优先 MyMemory, 国内可用)"""
    done = pyqtSignal(str)       # 翻译结果 HTML
    error = pyqtSignal(str)      # 错误信息

    def __init__(self, text: str, target: str = "zh"):
        super().__init__()
        self._text = text
        self._target = target  # "zh" 或 "en"

    def run(self):
        import urllib.request, urllib.parse, json
        try:
            # 自动检测: 中文字符占比 > 30% → 英译汉, 否则汉译英
            cjk_count = sum(1 for c in self._text if '\u4e00' <= c <= '\u9fff')
            if cjk_count > len(self._text) * 0.3:
                sl, tl = "zh", "en"
                src_label = "汉译英"
            else:
                sl, tl = "en", "zh-CN"
                src_label = "英译汉"

            translated = self._translate_mymemory(sl, tl)

            html = (
                f'<div style="font-size:13px; color:#722ed1; margin-bottom:10px; '
                f'padding:6px 10px; background:#f9f0ff; border-radius:4px; '
                f'font-weight:bold;">🔄 {src_label} (整段翻译)</div>'
                f'<div style="font-size:15px; line-height:1.8; color:#333;">{translated}</div>'
            )
            self.done.emit(html)
        except Exception as e:
            self.error.emit(f"翻译失败: {e}")

    def _translate_mymemory(self, sl: str, tl: str) -> str:
        """MyMemory 免费翻译 API, 免 key, 国内可用"""
        import urllib.request, urllib.parse, json
        # MyMemory 有 5000 字符上限, 分段
        text = self._text
        if len(text) > 4500:
            text = text[:4500] + "\n...(部分内容已截断)"
        url = "https://api.mymemory.translated.net/get"
        params = urllib.parse.urlencode({
            "q": text,
            "langpair": f"{sl}|{tl}",
        })
        req = urllib.request.Request(
            f"{url}?{params}",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("responseData", {}).get("translatedText", "")


class PdfThumbWorker(QThread):
    """后台线程渲染 PDF 所有页的缩略图 (低分辨率, 快速)

    逐页渲染并通过 page_done 信号发回, 主线程实时填充列表,
    finished_ok 表示所有页渲染完成。
    """
    page_done = pyqtSignal(int, QPixmap)   # (页索引, 缩略图)
    finished_ok = pyqtSignal()

    def __init__(self, pdf_path: str, page_count: int):
        super().__init__()
        self._path = pdf_path
        self._count = page_count

    def run(self):
        try:
            import fitz as _fitz
            doc = _fitz.open(self._path)
            # 中等分辨率: 约 100 DPI 的缩略图, 兼顾清晰度和速度
            matrix = _fitz.Matrix(0.7, 0.7)
            for i in range(self._count):
                page = doc[i]
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                qimg = QImage(pix.samples, pix.width, pix.height, pix.stride,
                              QImage.Format.Format_RGB888).copy()
                pm = QPixmap.fromImage(qimg)
                # 再缩到列表的 iconSize
                icon_size = QSize(96, 128)
                if pm.width() > icon_size.width() or pm.height() > icon_size.height():
                    pm = pm.scaled(icon_size, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
                self.page_done.emit(i, pm)
            doc.close()
            self.finished_ok.emit()
        except Exception:
            pass  # 后台线程吞掉异常, 避免弹窗崩溃


# ============================================================
# 全屏点读: 只显示当前页图片 (不显示框), 点图中内容即朗读
# ============================================================
class FullscreenReader(QWidget):
    """全屏点读窗口

    - 只显示当前页图片, 不显示点读框/编号
    - 点图中已框的内容 → 朗读该框 (点读体验, 框位置不可见)
    - 整页连读: 逐框朗读, 当前正在读的框给一层浅色高亮跟随
    - 底部工具条: 上一页 / 下一页 / 整页连读 / 语速 / 朗读人 / 退出
    """

    def __init__(self, main_win: "MainWindow"):
        super().__init__(None)
        self._main = main_win
        self.setWindowTitle("全屏点读")
        self.setStyleSheet("QWidget { background:#000; }")

        self._src: QPixmap | None = None   # 当前页源图 (identity 变了才重新缩放)
        self._scaled: QPixmap | None = None
        self._img_rect = QRect()           # 图片在窗口中的显示区域
        self._scale = 1.0
        self._last_active = -2             # 上次绘制的朗读框 (变化才重绘)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addStretch(1)                 # 图片区域 (paintEvent 画)

        # ---- 底部工具条 ----
        bar = QFrame()
        bar.setStyleSheet(
            "QFrame { background:#1c1c1c; border:none; }"
            "QPushButton { background:#2d2d2d; color:#eee; border:none;"
            "  border-radius:4px; padding:8px 18px; font-size:14px; }"
            "QPushButton:hover { background:#3a3a3a; }"
            "QLabel { color:#bbb; font-size:13px; background:transparent; }"
            "QSlider::groove:horizontal { height:4px; background:#444; border-radius:2px; }"
            "QSlider::handle:horizontal { width:14px; margin:-6px 0;"
            "  border-radius:7px; background:#1677ff; }"
            "QComboBox { background:#2d2d2d; color:#fff; border:none;"
            "  border-radius:4px; padding:6px 12px; font-size:14px; }"
            "QComboBox::drop-down { border:none; width:20px; }"
            "QComboBox::down-arrow { width:0; height:0;"
            "  border-left:5px solid transparent; border-right:5px solid transparent;"
            "  border-top:6px solid #aaa; }"
            "QComboBox QAbstractItemView { background:#ffffff; color:#111; outline:none; }"
            "QComboBox QAbstractItemView::item { min-height:26px; padding:2px 8px; }"
            "QComboBox QAbstractItemView::item:selected { background:#1677ff; color:#fff; }"
        )
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(16, 10, 16, 10)
        bl.setSpacing(10)

        self.btn_prev = QPushButton("◀ 上一页")
        self.btn_next = QPushButton("下一页 ▶")
        self.btn_read_page = QPushButton("🔊 整页连读")
        self.btn_exit = QPushButton("✕ 退出全屏点读")
        self.btn_exit.setStyleSheet(
            "QPushButton { background:#c62828; color:#fff; }"
            "QPushButton:hover { background:#e53935; }")
        self.sld_rate = QSlider(Qt.Orientation.Horizontal)
        self.sld_rate.setRange(-50, 50)
        self.sld_rate.setFixedWidth(140)
        self.lbl_rate = QLabel("语速 0%")
        self.cmb_voice = QComboBox()
        self.cmb_voice.setMinimumWidth(170)
        self.lbl_voice = QLabel("朗读人:")

        bl.addWidget(self.btn_prev)
        bl.addWidget(self.btn_next)
        bl.addSpacing(16)
        bl.addWidget(self.btn_read_page)
        bl.addSpacing(16)
        bl.addWidget(self.lbl_rate)
        bl.addWidget(self.sld_rate)
        bl.addSpacing(12)
        bl.addWidget(self.lbl_voice)
        bl.addWidget(self.cmb_voice)
        bl.addStretch(1)
        bl.addWidget(self.btn_exit)
        root.addWidget(bar)
        bar.setCursor(Qt.CursorShape.ArrowCursor)   # 工具条保持系统箭头

        self.setCursor(Qt.CursorShape.ArrowCursor)  # 使用系统光标

        # 信号
        self.btn_prev.clicked.connect(lambda: self._nav(-1))
        self.btn_next.clicked.connect(lambda: self._nav(1))
        self.btn_read_page.clicked.connect(self._main._on_read_page)
        self.btn_exit.clicked.connect(self.exit_fullscreen)
        self.sld_rate.valueChanged.connect(self._on_rate_changed)
        self.cmb_voice.currentIndexChanged.connect(self._on_voice_changed)

        # 轮询: 朗读框变化 / 页面切换 → 重绘
        self._poll = QTimer(self)
        self._poll.setInterval(120)
        self._poll.timeout.connect(self._poll_refresh)

    # ---------- 打开 / 退出 ----------
    def open_and_show(self):
        """同步主窗口设置后全屏显示"""
        m = self._main
        # 语速 / 朗读人: 与主窗口同步
        self.sld_rate.blockSignals(True)
        self.sld_rate.setValue(m.sld_rate.value())
        self.sld_rate.blockSignals(False)
        self.lbl_rate.setText(f"语速 {m.sld_rate.value():+d}%")
        self.cmb_voice.blockSignals(True)
        self.cmb_voice.clear()
        for i in range(m.cmb_voice.count()):
            self.cmb_voice.addItem(m.cmb_voice.itemText(i), m.cmb_voice.itemData(i))
        self.cmb_voice.setCurrentIndex(m.cmb_voice.currentIndex())
        self.cmb_voice.blockSignals(False)
        self._invalidate()
        self.showFullScreen()
        self._poll.start()
        self.setFocus()

    def exit_fullscreen(self):
        self._poll.stop()
        self.hide()
        m = self._main
        m.show()
        m.activateWindow()
        self.close()

    # ---------- 页面切换 ----------
    def _nav(self, delta: int):
        m = self._main
        if m._pdf_doc:
            (m._on_next_page if delta > 0 else m._on_prev_page)()
        elif m._thumb_mode == "folder" and m._thumb_files:
            try:
                i = m._thumb_files.index(m._thumb_path)
            except ValueError:
                i = 0
            ni = i + delta
            if 0 <= ni < len(m._thumb_files):
                m._thumb_path = m._thumb_files[ni]
                pix = QPixmap(m._thumb_path)
                if not pix.isNull():
                    m.canvas.setImage(pix)
                    m._switch_file(m._thumb_path)

    def _poll_refresh(self):
        m = self._main
        if m.canvas._orig is not self._src:
            self._invalidate()
        a = m.canvas._active_frame
        if a != self._last_active:
            self.update()

    def _invalidate(self):
        """源图变化 / 窗口尺寸变化 → 丢弃缩放缓存, 重绘"""
        self._src = None
        self._scaled = None
        self.update()

    # ---------- 绘制 ----------
    def _ensure_scaled(self):
        src = self._main.canvas._orig
        if src is None or src.isNull():
            self._scaled = None
            return
        if src is self._src and self._scaled is not None \
                and self._img_rect.size() == self._fit_size(src):
            return
        self._src = src
        dst = self._fit_size(src)
        self._scaled = src.scaled(dst, Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
        x = (self.width() - self._scaled.width()) // 2
        y = max(0, (self.height() - 64 - self._scaled.height()) // 2)
        self._img_rect = QRect(x, y, self._scaled.width(), self._scaled.height())
        self._scale = self._scaled.width() / src.width()

    def _fit_size(self, src: QPixmap) -> QSize:
        avail_w = max(100, self.width() - 40)
        avail_h = max(100, self.height() - 64 - 20)   # 扣除底部工具条
        return src.size().scaled(QSize(avail_w, avail_h),
                                 Qt.AspectRatioMode.KeepAspectRatio)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor("#000"))
        self._ensure_scaled()
        if not self._scaled:
            p.setPen(QColor("#888"))
            p.setFont(QFont("Microsoft YaHei", 14))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "没有可显示的页面, 请先导入图片或PDF")
            return
        p.drawPixmap(self._img_rect.topLeft(), self._scaled)
        # 当前正在朗读的框: 浅色高亮跟随 (不画框线和编号)
        m = self._main
        idx = m.canvas._active_frame
        self._last_active = idx
        frames = m.canvas._auto_frames
        if 0 <= idx < len(frames) and self._scale > 0:
            r = frames[idx]["rect"]
            hx = self._img_rect.x() + int(r.x() * self._scale)
            hy = self._img_rect.y() + int(r.y() * self._scale)
            hw = int(r.width() * self._scale)
            hh = int(r.height() * self._scale)
            p.fillRect(QRect(hx, hy, hw, hh), QColor(22, 119, 255, 60))
            # 点中句子显示边框, 让孩子看清正在读哪句
            p.setPen(QPen(QColor("#1677ff"), 3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRect(hx, hy, hw, hh))

    # ---------- 交互 ----------
    def mousePressEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        m = self._main
        pos = ev.position().toPoint()
        if not self._img_rect.contains(pos) or self._scale <= 0:
            return
        ox = int((pos.x() - self._img_rect.x()) / self._scale)
        oy = int((pos.y() - self._img_rect.y()) / self._scale)
        ox = max(0, min(ox, (m.canvas._orig.width() - 1) if m.canvas._orig else 0))
        oy = max(0, min(oy, (m.canvas._orig.height() - 1) if m.canvas._orig else 0))
        idx = m.canvas._frame_at(QPoint(ox, oy), pad=True)
        if idx >= 0:
            m._on_frame_click(idx)   # 点读: 朗读该框 (高亮由 active_frame 驱动)

    def keyPressEvent(self, ev):
        k = ev.key()
        if k == Qt.Key.Key_Escape:
            self.exit_fullscreen()
        elif k == Qt.Key.Key_Left:
            self._nav(-1)
        elif k == Qt.Key.Key_Right:
            self._nav(1)
        elif k == Qt.Key.Key_Space:
            self._main._on_read()
        else:
            super().keyPressEvent(ev)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._invalidate()

    def _on_rate_changed(self, v: int):
        self.lbl_rate.setText(f"语速 {v:+d}%")
        self._main.sld_rate.setValue(v)   # 同步主窗口 (自动保存设置)

    def _on_voice_changed(self, idx: int):
        if 0 <= idx < self._main.cmb_voice.count():
            self._main.cmb_voice.setCurrentIndex(idx)   # 同步主窗口


# ============================================================
# 主窗口
# ============================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1180, 780)

        # 窗口图标
        icon_path = os.path.join(app_dir(), "图标.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self._pdf_doc = None
        self._pdf_pages = 0
        self._cur_page = 0
        self._ocr_worker: OCRWorker | None = None
        self._tts_worker: TTSWorker | None = None
        self._cur_text = ""
        self._cur_audio = ""
        self._last_sel_rect: QRect | None = None  # 最近一次框选的原图坐标 (供框加入点读框)
        self._auto_read = True
        self._suppress_edit = False  # 程序内部设置文本时屏蔽 textChanged

        # 缩略图面板
        self._thumb_mode: str = "none"  # 'none' / 'pdf' / 'folder'
        self._thumb_files: list[str] = []   # 图片模式: 当前文件夹所有图片完整路径
        self._thumb_path: str = ""          # 当前打开的文件完整路径 (用于高亮当前项)
        self._thumb_worker = None           # PDF 缩略图后台渲染线程
        self._cur_file_path: str = ""       # 当前打开的图片/PDF 完整路径
        self._fill_worker = None            # 2.0: 填充空框的后台整页OCR线程
        self._suppress_frame_tbl = False    # 重建点读框表格时屏蔽 itemChanged
        self._playing_stream = False        # 流式朗读播放中
        self._dead_workers: list = []       # 已取消的旧 worker, 防止线程未结束被回收
        self.tts_device = None              # 流式音频源设备
        self._cur_voice = ""
        self._cur_rate = "+0%"
        # 整页连读: 逐框朗读队列 (识别结果只显示当前行, 画布/列表同步高亮)
        self._seq_mode = False
        self._seq_frames: list[int] = []
        self._seq_total = 0

        # 设置持久化 (发音人/语速/自动朗读)
        self._settings = QSettings("ChuangXuan", APP_TITLE)

        self._build_ui()
        self._wire_signals()
        self._load_settings()
        self._update_page_label()
        # 后台拉取 Azure 实时 voice 列表, 到达后刷新下拉框 (窗口先显示 fallback)
        self._voice_thread = _VoiceListThread()
        self._voice_thread.done.connect(self._on_voices_loaded)
        self._voice_thread.start()
        self._status("就绪。正在后台预热OCR引擎…")
        # 后台预热OCR引擎, 使首次选区识别无需等待模型加载
        self._warmer = _WarmerThread()
        self._warmer.done.connect(lambda: self._status("就绪。请导入图片或PDF文件, 在画面上拖拽选择区域即可朗读。"))
        self._warmer.start()
        # 延迟恢复上次打开的文件 (等 OCR 预热和 UI 初始化)
        QTimer.singleShot(800, self._auto_reopen_last)

    # ----------------------------------------------------------
    # 构建界面
    # ----------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ---- 工具栏行1: 文件操作 ----
        row1 = QHBoxLayout()
        row1.setSpacing(6)
        self.btn_open_img = QPushButton("导入图片")
        self.btn_open_img.setToolTip("支持 JPG / PNG / BMP / WEBP 等格式")
        self.btn_open_pdf = QPushButton("导入PDF")
        self.btn_open_pdf.setToolTip("导入 PDF 文件, 可逐页浏览选择")

        row1.addWidget(self.btn_open_img)
        row1.addWidget(self.btn_open_pdf)
        row1.addStretch(1)
        self.btn_toggle_thumbs = QPushButton("🖼 缩略图")
        self.btn_toggle_thumbs.setCheckable(True)
        self.btn_toggle_thumbs.setChecked(True)
        self.btn_toggle_thumbs.setToolTip("显示/隐藏左侧缩略图面板")
        row1.addWidget(self.btn_toggle_thumbs)
        self.btn_about = QPushButton("关于")
        row1.addWidget(self.btn_about)
        root.addLayout(row1)

        # ---- 工具栏行2: 语音设置 ----
        row2 = QHBoxLayout()
        row2.setSpacing(6)
        self.cmb_voice = QComboBox()
        # 先填充 fallback 列表 (Azure 实时列表由后台线程拉取后刷新, 不阻塞窗口显示)
        for name, vid in VOICES_FALLBACK:
            self.cmb_voice.addItem(name, vid)
        # 缺省选中美式英语 (Aria); 无保存设置时不再缺省中文
        _def_idx = self.cmb_voice.findData(DEFAULT_VOICE_ID)
        if _def_idx < 0:
            for _i in range(self.cmb_voice.count()):
                if str(self.cmb_voice.itemData(_i) or "").startswith("en-US"):
                    _def_idx = _i
                    break
        self.cmb_voice.setCurrentIndex(max(0, _def_idx))
        self.cmb_voice.setMinimumWidth(180)
        lbl_rate = QLabel("语速:")
        self.sld_rate = QSlider(Qt.Orientation.Horizontal)
        self.sld_rate.setRange(-50, 50)
        self.sld_rate.setValue(0)
        self.sld_rate.setFixedWidth(140)
        self.sld_rate.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.sld_rate.setTickInterval(10)
        self.lbl_rate_val = QLabel("0%")
        self.lbl_rate_val.setFixedWidth(40)
        self.chk_auto = QCheckBox("自动朗读")
        self.chk_auto.setChecked(True)

        row2.addWidget(QLabel("发音人:"))
        row2.addWidget(self.cmb_voice)
        row2.addSpacing(8)
        row2.addWidget(lbl_rate)
        row2.addWidget(self.sld_rate)
        row2.addWidget(self.lbl_rate_val)
        row2.addSpacing(8)
        row2.addWidget(self.chk_auto)
        row2.addSpacing(16)
        # 全屏点读: 干净大图给孩子点读
        self.btn_fullscreen = QPushButton("🖥 全屏点读")
        self.btn_fullscreen.setStyleSheet(
            "QPushButton { font-weight: bold; padding: 5px 16px; }")
        self.btn_fullscreen.setToolTip(
            "进入全屏点读: 只显示当前页图片, 点图中内容即朗读。\n"
            "含 上一页/下一页/整页连读/语速/朗读人, Esc 或「退出全屏点读」返回编辑。")
        row2.addWidget(self.btn_fullscreen)
        row2.addStretch(1)
        root.addLayout(row2)

        # ---- 分割条: 缩略图 / 左边画布 / 右边面板 ----
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        # 最左: 缩略图面板
        self._thumb_box = QGroupBox("缩略图")
        tbl = QVBoxLayout(self._thumb_box)
        tbl.setContentsMargins(4, 4, 4, 4)
        self.lst_thumbs = QListWidget()
        self.lst_thumbs.setViewMode(QListWidget.ViewMode.ListMode)  # 竖向列表, 每行一个
        self.lst_thumbs.setIconSize(QSize(96, 128))
        self.lst_thumbs.setMovement(QListWidget.Movement.Static)
        self.lst_thumbs.setSpacing(4)
        self.lst_thumbs.setResizeMode(QListWidget.ResizeMode.Fixed)
        self.lst_thumbs.setMinimumWidth(160)
        self.lst_thumbs.setMaximumWidth(240)
        self.lst_thumbs.setWordWrap(True)
        self.lst_thumbs.setStyleSheet(
            "QListWidget { background:#fafafa; border:1px solid #ddd; border-radius:4px; }"
            "QListWidget::item { padding:4px 6px; border-bottom:1px solid #eee; }"
            "QListWidget::item:selected { background:#cceeff; border:1px solid #1677ff; border-radius:3px; color:#003366; }"
            "QListWidget::item:selected:!active { background:#e8f4ff; }"
            "QListWidget::icon { padding-right:6px; }"
        )
        tbl.addWidget(self.lst_thumbs)
        splitter.addWidget(self._thumb_box)

        # 左: 画布 + 底部翻页栏 (放在同一个容器内, 按钮只在画布宽度内居中)
        left_container = QWidget()
        lcl = QVBoxLayout(left_container)
        lcl.setContentsMargins(0, 0, 0, 0)
        lcl.setSpacing(6)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)  # 默认让 canvas 自动填满视口
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas = ImageCanvas()
        self.scroll.setWidget(self.canvas)
        lcl.addWidget(self.scroll, 1)

        # ---- 翻页栏 (在左侧容器内, 只占画布宽度) ----
        row3 = QHBoxLayout()
        row3.setSpacing(6)
        self.btn_zoom_fit = QPushButton("适应窗口显示")
        self.btn_prev = QPushButton("◀ 上一页")
        self.btn_next = QPushButton("下一页 ▶")
        self.lbl_page = QLabel("页: 0 / 0")
        self.lbl_page.setMinimumWidth(100)
        self.lbl_page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_page.setStyleSheet("color: #0a6; font-weight: bold;")
        self.edt_jump = QLineEdit()
        self.edt_jump.setFixedWidth(60)
        self.edt_jump.setPlaceholderText("页码")
        self.btn_jump = QPushButton("跳转")
        self.btn_jump.setFixedWidth(60)
        # 整页连读 (放画布正下方, 离目标最近)
        self.btn_read_page = QPushButton("🔊 整页连读")
        self.btn_read_page.setEnabled(False)
        self.btn_read_page.setToolTip(
            "按框的阅读顺序连续朗读整页内容 (需先框选生成点读框)。")

        row3.addStretch(1)
        row3.addWidget(self.btn_read_page)
        row3.addSpacing(12)
        row3.addWidget(self.btn_zoom_fit)
        row3.addSpacing(16)
        row3.addWidget(self.btn_prev)
        row3.addWidget(self.lbl_page)
        row3.addWidget(self.btn_next)
        row3.addSpacing(12)
        row3.addWidget(QLabel("跳至:"))
        row3.addWidget(self.edt_jump)
        row3.addWidget(self.btn_jump)
        row3.addStretch(1)
        lcl.addLayout(row3)

        splitter.addWidget(left_container)

        # 右: 面板
        panel = QWidget()
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(6)

        # 识别结果区 (较小空间, 词典跳查已改为弹窗)
        grp_result = QGroupBox("识别结果")
        grl = QVBoxLayout(grp_result)
        self.txt_result = QTextEdit()
        self.txt_result.setReadOnly(False)
        self.txt_result.setPlaceholderText("选择区域后, 识别到的文字会显示在这里…")
        self.txt_result.setMinimumHeight(100)
        self.txt_result.setReadOnly(True)  # 防止误触改内容, 用「✏️ 编辑」按钮弹出编辑
        # 大字体: 识别结果
        result_font = QFont("Microsoft YaHei", 13)
        self.txt_result.setFont(result_font)
        grl.addWidget(self.txt_result, 1)
        btn_row = QHBoxLayout()
        self.btn_read = QPushButton("▶ 朗读")
        self.btn_read.setEnabled(False)
        self.btn_stop = QPushButton("■ 停止")
        self.btn_copy = QPushButton("复制")
        self.btn_edit = QPushButton("✏️ 编辑")
        self.btn_edit.setToolTip("弹出编辑框修改识别结果")
        self.btn_learn = QPushButton("📚 深度学习")
        self.btn_learn.setToolTip("弹出深度学习对话框, 左侧原文, 右侧词典释义")
        self.btn_clear = QPushButton("清空")
        btn_row.addWidget(self.btn_read)
        btn_row.addWidget(self.btn_stop)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_edit)
        btn_row.addWidget(self.btn_learn)
        btn_row.addWidget(self.btn_copy)
        btn_row.addWidget(self.btn_clear)
        grl.addLayout(btn_row)
        pl.addWidget(grp_result, 1)

        # 点读框列表 (与画布双向联动; 单击定位 / 双击朗读 / 双击文本改字)
        grp_frames = QGroupBox("点读框")
        grp_frames.setToolTip(
            "单击行: 画布定位该框 · 双击行: 朗读 · 双击文本: 修改文字\n"
            "Ctrl/Shift 多选后可合并/删除; 快捷键 Ctrl+E拆分 Ctrl+Q合并 Ctrl+D删除 Ctrl+F填充")
        gfl = QVBoxLayout(grp_frames)
        self.tbl_frames = QTableWidget(0, 2)
        self.tbl_frames.setHorizontalHeaderLabels(["框", "文本"])
        self.tbl_frames.verticalHeader().setVisible(False)
        self.tbl_frames.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.tbl_frames.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.tbl_frames.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked)
        self.tbl_frames.setWordWrap(True)  # 长文本自动换行, 完整显示不截断
        self.tbl_frames.setSortingEnabled(False)
        hh = self.tbl_frames.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.tbl_frames.setColumnWidth(0, 44)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setHighlightSections(False)
        self.tbl_frames.setStyleSheet(
            "QTableWidget { background:#fafafa; border:1px solid #ddd; }"
            "QTableWidget::item:selected { background:#cceeff; color:#003366; }"
        )
        gfl.addWidget(self.tbl_frames)
        tbl_btns = QHBoxLayout()
        self.btn_frame_del = QPushButton("🗑 删除")
        self.btn_frame_del.setToolTip("删除列表中选中的点读框 (Ctrl/Shift多选, 或按 Delete 键)")
        self.btn_frame_split = QPushButton("✂ 拆分")
        self.btn_frame_split.setToolTip(
            "把选中的 1 个框一分为二: 高>80 竖切, 否则横切 (金手指 Ctrl+E 风格)")
        self.btn_frame_merge = QPushButton("🔗 合并")
        self.btn_frame_merge.setToolTip(
            "把 Ctrl 多选的 2 个及以上点读框合并为一个 (取并集矩形, 文本按阅读顺序拼接)")
        self.btn_frame_ins_up = QPushButton("⬆ 上插框")
        self.btn_frame_ins_up.setToolTip("在选中框上方插入一个新框 (新框文本为空)")
        self.btn_frame_ins_dn = QPushButton("⬇ 下插框")
        self.btn_frame_ins_dn.setToolTip("在选中框下方插入一个新框 (新框文本为空)")
        self.btn_frame_fill = QPushButton("📥 填充空框")
        self.btn_frame_fill.setToolTip(
            "整页识别一次, 把文字按位置分配给空文本的框 (拆分/插框后用来补文字)")
        self.btn_frame_clear = QPushButton("清空全部")
        self.btn_frame_clear.setToolTip("清除本页全部点读框")
        for b in (self.btn_frame_del, self.btn_frame_split, self.btn_frame_merge,
                  self.btn_frame_ins_up, self.btn_frame_ins_dn, self.btn_frame_fill):
            tbl_btns.addWidget(b)
        tbl_btns.addStretch(1)
        tbl_btns.addWidget(self.btn_frame_clear)
        gfl.addLayout(tbl_btns)
        pl.addWidget(grp_frames, 3)
        self.grp_frames = grp_frames  # 保存引用

        splitter.addWidget(panel)
        splitter.setStretchFactor(0, 0)   # 缩略图面板: 固定宽度 (min/max 限制)
        splitter.setStretchFactor(1, 3)   # 画布容器
        splitter.setStretchFactor(2, 2)   # 右侧面板
        splitter.setSizes([200, 720, 440])
        self.splitter = splitter  # 保存引用: 宽度变化时让点读框行高自适应
        root.addWidget(splitter, 1)

        # ---- 醒目提示条 ----
        tip = QFrame()
        tip.setStyleSheet(
            "QFrame { background: #fff3cd; border: 1px solid #ffe69c;"
            " border-radius: 4px; }"
            "QLabel { color: #c47800; }"
        )
        tip_l = QHBoxLayout(tip)
        tip_l.setContentsMargins(10, 4, 10, 4)
        lbl_tip = QLabel("两步即用: ① 导入图片/PDF, 在画面上拖拽框选要读的段落 (可框多个); "
                         "② 点「🖥 全屏点读」, 点图中内容即朗读。框的位置和文字会自动保存, 下次打开直接用。")
        lbl_tip.setFont(TIP_FONT)
        lbl_tip.setWordWrap(True)
        tip_l.addWidget(lbl_tip)
        root.addWidget(tip)

        # ---- 状态栏 ----
        sb = QStatusBar()
        self.setStatusBar(sb)
        self._lbl_status = QLabel()
        sb.addWidget(self._lbl_status, 1)

    # ----------------------------------------------------------
    # 信号连接
    # ----------------------------------------------------------
    def _wire_signals(self):
        self.btn_open_img.clicked.connect(self._on_open_image)
        self.btn_open_pdf.clicked.connect(self._on_open_pdf)
        self.btn_read_page.clicked.connect(self._on_read_page)
        self.btn_about.clicked.connect(self._on_about)
        self.btn_prev.clicked.connect(self._on_prev_page)
        self.btn_next.clicked.connect(self._on_next_page)
        self.btn_jump.clicked.connect(self._on_jump_page)
        self.edt_jump.returnPressed.connect(self._on_jump_page)
        self.btn_zoom_fit.clicked.connect(self.canvas.zoomFit)
        self.sld_rate.valueChanged.connect(
            lambda v: self.lbl_rate_val.setText(f"{v:+d}%")
        )
        self.chk_auto.toggled.connect(self._on_auto_toggled)
        self.btn_read.clicked.connect(self._on_read)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_copy.clicked.connect(self._on_copy)
        self.btn_clear.clicked.connect(self._on_clear)
        self.btn_edit.clicked.connect(self._on_edit)
        self.btn_learn.clicked.connect(self._on_deep_learn)
        self.btn_toggle_thumbs.toggled.connect(self._on_toggle_thumbs)
        self.lst_thumbs.itemClicked.connect(self._on_thumb_click)
        # 全屏点读
        self.btn_fullscreen.clicked.connect(self._on_fullscreen)
        # 面板宽度变化时, 点读框行高按换行文本自适应
        self.splitter.splitterMoved.connect(
            lambda *_: self.tbl_frames.resizeRowsToContents())

        # 设置变化时自动保存
        self.cmb_voice.currentIndexChanged.connect(self._save_settings)
        self.sld_rate.valueChanged.connect(self._save_settings)
        self.chk_auto.toggled.connect(self._save_settings)

        self.canvas.regionSelected.connect(self._on_region_selected)
        self.canvas.selectionChanged.connect(
            lambda has: self.btn_read.setEnabled(has or bool(self._cur_text))
        )
        # 点框朗读 (画布单击框 → 朗读; 框图/点读两种模式都走这里)
        self.canvas.autoFrameClicked.connect(self._on_frame_click)
        # 2.0: 框增删改时同步「整页连读」按钮 + 点读框列表
        self.canvas.framesChanged.connect(self._on_frames_changed)
        # 2.0: 点读框列表 ↔ 画布双向联动
        self.tbl_frames.cellClicked.connect(self._on_frame_tbl_click)
        self.tbl_frames.cellDoubleClicked.connect(self._on_frame_tbl_double)
        self.tbl_frames.itemChanged.connect(self._on_frame_tbl_changed)
        # 2.0: 点读框编辑 (金手指风格: 删除/拆分/合并/插框/填充/清空)
        self.btn_frame_del.clicked.connect(self._delete_selected_frames)
        self.btn_frame_split.clicked.connect(self._split_selected_frame)
        self.btn_frame_merge.clicked.connect(self._merge_selected_frames)
        self.btn_frame_ins_up.clicked.connect(lambda: self._insert_frame("above"))
        self.btn_frame_ins_dn.clicked.connect(lambda: self._insert_frame("below"))
        self.btn_frame_fill.clicked.connect(self._fill_empty_frames)
        self.btn_frame_clear.clicked.connect(self.canvas.clear_auto_frames)
        del_sc = QShortcut(QKeySequence.StandardKey.Delete, self.tbl_frames)
        del_sc.activated.connect(self._delete_selected_frames)

        # 快捷键 (仅主窗口激活时生效, 避免全屏点读时被抢按键)
        for seq, slot in [
            ("Ctrl+O", self._on_open_image),
            ("Ctrl+P", self._on_open_pdf),
            ("Space",   self._on_read),
            ("Esc",     self._on_stop),
            ("Left",    self._on_prev_page),
            ("Right",   self._on_next_page),
            # 金手指风格框编辑快捷键
            ("Ctrl+E",  self._split_selected_frame),
            ("Ctrl+Q",  self._merge_selected_frames),
            ("Ctrl+D",  self._delete_selected_frames),
            ("Ctrl+F",  self._fill_empty_frames),
        ]:
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(slot)

    # ----------------------------------------------------------
    # 文件导入
    # ----------------------------------------------------------
    def _on_open_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片文件 (*.jpg *.jpeg *.png *.bmp *.webp *.tif *.tiff)"
        )
        if not path:
            return
        self._pdf_doc = None
        self._pdf_pages = 0
        self._cur_page = 0
        pix = QPixmap(path)
        if pix.isNull():
            self._status(f"无法加载图片: {path}")
            return
        self.canvas.setImage(pix)
        self._update_page_label()
        self._build_folder_thumbs(path)
        self._switch_file(path)  # 保存旧历史 + 加载新文件历史
        # 切换文件时也清空识别结果和停止朗读
        self._stop_playback()
        self._suppress_edit = True
        try:
            self.txt_result.clear()
        finally:
            self._suppress_edit = False
        self._cur_text = ""
        self.btn_read.setEnabled(False)
        self._status(f"已加载图片: {Path(path).name}")

    def _on_open_pdf(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择PDF文件", "", "PDF文件 (*.pdf)")
        if not path:
            return
        try:
            doc = fitz.open(path)
        except Exception as e:
            self._status(f"打开PDF失败: {e}")
            return
        self._pdf_doc = doc
        self._pdf_pages = len(doc)
        self._cur_page = 0
        self._render_pdf_page()
        self._update_page_label()
        self._build_pdf_thumbs(path)
        self._switch_file(path)  # 保存旧历史 + 加载新文件历史
        self._status(f"已加载PDF: {Path(path).name}  共 {self._pdf_pages} 页")

    def _render_pdf_page(self):
        if not self._pdf_doc:
            return
        page = self._pdf_doc[self._cur_page]
        # 高DPI渲染以保证OCR质量
        zoom = fitz.Matrix(2.0, 2.0)  # 约 144 DPI
        pix = page.get_pixmap(matrix=zoom, alpha=False)
        fmt = QImage.Format.Format_RGB888
        # 立即深拷贝, 避免引用 pix.samples 缓冲区失效
        qimg = QImage(pix.samples, pix.width, pix.height, pix.stride, fmt).copy()
        qpix = QPixmap.fromImage(qimg)
        self.canvas.setImage(qpix)
        # 翻页时清空识别结果和停止朗读 (换页=新一页的上下文)
        self._stop_playback()
        self._suppress_edit = True
        try:
            self.txt_result.clear()
        finally:
            self._suppress_edit = False
        self._cur_text = ""
        self.btn_read.setEnabled(False)

    def _on_prev_page(self):
        if not self._pdf_doc or self._cur_page <= 0:
            return
        new_page = self._cur_page - 1
        self._switch_page_history(new_page)
        self._render_pdf_page()
        self._update_page_label()
        self._sync_thumb_current()
        self._status(f"第 {self._cur_page + 1} / {self._pdf_pages} 页")

    def _on_next_page(self):
        if not self._pdf_doc or self._cur_page >= self._pdf_pages - 1:
            return
        new_page = self._cur_page + 1
        self._switch_page_history(new_page)
        self._render_pdf_page()
        self._update_page_label()
        self._sync_thumb_current()
        self._status(f"第 {self._cur_page + 1} / {self._pdf_pages} 页")

    def _on_jump_page(self):
        if not self._pdf_doc:
            self.edt_jump.clear()
            return
        text = self.edt_jump.text().strip()
        if not text.isdigit():
            self.edt_jump.selectAll()
            self.edt_jump.setFocus()
            return
        target = int(text)
        if 1 <= target <= self._pdf_pages:
            self._switch_page_history(target - 1)
            self._render_pdf_page()
            self._update_page_label()
            self._sync_thumb_current()
            self.edt_jump.clear()
            self._status(f"已跳转至第 {target} 页")
        else:
            self._status(f"页码超出范围 (1 - {self._pdf_pages})")

    def _update_page_label(self):
        if self._pdf_doc:
            self.lbl_page.setText(f"页: {self._cur_page + 1} / {self._pdf_pages}")
        else:
            self.lbl_page.setText("页: -")

    # ----------------------------------------------------------
    # 缩略图面板
    # ----------------------------------------------------------
    def _build_pdf_thumbs(self, pdf_path: str):
        """加载 PDF 后在后台渲染每页缩略图"""
        self._thumb_mode = "pdf"
        self._thumb_path = pdf_path
        self.lst_thumbs.clear()
        # 先填充占位, 让用户看到有多少页
        for i in range(self._pdf_pages):
            it = QListWidgetItem(f"第 {i + 1} 页")
            it.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.lst_thumbs.addItem(it)
        self.lst_thumbs.setCurrentRow(self._cur_page)
        self.lst_thumbs.scrollToItem(self.lst_thumbs.currentItem())
        # 后台线程渲染
        if self._thumb_worker and self._thumb_worker.isRunning():
            self._thumb_worker.quit()
            self._thumb_worker.wait()
        self._thumb_worker = PdfThumbWorker(pdf_path, self._pdf_pages)
        self._thumb_worker.page_done.connect(self._on_pdf_thumb_page)
        self._thumb_worker.finished_ok.connect(self._on_pdf_thumbs_ready)
        self._thumb_worker.start()

    def _on_pdf_thumb_page(self, idx: int, pixmap: QPixmap):
        """单页 PDF 缩略图渲染完成"""
        it = self.lst_thumbs.item(idx)
        if it is not None:
            it.setIcon(QIcon(pixmap))

    def _on_pdf_thumbs_ready(self):
        pass  # 所有缩略图已逐页填充完成, 无需额外动作

    def _build_folder_thumbs(self, img_path: str):
        """扫描同目录所有图片, 按名称排序后显示"""
        self._thumb_mode = "folder"
        self._thumb_path = img_path
        self.lst_thumbs.clear()
        folder = os.path.dirname(os.path.abspath(img_path))
        exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tif", ".tiff"}
        try:
            all_files = [
                os.path.join(folder, f) for f in os.listdir(folder)
                if os.path.splitext(f)[1].lower() in exts
            ]
        except Exception:
            all_files = [img_path]
        # 按文件名自然排序 (普通字母数字顺序)
        all_files.sort(key=lambda p: os.path.basename(p).lower())
        self._thumb_files = all_files
        # 逐张加载缩略图
        thumb_size = self.lst_thumbs.iconSize()
        for f in all_files:
            it = QListWidgetItem(os.path.basename(f)[:20])
            it.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            pm = QPixmap(f)
            if not pm.isNull():
                sc = pm.scaled(thumb_size, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
                it.setIcon(QIcon(sc))
            it.setData(Qt.ItemDataRole.UserRole, f)
            self.lst_thumbs.addItem(it)
        # 高亮当前文件
        cur_idx = all_files.index(os.path.abspath(img_path)) if os.path.abspath(img_path) in all_files else 0
        self.lst_thumbs.setCurrentRow(cur_idx)
        self.lst_thumbs.scrollToItem(self.lst_thumbs.currentItem())

    def _on_thumb_click(self, item: QListWidgetItem):
        """点击缩略图: PDF 切页 / 图片切文件"""
        row = self.lst_thumbs.row(item)
        if self._thumb_mode == "pdf" and self._pdf_doc:
            if 0 <= row < self._pdf_pages and row != self._cur_page:
                self._switch_page_history(row)
                self._render_pdf_page()
                self._update_page_label()
                self.canvas.clear_highlight()
                self._status(f"第 {self._cur_page + 1} / {self._pdf_pages} 页")
        elif self._thumb_mode == "folder":
            path = item.data(Qt.ItemDataRole.UserRole)
            if path and path != self._thumb_path:
                self._thumb_path = path
                self._pdf_doc = None
                self._pdf_pages = 0
                self._cur_page = 0
                pix = QPixmap(path)
                if pix.isNull():
                    self._status(f"无法加载图片: {os.path.basename(path)}")
                    return
                self.canvas.setImage(pix)
                self._update_page_label()
                self._switch_file(path)  # 保存旧图点读框 + 加载新图点读框
                self.canvas.clear_highlight()
                self._status(f"已加载图片: {os.path.basename(path)}")

    def _sync_thumb_current(self):
        """翻页后同步缩略图面板的高亮 (只在 PDF 模式下有效)"""
        if self._thumb_mode == "pdf" and self._pdf_doc:
            self.lst_thumbs.setCurrentRow(self._cur_page)
            self.lst_thumbs.scrollToItem(self.lst_thumbs.currentItem())

    def _on_toggle_thumbs(self, checked: bool):
        """显示/隐藏左侧缩略图面板"""
        self._thumb_box.setVisible(checked)

    # ----------------------------------------------------------
    # 区域选中 -> OCR
    # ----------------------------------------------------------
    # ----------------------------------------------------------
    # 全屏点读
    # ----------------------------------------------------------
    def _on_fullscreen(self):
        """进入全屏点读: 干净大图, 点图中内容即朗读"""
        if not self.canvas._orig:
            self._status("请先导入图片或PDF, 再进入全屏点读。")
            return
        if getattr(self, "_fs_reader", None) is None:
            self._fs_reader = FullscreenReader(self)
        self.hide()
        self._fs_reader.open_and_show()

    def _on_frames_changed(self):
        """点读框增删/清除/移动/缩放 → 同步按钮与列表 + 立即持久化"""
        self._sync_read_page_btn()
        self._rebuild_frame_table()
        self._save_current_file_history()

    def _sync_read_page_btn(self):
        """「整页连读」按钮: 有点读框才可用"""
        self.btn_read_page.setEnabled(bool(self.canvas._auto_frames))

    # ----------------------------------------------------------
    # 2.0: 点读框列表 (金手指风格表格, 与画布双向联动)
    # ----------------------------------------------------------
    def _rebuild_frame_table(self):
        """从画布的自动框数据重建点读框列表"""
        frames = self.canvas._auto_frames
        self._suppress_frame_tbl = True
        try:
            self.tbl_frames.setRowCount(len(frames))
            for i, fr in enumerate(frames):
                # 框号列 (只读, 居中)
                it_num = QTableWidgetItem(str(i + 1))
                it_num.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                it_num.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                it_num.setData(Qt.ItemDataRole.UserRole, i)   # 挂框序号
                self.tbl_frames.setItem(i, 0, it_num)
                # 文本列 (可双击编辑, 改完连读/点读都生效)
                it_txt = QTableWidgetItem(fr["text"])
                it_txt.setToolTip(fr["text"])  # 悬停看完整文本
                self.tbl_frames.setItem(i, 1, it_txt)
        finally:
            self._suppress_frame_tbl = False
        # 行高按换行后的内容自适应
        self.tbl_frames.resizeRowsToContents()

    def _select_frame_row(self, idx: int):
        """画布点框 → 列表同步选中该行"""
        if not (0 <= idx < self.tbl_frames.rowCount()):
            return
        self._suppress_frame_tbl = True
        try:
            self.tbl_frames.selectRow(idx)
            self.tbl_frames.scrollToItem(self.tbl_frames.item(idx, 0))
        finally:
            self._suppress_frame_tbl = False

    def _load_frame_text(self, idx: int):
        """把框文本载入识别结果区 (不朗读)"""
        frames = self.canvas._auto_frames
        if not (0 <= idx < len(frames)):
            return
        text = frames[idx]["text"]
        self._suppress_edit = True
        try:
            self.txt_result.setPlainText(text)
        finally:
            self._suppress_edit = False
        self._cur_text = text
        self.btn_read.setEnabled(bool(text.strip()))

    def _on_frame_tbl_click(self, row: int, col: int):
        """列表单击 → 画布定位高亮 + 载入文本 (不朗读)"""
        if self._suppress_frame_tbl:
            return
        self.canvas.set_active_frame(row)
        self._load_frame_text(row)
        self._status(f"已选中第 {row + 1} 框 (双击朗读)")

    def _on_frame_tbl_double(self, row: int, col: int):
        """列表双击 → 定位 + 朗读 (点中文本单元格时仅进入编辑, 不朗读)"""
        if self._suppress_frame_tbl:
            return
        if col == 1 and self.tbl_frames.item(row, 1) is not None:
            return  # 双击文本列 → 编辑文本, 不朗读
        self.canvas.set_active_frame(row)
        self._load_frame_text(row)
        self._status(f"点读 第 {row + 1} 框, 正在朗读…")
        QTimer.singleShot(30, self._on_read)

    def _on_frame_tbl_changed(self, item):
        """列表文本被编辑 → 回写到画布框数据 (点读/连读按新文本)"""
        if self._suppress_frame_tbl or item is None:
            return
        row, col = item.row(), item.column()
        frames = self.canvas._auto_frames
        if col == 1 and 0 <= row < len(frames):
            frames[row]["text"] = item.text().strip()
            # 若该框文本正显示在识别结果区, 同步刷新
            if self.canvas._active_frame == row:
                self._suppress_edit = True
                try:
                    self.txt_result.setPlainText(frames[row]["text"])
                finally:
                    self._suppress_edit = False
                self._cur_text = frames[row]["text"]
                self.btn_read.setEnabled(bool(frames[row]["text"].strip()))
            self._save_current_file_history()  # 改字立即持久化

    def _selected_frame_rows(self) -> list[int]:
        """取点读框列表当前选中的行号 (升序去重)"""
        return sorted({i.row() for i in self.tbl_frames.selectedIndexes()})

    def _delete_selected_frames(self):
        """删除选中的点读框 (支持 Ctrl/Shift 多选)"""
        rows = self._selected_frame_rows()
        if not rows:
            self._status("请先在点读框列表中选择要删除的框。")
            return
        frames = self.canvas._auto_frames
        for r in sorted(rows, reverse=True):
            if 0 <= r < len(frames):
                frames.pop(r)
        self.canvas._active_frame = -1
        self.canvas._frame_drag = None
        self.canvas.update()
        self._on_frames_changed()
        self._status(f"已删除 {len(rows)} 个点读框, 剩余 {len(frames)} 个")

    def _merge_selected_frames(self):
        """合并选中的 2 个及以上点读框: 取并集矩形, 文本按阅读顺序拼接 (金手指 Ctrl+Q 风格)"""
        rows = self._selected_frame_rows()
        frames = self.canvas._auto_frames
        if len(rows) < 2:
            self._status("合并需要先按住 Ctrl 选择 2 个及以上的点读框。")
            return
        # 并集矩形
        x0 = min(frames[r]["rect"].left() for r in rows)
        y0 = min(frames[r]["rect"].top() for r in rows)
        x1 = max(frames[r]["rect"].right() for r in rows)
        y1 = max(frames[r]["rect"].bottom() for r in rows)
        # 文本按行号顺序 (阅读顺序) 智能拼接
        text = ""
        for r in rows:
            text = _join_text_pair(text, frames[r]["text"])
        base = rows[0]
        full = QRect(0, 0, self.canvas._orig.width(), self.canvas._orig.height()) \
            if self.canvas._orig else None
        merged = QRect(x0, y0, x1 - x0 + 1, y1 - y0 + 1)
        if full is not None:
            merged = merged.intersected(full)
        frames[base]["rect"] = merged
        frames[base]["text"] = text
        for r in sorted(rows[1:], reverse=True):
            frames.pop(r)
        self.canvas._active_frame = base
        self.canvas._frame_drag = None
        self.canvas.update()
        self._on_frames_changed()
        self._select_frame_row(base)
        self._status(
            f"已合并 {len(rows)} 个框为第 {base + 1} 框, "
            f"剩余 {len(frames)} 个 (双击可朗读验证)")

    def _split_selected_frame(self):
        """拆分框 (金手指 Ctrl+E): 高>80 竖切, 否则横切, 留 10px 缝, 文本按中点就近分隔符分半"""
        rows = self._selected_frame_rows()
        if len(rows) != 1:
            if not rows and 0 <= self.canvas._active_frame < len(self.canvas._auto_frames):
                rows = [self.canvas._active_frame]
            else:
                self._status("拆分需要先选中 1 个点读框 (列表单击或画布点框)。")
                return
        idx = rows[0]
        frames = self.canvas._auto_frames
        rect = QRect(frames[idx]["rect"])
        text = frames[idx]["text"]
        if not self.canvas._orig:
            return
        full = QRect(0, 0, self.canvas._orig.width(), self.canvas._orig.height())
        if rect.height() > 80:   # 竖切 (上下两段)
            mid = rect.top() + rect.height() // 2
            r1 = QRect(rect.left(), rect.top(), rect.width(), mid - rect.top() - 5)
            r2 = QRect(rect.left(), mid + 5, rect.width(), rect.bottom() - mid - 5)
        else:                     # 横切 (左右两段)
            mid = rect.left() + rect.width() // 2
            r1 = QRect(rect.left(), rect.top(), mid - rect.left() - 5, rect.height())
            r2 = QRect(mid + 5, rect.top(), rect.right() - mid - 5, rect.height())
        r1 = r1.intersected(full)
        r2 = r2.intersected(full)
        if r1.width() < 10 or r1.height() < 8 or r2.width() < 10 or r2.height() < 8:
            self._status("框太小, 无法拆分。")
            return
        t1, t2 = _split_text_half(text)
        frames[idx]["rect"] = r1
        frames[idx]["text"] = t1
        frames.insert(idx + 1, {"rect": r2, "text": t2})
        self.canvas._active_frame = idx
        self.canvas.update()
        self._on_frames_changed()
        self._select_frame_row(idx)
        self._status(f"已拆分为第 {idx + 1}/{idx + 2} 框 (可用「📥 填充空框」补文字或双击改字)")

    def _insert_frame(self, where: str):
        """在选中框上/下方插入同宽新框 (文本为空, 可用填充空框补文字) — 金手指插框风格"""
        rows = self._selected_frame_rows()
        if not rows and 0 <= self.canvas._active_frame < len(self.canvas._auto_frames):
            rows = [self.canvas._active_frame]
        if len(rows) != 1 or not self.canvas._orig:
            self._status("请先选中 1 个点读框 (列表单击或画布点框)。")
            return
        idx = rows[0]
        frames = self.canvas._auto_frames
        ref = frames[idx]["rect"]
        h = max(30, min(ref.height(), 100))
        full_h = self.canvas._orig.height()
        if where == "above":
            top = max(0, ref.top() - h - 10)
            ins = idx
        else:
            top = min(full_h - h, ref.bottom() + 10)
            top = max(0, top)
            ins = idx + 1
        nr = QRect(ref.left(), top, ref.width(), h).intersected(
            QRect(0, 0, self.canvas._orig.width(), full_h))
        frames.insert(ins, {"rect": nr, "text": ""})
        self.canvas._active_frame = ins
        self.canvas.update()
        self._on_frames_changed()
        self._select_frame_row(ins)
        self._status(f"已在第 {idx + 1} 框{'上方' if where == 'above' else '下方'}插入新框 "
                     f"(第 {ins + 1} 框, 文本为空, 可拖拽调整或「📥 填充空框」)")

    def _fill_empty_frames(self):
        """填充空框 (金手指 Ctrl+F): 整页识别一次, 文字按中心点归属分配给空文本框"""
        frames = self.canvas._auto_frames
        empty = sum(1 for fr in frames if not fr["text"].strip())
        if empty == 0:
            self._status("没有空文本的点读框, 无需填充。")
            return
        if not self.canvas._orig:
            return
        if self._fill_worker and self._fill_worker.isRunning():
            return
        self._status(f"正在整页识别, 为 {empty} 个空框填充文字…")
        arr = qimage_to_bgr(self.canvas._orig.toImage())
        self._fill_worker = _PageOcrWorker(arr)
        self._fill_worker.finished.connect(self._on_fill_done)
        self._fill_worker.error.connect(self._status)
        self._fill_worker.start()

    def _on_fill_done(self, toks: list):
        frames = self.canvas._auto_frames
        filled = 0
        for fr in frames:
            if fr["text"].strip():
                continue
            r = fr["rect"]
            # 中心点落在框内的词 → 归属该框 (嵌套时取面积最小的框, 此处按帧序处理即可)
            inside = [(t, tx) for t, tx in toks if r.contains(t.center())]
            if not inside:
                continue
            text = ""
            for t, tx in inside:
                text = _join_text_pair(text, tx)
            fr["text"] = text
            filled += 1
        self._rebuild_frame_table()
        af = self.canvas._active_frame
        if 0 <= af < len(frames) and frames[af]["text"].strip():
            self._load_frame_text(af)
        if filled:
            self._status(f"已为 {filled} 个空框填充文字 (双击可修改)")
        else:
            self._status("未能为空框识别到文字 (框内可能没有内容, 可拖拽调整框位置)")

    def _on_read_page(self):
        """整页连读: 按框顺序逐框朗读; 识别结果只显示当前行, 画布/列表同步高亮"""
        frames = self.canvas._auto_frames
        if not frames:
            self._status("请先框选生成点读框, 再整页连读。")
            return
        order = [i for i, fr in enumerate(frames) if fr["text"].strip()]
        if not order:
            self._status("点读框内没有可朗读的文字。")
            return
        self._seq_frames = list(order)
        self._seq_total = len(order)
        self._seq_mode = True
        self._status(f"整页连读: 共 {len(order)} 个框, 按顺序朗读 (点「■ 停止」可随时中断)")
        self._seq_read_next()

    def _seq_read_next(self):
        """整页连读: 朗读队列中的下一框 (上一行播完由 EndOfMedia 驱动)"""
        if not self._seq_mode:
            return
        frames = self.canvas._auto_frames
        while self._seq_frames:
            idx = self._seq_frames.pop(0)
            if 0 <= idx < len(frames) and frames[idx]["text"].strip():
                self.canvas.set_active_frame(idx)   # 画布高亮当前框
                self._select_frame_row(idx)         # 列表高亮并滚动到当前行
                self._load_frame_text(idx)          # 识别结果只显示当前行
                done = self._seq_total - len(self._seq_frames)
                self._status(f"整页连读 {done} / {self._seq_total}…")
                QTimer.singleShot(30, self._on_read)
                return
        self._seq_finish()

    def _seq_finish(self):
        """整页连读: 全部读完收尾"""
        if not self._seq_mode:
            return
        self._seq_cancel()
        self.canvas.set_active_frame(-1)
        self._status("整页连读完成")

    def _seq_cancel(self):
        """中断整页连读队列 (停止/点其他框/切页/关窗时)"""
        self._seq_mode = False
        self._seq_frames = []

    def _on_frame_click(self, idx: int):
        """点读: 点击自动框 → 立即显示该框文本并朗读 (无需重新OCR)"""
        self._seq_cancel()   # 手动点框接管, 中断整页连读队列
        frames = self.canvas._auto_frames
        if not (0 <= idx < len(frames)):
            return
        fr = frames[idx]
        text = fr["text"]
        self._last_sel_rect = QRect(fr["rect"])
        self._suppress_edit = True
        try:
            self.txt_result.setPlainText(text)
        finally:
            self._suppress_edit = False
        self._cur_text = text
        self.btn_read.setEnabled(bool(text.strip()))
        self._select_frame_row(idx)   # 列表同步选中该框
        self.canvas.set_active_frame(idx)   # 全屏点读: 点中句子显示高亮框
        self._status(f"点读 第 {idx + 1} 框, 正在朗读…")
        # 点读语义: 点击即读 (不受"自动朗读"开关影响)
        QTimer.singleShot(30, self._on_read)

    def _on_region_selected(self, qimg: QImage, orig_rect: QRect):
        self._last_sel_rect = orig_rect  # 保存到 MainWindow, 供历史记录写入
        # 注意: 不清 canvas 的蓝色实线选区 — 框要一直保留, paintEvent 会自动用
        # 当前 _zoom 重绘, 用户缩放时框也会跟着缩放, 作为"当前识别区域"的视觉标志
        if qimg.isNull():
            return
        self._status("正在识别文字…")
        self.btn_read.setEnabled(False)
        self.canvas.setLoading(True)  # 显示识别等待遮罩
        # 在主线程完成 QImage->BGR numpy 转换(内存自持, 线程安全)
        try:
            arr = qimage_to_bgr(qimg)
        except Exception as e:
            self.canvas.setLoading(False)
            self._status(f"图像转换失败: {e}")
            return
        # 清理旧 worker
        if self._ocr_worker and self._ocr_worker.isRunning():
            self._ocr_worker.quit()
            self._ocr_worker.wait()
        self._ocr_worker = OCRWorker(arr)
        self._ocr_worker.finished.connect(self._on_ocr_done)
        self._ocr_worker.error.connect(self._on_ocr_error)
        self._ocr_worker.start()

    def _on_ocr_done(self, text: str):
        self.canvas.setLoading(False)
        text = self._clean_ocr_text(text)
        self._cur_text = text
        self._suppress_edit = True
        try:
            if text:
                self.txt_result.setPlainText(text)
                # 手动拖拽的选区直接作为一个点读框加入列表 (可点读/编辑/合并/连读)
                if self._last_sel_rect is not None and self.canvas._orig:
                    frames = self.canvas._auto_frames
                    frames.append({"rect": QRect(self._last_sel_rect), "text": text})
                    new_idx = len(frames) - 1
                    self.canvas._active_frame = new_idx
                    # 选区已变成点读框 (蓝色高亮+手柄), 清掉重复的选区线
                    self.canvas._sel_orig_p1 = self.canvas._sel_orig_p2 = None
                    self.canvas.update()
                    self._on_frames_changed()  # 同步列表并持久化
                    self._select_frame_row(new_idx)
                    self._status("识别完成, 已加入点读框列表。" +
                                 ("即将朗读…" if self._auto_read else "可点击「朗读」"))
                else:
                    self._status("识别完成。" + ("即将朗读…" if self._auto_read else "可点击「朗读」"))
                self.btn_read.setEnabled(True)

                if self._auto_read:
                    QTimer.singleShot(50, self._on_read)
            else:
                self.txt_result.setPlainText("")

                self._status("该区域未识别到文字, 请重新选择。")
        finally:
            self._suppress_edit = False

    def _build_dict_html(self, text: str, is_selection: bool = False) -> str | None:
        """构建词典查询的 HTML (不显示, 只返回内容); 未找到返回 None"""
        d = get_dict()
        if not d.available:
            return None
        result = d.lookup_text(text)
        items_en = result["en"]
        items_zh = result["zh"]
        if not items_en and not items_zh:
            return None
        html_parts = []
        # 词典标题
        if is_selection:
            html_parts.append(
                f'<div style="font-size:13px; color:#1677ff; margin-bottom:6px; padding:4px 8px;'
                f' background:#e6f4ff; border-radius:3px;">'
                f'📖 释义: <b>{text}</b></div>'
            )

        # --- 英文 ---
        if items_en:
            html_parts.append('<div style="font-size:13px; color:#888; margin-bottom:4px;">📖 英汉 (ECDICT)</div>')
            for it in items_en:
                word = it["word"]
                phonetic = it["phonetic"]
                trans = it["translation"].replace("\n", "<br>")
                phon_html = f' <span style="color:#888;">/{phonetic}/</span>' if phonetic else ""
                html_parts.append(
                    f'<div style="margin-bottom:8px;">'
                    f'<b style="color:#0a6;">{word}</b>{phon_html}'
                    f'<div style="color:#333; font-size:14px;">{trans}</div>'
                    f'</div>'
                )

        # --- 中文 (cedict + idiom) ---
        if items_zh:
            html_parts.append('<div style="font-size:13px; color:#888; margin:8px 0 4px;">📖 汉语 (cedict + 成语)</div>')
            for it in items_zh:
                word = it["word"]
                pinyin = it.get("pinyin", "")
                pinyin_html = f' <span style="color:#888;">{pinyin}</span>' if pinyin else ""
                wtype = it.get("type", "")
                if wtype == "idiom":
                    exp = it.get("explanation", "").replace("\n", "<br>")
                    example = it.get("example", "").replace("\n", "<br>")
                    ex_html = f'<div style="color:#888; font-size:13px; margin-top:2px;">例: {example}</div>' if example else ""
                    html_parts.append(
                        f'<div style="margin-bottom:10px; padding:6px; background:#fffbe6; border-left:3px solid #faad14;">'
                        f'<b style="color:#d48806;">{word}</b>{pinyin_html}'
                        f'<span style="color:#fa8c16; font-size:12px; margin-left:6px;">[成语]</span>'
                        f'<div style="color:#333; font-size:14px; margin-top:4px;">{exp}</div>'
                        f'{ex_html}'
                        f'</div>'
                    )
                else:
                    defn = it.get("defn", "").replace(";", ";<br>")
                    trad = it.get("trad", "")
                    trad_html = f' <span style="color:#aaa; font-size:12px;">繁: {trad}</span>' if trad and trad != word else ""
                    html_parts.append(
                        f'<div style="margin-bottom:8px;">'
                        f'<b style="color:#0a6;">{word}</b>{pinyin_html}{trad_html}'
                        f'<div style="color:#333; font-size:14px;">{defn}</div>'
                        f'</div>'
                    )

        return "\n".join(html_parts)

    def _on_deep_learn(self):
        """深度学习按钮: 弹出左原文 + 右词典释义的对话框"""
        text = self.txt_result.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "深度学习", "请先选择区域识别文字, 或在识别结果里输入内容。")
            return
        html = self._build_dict_html(text) or ""
        # 加上标题
        if html:
            html = (
                f'<div style="font-size:13px; color:#1677ff; margin-bottom:10px; padding:6px 10px;'
                f' background:#e6f4ff; border-radius:4px; font-weight:bold;">'
                f'📚 词典释义 (全文自动提取)</div>'
                + html
            )
        voice = self.cmb_voice.currentData()
        rate = f"{self.sld_rate.value():+d}%"
        self._deep_dialog = DeepLearnDialog(self, text, html, voice, rate)
        self._deep_dialog.show()

    def _on_ocr_error(self, msg: str):
        self.canvas.setLoading(False)
        self.btn_read.setEnabled(True)
        self._status(msg)

    def _on_about(self):
        """关于对话框（标准组件，版本读 version.py）"""
        icon_path = os.path.join(app_dir(), "图标.png")
        show_about(APP_TITLE, self,
                   icon_path=icon_path if os.path.exists(icon_path) else None)

    # ----------------------------------------------------------
    # 朗读
    # ----------------------------------------------------------
    def _on_auto_toggled(self, on: bool):
        self._auto_read = on

    def _on_voices_loaded(self, voices: list):
        """Azure 实时 voice 列表到达, 刷新下拉框并保持当前选择 (voice 已下架则回退到第一个)"""
        if not voices:
            return
        cur = self.cmb_voice.currentData()
        self.cmb_voice.blockSignals(True)
        self.cmb_voice.clear()
        for name, vid in voices:
            self.cmb_voice.addItem(name, vid)
        idx = self.cmb_voice.findData(cur) if cur else -1
        if idx < 0:
            # 当前选择已下架 → 回退缺省美式英语, 再不行取列表第一个
            idx = self.cmb_voice.findData(DEFAULT_VOICE_ID)
        if idx < 0:
            for i in range(self.cmb_voice.count()):
                if str(self.cmb_voice.itemData(i) or "").startswith("en-US"):
                    idx = i
                    break
        self.cmb_voice.setCurrentIndex(max(0, idx))
        self.cmb_voice.blockSignals(False)

    def _on_read(self):
        text = self.txt_result.toPlainText().strip()
        if not text:
            self._status("没有可朗读的文字")
            return
        self._cur_text = text
        # 停止当前播放/合成
        self._stop_playback()
        voice = self.cmb_voice.currentData()
        rate = f"{self.sld_rate.value():+d}%"
        self._cur_voice, self._cur_rate = voice, rate
        # 不使用缓存: 每次都重新流式合成 (边合成边播放, AI对话式秒出声)
        self._audio_bytes = bytearray()
        self._player_started = False
        self._playing_stream = True
        self.btn_read.setEnabled(False)
        self._status("正在连接 Azure 合成语音…")
        worker = TTSWorker(text, voice, rate)
        self._tts_worker = worker
        worker.piece_ready.connect(self._on_piece)
        worker.all_done.connect(self._on_stream_done)
        worker.error.connect(self._on_tts_error)
        worker.progress.connect(self._status)
        worker.start()

    # ----------------------------------------------------------
    # 流式朗读: 音频数据边到边写边播 (单一播放器, 全程零切换零重建)
    # ----------------------------------------------------------
    def _on_piece(self, data: bytes):
        if not self._playing_stream:
            return
        self._audio_bytes += data
        if not self._player_started:
            # 预缓冲约12KB(24kbps下约4秒)再开播; 短文本等合成完一起播
            worker_done = (self._tts_worker is None or not self._tts_worker.isRunning())
            if len(self._audio_bytes) >= 12288 or worker_done:
                self._start_stream_play()
        else:
            self.tts_device.write_data(data)

    def _start_stream_play(self):
        if self._player_started or not self._playing_stream:
            return
        self._player_started = True
        self.tts_device = _TtsStreamDevice()
        self.tts_device.write_data(bytes(self._audio_bytes))
        self._close_player()
        self.player = QMediaPlayer()
        self.audio_out = QAudioOutput()
        self.player.setAudioOutput(self.audio_out)
        self.audio_out.setVolume(0.9)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.errorOccurred.connect(self._on_playback_error)
        self.player.setSourceDevice(self.tts_device)
        self.player.play()
        self._status("边合成边播放中…")

    def _close_player(self):
        # 关键: 旧 QAudioOutput 不能被 Python 引用计数立即销毁 (旧播放器还在异步收尾,
        # 会形成悬空指针导致原生崩溃), 必须和播放器一起走 deleteLater 安全销毁
        p = getattr(self, "player", None)
        ao = getattr(self, "audio_out", None)
        if ao is not None:
            try:
                ao.deleteLater()
            except RuntimeError:
                pass
        if p is not None:
            try:
                p.stop()
                p.deleteLater()
            except RuntimeError:
                pass
        self.player = None
        self.audio_out = None

    def _on_stream_done(self, ok: bool):
        if not self._playing_stream:
            return
        if ok:
            if getattr(self, "tts_device", None) is not None:
                self.tts_device.finish()
            if not self._player_started and self._audio_bytes:
                self._start_stream_play()
            elif not self._player_started:
                # 没有任何音频产出 (罕见): 播放不会触发 EndOfMedia, 直接待下一行
                self._status("合成完成")
                if self._seq_mode:
                    QTimer.singleShot(100, self._seq_read_next)
            else:
                self._status("合成完成")
        # ok=False 是用户取消, 设备已在 _stop_playback 里 abort, 无需处理

    def _on_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia and self._playing_stream:
            self._playing_stream = False
            self.btn_read.setEnabled(bool(self.txt_result.toPlainText().strip()))
            if self._seq_mode:
                # 整页连读: 本行播完, 接着读下一行
                self._seq_read_next()
            else:
                self._status("朗读结束")

    def _on_playback_error(self, err, msg):
        self._status(f"播放错误: {msg}")
        self._playing_stream = False
        self.btn_read.setEnabled(bool(self.txt_result.toPlainText().strip()))

    def _on_tts_error(self, msg: str):
        self._status(msg)
        # 已开播的内容继续播完缓冲; 未开播则直接收尾
        if getattr(self, "tts_device", None) is not None:
            self.tts_device.finish()
        if not self._player_started:
            self._playing_stream = False
        self.btn_read.setEnabled(bool(self.txt_result.toPlainText().strip()))
        if self._seq_mode:
            # 整页连读: 本行失败, 稍后跳到下一行 (不中断整页)
            self._status(f"{msg} → 跳过本行继续连读")
            QTimer.singleShot(600, self._seq_read_next)

    def _on_stop(self):
        self._seq_cancel()   # 中断整页连读队列
        self._stop_playback()
        self._status("已停止朗读")

    def _stop_playback(self):
        self._playing_stream = False
        # 唤醒并结束流式设备 (媒体解码线程可能正阻塞等数据)
        dev = getattr(self, "tts_device", None)
        if dev is not None:
            dev.abort()
            self.tts_device = None
        self._close_player()
        if self._tts_worker is not None:
            if self._tts_worker.isRunning():
                self._tts_worker.cancel()
                self._detach_worker(self._tts_worker)
            self._tts_worker = None
        self.btn_read.setEnabled(bool(self.txt_result.toPlainText().strip()))

    def _detach_worker(self, w):
        """断开旧 worker 的信号并放入回收列表, 防止线程未结束时被垃圾回收"""
        for sig in (w.piece_ready, w.all_done, w.error, w.progress):
            try:
                sig.disconnect()
            except TypeError:
                pass
        self._dead_workers.append(w)
        w.finished.connect(
            lambda: self._dead_workers.remove(w) if w in self._dead_workers else None
        )

    # ----------------------------------------------------------
    # 结果操作
    # ----------------------------------------------------------
    def _on_edit(self):
        """弹出编辑框, 修改识别结果后保存"""
        cur_text = self.txt_result.toPlainText()
        if not cur_text.strip():
            self._status("没有可编辑的识别结果")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("编辑识别结果")
        dlg.setMinimumSize(640, 420)
        dlg.resize(720, 500)
        layout = QVBoxLayout(dlg)
        tip = QLabel("修正 OCR 识别错误后点击「保存」")
        tip.setStyleSheet("color:#666; font-size:12px;")
        layout.addWidget(tip)
        editor = QTextEdit()
        editor.setPlainText(cur_text)
        editor.selectAll()
        editor.setFont(QFont("Microsoft YaHei", 13))
        layout.addWidget(editor, 1)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(dlg.reject)
        btn_save = QPushButton("💾 保存")
        btn_save.setDefault(True)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_save)
        layout.addLayout(btn_row)

        def _do_save():
            new_text = editor.toPlainText().strip()
            if not new_text:
                QMessageBox.warning(dlg, "提示", "识别结果不能为空")
                return
            dlg.accept()
            # 更新识别结果框
            self._suppress_edit = True
            try:
                self.txt_result.setPlainText(new_text)
            finally:
                self._suppress_edit = False
            # 同步回当前点读框 (编辑结果即该框文本时)
            frames = self.canvas._auto_frames
            idx = self.canvas._active_frame
            if 0 <= idx < len(frames) and frames[idx]["text"] == cur_text.strip():
                frames[idx]["text"] = new_text
                self._rebuild_frame_table()
                self._save_current_file_history()
            self._cur_text = new_text
            self.btn_read.setEnabled(True)
            self._status("已保存修改")

        btn_save.clicked.connect(_do_save)
        dlg.exec()

    def _on_copy(self):
        QApplication.clipboard().setText(self.txt_result.toPlainText())
        self._status("已复制到剪贴板")

    def _on_clear(self):
        self._suppress_edit = True
        try:
            self.txt_result.clear()
        finally:
            self._suppress_edit = False
        self._cur_text = ""
        
        
        self.btn_read.setEnabled(False)

    # ----------------------------------------------------------
    # OCR 后处理: 清理拼音/注音/页眉页脚噪声
    # ----------------------------------------------------------
    def _clean_ocr_text(self, text: str) -> str:
        """OCR 后处理: 目前直接返回原文本, 不做任何清理

        之前的拼音清理规则会误删整行英文 (如 "I joined an online book fair..."),
        故暂时全部移除。拼音问题请用深度学习/词典功能辅助理解。
        """
        return (text or "").strip()

    # ----------------------------------------------------------
    # 历史记录持久化 (history.json)
    # ----------------------------------------------------------
    def _history_json_path(self) -> str:
        """history.json 路径: 用户可写目录 (%LOCALAPPDATA%\\书页朗读助手)"""
        return os.path.join(app_user_dir(), "history.json")

    def _read_history_json(self) -> dict:
        """读取 history.json, 文件不存在或损坏返回空 dict"""
        import json
        try:
            p = self._history_json_path()
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {"last_file": "", "last_page": 0, "per_file": {}}

    def _write_history_json(self, data: dict):
        """写入 history.json"""
        import json
        try:
            p = self._history_json_path()
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            _crash_log.write(f"\n[history] write fail: {e}\n")
            _crash_log.flush()

    def _history_page_key(self) -> str:
        """当前历史记录归属的页 key: PDF 用页码, 图片用 'all'"""
        if self._pdf_doc:
            return str(self._cur_page)
        return "all"

    def _save_current_file_history(self):
        """把当前页的全部点读框写到 JSON (per_file[path][page_key])

        存储格式与旧版历史记录兼容: [{"text": str, "rect": [x,y,w,h]}, ...]
        旧版本保存的历史选区加载后会自动变回点读框。
        """
        if not self._cur_file_path:
            return
        data = self._read_history_json()
        entries = []
        for fr in self.canvas._auto_frames:
            r = fr["rect"]
            entries.append({"text": fr["text"],
                            "rect": [r.x(), r.y(), r.width(), r.height()]})
        data.setdefault("per_file", {})
        bucket = data["per_file"].setdefault(self._cur_file_path, {})
        # 兼容老格式: 旧版 per_file[path] 直接是 list, 迁移到 {"all": list}
        if isinstance(bucket, list):
            bucket = {"all": bucket}
            data["per_file"][self._cur_file_path] = bucket
        bucket[self._history_page_key()] = entries
        self._write_history_json(data)

    def _load_file_history(self, file_path: str, page_key: str | None = None):
        """从 JSON 读取指定文件 + 页的点读框, 直接恢复到画布"""
        # 静默清空当前框 (不走 clear_auto_frames, 避免触发 framesChanged 误保存)
        self.canvas._auto_frames = []
        self.canvas._active_frame = -1
        self.canvas._frame_drag = None
        self.canvas.update()
        self._sync_read_page_btn()
        self._rebuild_frame_table()
        if not file_path:
            return
        if page_key is None:
            page_key = self._history_page_key()
        data = self._read_history_json()
        file_bucket = data.get("per_file", {}).get(file_path, {})
        # 兼容老格式: list 迁移到 dict, 全部塞到 "all"
        if isinstance(file_bucket, list):
            entries = file_bucket if page_key == "all" else []
        else:
            entries = file_bucket.get(page_key, [])
        frames = []
        for entry in entries:
            rect_list = entry.get("rect")
            text = entry.get("text", "")
            if not rect_list or len(rect_list) != 4:
                continue  # 没有位置的旧记录无法成框, 跳过
            frames.append({"rect": QRect(*rect_list), "text": text})
        if frames:
            self.canvas._auto_frames = frames
            self.canvas._active_frame = -1
            self.canvas.update()
            self._sync_read_page_btn()
            self._rebuild_frame_table()

    def _switch_file(self, new_path: str):
        """切换文件: 先保存旧文件当前页的点读框, 再加载新文件的点读框"""
        self._seq_cancel()   # 换文件中断整页连读
        if self._cur_file_path and self._cur_file_path != new_path:
            self._save_current_file_history()
        self._cur_file_path = new_path
        self._load_file_history(new_path)

    def _switch_page_history(self, new_page: int):
        """翻 PDF 页: 先保存旧页点读框, 再加载新页点读框"""
        self._seq_cancel()   # 翻页中断整页连读
        if not self._cur_file_path or not self._pdf_doc:
            return
        self._save_current_file_history()      # 存旧页
        self._cur_page = new_page
        self._load_file_history(self._cur_file_path, str(new_page))  # 加新页

    def _auto_reopen_last(self):
        """启动时自动打开上次关闭的文件 + 页码 + 历史记录"""
        data = self._read_history_json()
        path = data.get("last_file", "")
        if not path or not os.path.exists(path):
            return
        last_page = data.get("last_page", 0)
        try:
            suffix = os.path.splitext(path)[1].lower()
            if suffix == ".pdf":
                doc = fitz.open(path)
                self._pdf_doc = doc
                self._pdf_pages = len(doc)
                self._cur_page = max(0, min(last_page - 1, self._pdf_pages - 1))
                self._render_pdf_page()
                self._update_page_label()
                self._build_pdf_thumbs(path)
            else:
                pix = QPixmap(path)
                if pix.isNull():
                    return
                self._pdf_doc = None
                self._pdf_pages = 0
                self._cur_page = 0
                self.canvas.setImage(pix)
                self._update_page_label()
                self._build_folder_thumbs(path)
            # 设置路径 + 加载对应历史记录
            self._cur_file_path = path
            self._load_file_history(path)
            self._status(f"已恢复上次文件: {Path(path).name}")
        except Exception as e:
            _crash_log.write(f"\n[auto_reopen] fail: {e}\n")
            _crash_log.flush()

    # ----------------------------------------------------------
    # 设置持久化: 发音人 / 语速 / 自动朗读
    # ----------------------------------------------------------
    def _load_settings(self):
        """启动时加载上次保存的语音设置"""
        voice = self._settings.value("voice", "")
        idx = self.cmb_voice.findData(voice)
        if idx >= 0:
            self.cmb_voice.setCurrentIndex(idx)
        try:
            rate = int(self._settings.value("rate", 0))
        except (TypeError, ValueError):
            rate = 0
        self.sld_rate.setValue(max(-50, min(50, rate)))
        self.lbl_rate_val.setText(f"{rate:+d}%")
        auto = self._settings.value("auto_read", True)
        auto = auto if isinstance(auto, bool) else str(auto).lower() == "true"
        self.chk_auto.setChecked(auto)  # 触发 _on_auto_toggled 同步 self._auto_read

    def _save_settings(self, *args):
        """设置变化时立即保存, 下次启动自动恢复"""
        self._settings.setValue("voice", self.cmb_voice.currentData())
        self._settings.setValue("rate", self.sld_rate.value())
        self._settings.setValue("auto_read", self.chk_auto.isChecked())

    # ----------------------------------------------------------
    # 工具方法
    # ----------------------------------------------------------
    def _status(self, msg: str):
        self._lbl_status.setText(msg)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        # 窗口尺寸变化 → 点读框列宽变化 → 行高按换行文本自适应
        tbl = getattr(self, "tbl_frames", None)
        if tbl is not None:
            tbl.resizeRowsToContents()

    def closeEvent(self, ev):
        self._seq_cancel()
        self._stop_playback()
        # 保存 history.json: last_file + last_page + 当前文件历史
        if self._cur_file_path:
            data = self._read_history_json()
            data["last_file"] = self._cur_file_path
            data["last_page"] = self._cur_page + 1  # 页码对用户从 1 开始
            self._save_current_file_history()  # 先写 per_file
            self._write_history_json(data)      # 再写 last_file/last_page
        super().closeEvent(ev)


# ============================================================
# 入口
# ============================================================
def ensure_online_tts_consent() -> bool:
    """首次运行告知：在线朗读会把框选文字发送到微软语音服务。"""
    settings = QSettings("Chuangxuan", "PageReadAloud")
    if settings.value("tts_online_consent", "", type=str) == "yes":
        return True
    box = QMessageBox()
    box.setWindowTitle("关于在线朗读")
    box.setIcon(QMessageBox.Icon.Information)
    box.setText(
        "朗读使用在线神经语音服务。\n\n"
        "框选出的文字会发送到微软语音服务合成音频，图片和 PDF 文件不会上传，"
        "也不会保存到任何服务器。\n"
        "如果不希望文字离开本机，请退出本程序。\n\n"
        "继续使用即表示同意。")
    box.setStandardButtons(QMessageBox.StandardButton.Ok |
                           QMessageBox.StandardButton.Cancel)
    box.button(QMessageBox.StandardButton.Ok).setText("同意并继续")
    box.button(QMessageBox.StandardButton.Cancel).setText("退出")
    if box.exec() != QMessageBox.StandardButton.Ok:
        return False
    settings.setValue("tts_online_consent", "yes")
    return True


def main():
    # 全局异常捕获: 记录未处理的Python异常
    def _excepthook(exc_type, exc_value, tb):
        msg = "".join(traceback.format_exception(exc_type, exc_value, tb))
        _crash_log.write(f"[未捕获异常]\n{msg}\n")
        _crash_log.flush()
        sys.__excepthook__(exc_type, exc_value, tb)
    sys.excepthook = _excepthook

    # Qt 消息处理: 捕获Qt内部警告/严重错误
    from PyQt6.QtCore import qInstallMessageHandler, QtMsgType
    def _msghandler(msg_type, ctx, msg):
        if msg_type != QtMsgType.QtDebugMsg:
            _crash_log.write(f"[Qt消息 {msg_type}] {msg}\n  context: {ctx.file}:{ctx.line}\n")
            _crash_log.flush()
    qInstallMessageHandler(_msghandler)

    app = QApplication(sys.argv)
    # 原生崩溃时把各线程堆栈写入 crash.log, 便于定位 Qt 底层崩溃点
    import faulthandler
    try:
        faulthandler.enable(_crash_log)
    except Exception:
        pass
    app.setApplicationName(APP_TITLE)
    # 应用级图标
    _app_icon_path = os.path.join(app_dir(), "图标.png")
    if os.path.exists(_app_icon_path):
        app.setWindowIcon(QIcon(_app_icon_path))
    if not ensure_online_tts_consent():
        return
    w = MainWindow()
    w.show()
    ret = app.exec()
    _crash_log.write(f"[正常退出] code={ret}\n")
    _crash_log.close()
    sys.exit(ret)


if __name__ == "__main__":
    main()
