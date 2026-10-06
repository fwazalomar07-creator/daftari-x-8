"""مكتبة flet وهمية: تسمح بتنفيذ كل دوال بناء الشاشات بدون Flet الحقيقي، لكشف الأخطاء البرمجية
(أسماء خاطئة، متغيرات ناقصة، استدعاءات غير موجودة في منطقنا). لا تتحقق من صحة واجهة Flet نفسها."""
import sys, types, asyncio


class Ctl:
    def __init__(self, *args, **kw):
        self.args, self.kw = args, kw
        for k, v in kw.items():
            setattr(self, k, v)
        if args and "content" not in kw and not isinstance(args[0], (list, tuple, str)):
            self.content = args[0]
        self.controls = kw.get("controls", list(args[0]) if args and isinstance(args[0], (list, tuple)) else [])
        self.value = kw.get("value")
    def update(self): pass
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return None


class Enum:
    def __getattr__(self, n):
        return n


class Page(Ctl):
    def __init__(self):
        super().__init__()
        self.services, self.dialogs, self.added, self.tasks = [], [], [], []
    def update(self): pass
    def add(self, *c): self.added += c
    def show_dialog(self, d): self.dialogs.append(d)
    def pop_dialog(self): self.dialogs and self.dialogs.pop()
    def run_task(self, fn, *a): self.tasks.append((fn, a))


class FilePicker(Ctl):
    """يحاكي نافذة الحفظ على سطح المكتب: تعيد المسار المختار **ولا تكتب الملف** (كسلوك Flet الحقيقي)."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.save_path = None       # None = مسار مؤقت تلقائي · False = المستخدم ألغى النافذة · نص = مسار محدد
        self.save_calls = []
        self.raise_on_save = None   # استثناء يُرفع من save_file (منصة لا تدعمه)

    async def pick_files(self, **kw): return []

    async def save_file(self, **kw):
        self.save_calls.append(kw)
        if self.raise_on_save:
            raise self.raise_on_save
        if self.save_path is False:
            return None
        if self.save_path:
            return self.save_path
        import os, tempfile
        return os.path.join(tempfile.gettempdir(), "daftari-fake-" + str(kw.get("file_name") or "out"))


class _Border(Ctl):
    @staticmethod
    def all(*a, **kw): return Ctl()
    @staticmethod
    def only(*a, **kw): return Ctl()


class AudioRecorder(Ctl):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.started = False
        self.on_stream = kw.get("on_stream")

    async def start_recording(self, *a, **kw):
        self.started = True

    async def stop_recording(self, *a, **kw):
        self.started = False
        if self.on_stream:
            ev = Ctl(chunk=b"\x00\x01" * 32)
            ev.chunk = b"\x00\x01" * 32
            self.on_stream(ev)
        return None


def install():
    m = types.ModuleType("flet")
    def __getattr__(name):
        if name == "Page": return Page
        if name == "FilePicker": return FilePicker
        if name == "AudioRecorder": return AudioRecorder
        if name == "border": return _Border
        if name in ("Colors", "Icons", "FontWeight", "KeyboardType", "ScrollMode", "MainAxisAlignment", "CrossAxisAlignment",
                    "TextAlign", "FilePickerFileType", "BoxFit", "TextOverflow", "MarkdownExtensionSet", "ThemeMode", "AnimatedSwitcherTransition", "AnimationCurve", "ClipBehavior"):
            return Enum()
        return Ctl
    m.__getattr__ = __getattr__
    sys.modules["flet"] = m
    return m
