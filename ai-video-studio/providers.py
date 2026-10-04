"""对外部服务的薄封装：Claude（剧本）+ fal.ai（图像 / 视频）。

设置 STUDIO_MOCK=1 时不调用任何付费 API，而是生成占位图片和视频，
用于测试整条流水线或调整剪辑参数。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def _load_dotenv(path: Path):
    """读取项目根目录的 .env（KEY=VALUE 格式）。已经设置的环境变量优先，不会被覆盖。"""
    if not path.exists():
        return
    for line in path.read_text("utf-8-sig").splitlines():  # -sig：兼容 Windows 记事本写入的 BOM
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


_load_dotenv(Path(__file__).resolve().parent / ".env")

MOCK = os.environ.get("STUDIO_MOCK") == "1"


def require_ffmpeg():
    """确认 ffmpeg / ffprobe 可用，否则按系统给出安装提示后退出。"""
    missing = [b for b in ("ffmpeg", "ffprobe") if not shutil.which(b)]
    if not missing:
        return
    hint = ("winget install --id Gyan.FFmpeg -e   （装完重开终端）" if IS_WIN
            else "brew install ffmpeg" if IS_MAC
            else "sudo apt install ffmpeg")
    sys.exit(f"找不到 {' / '.join(missing)}，请先安装：\n  {hint}")


# --------------------------------------------------------------------------- #
# Claude
# --------------------------------------------------------------------------- #
def claude_json(system: str, user: str, model: str, max_tokens: int = 8000) -> dict:
    """调用 Claude 并把回复解析成 JSON。"""
    if MOCK:
        from mock_data import MOCK_STORYBOARD
        return json.loads(json.dumps(MOCK_STORYBOARD))

    import anthropic

    client = anthropic.Anthropic()  # 读取 ANTHROPIC_API_KEY
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return _extract_json(text)


def _extract_json(text: str) -> dict:
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(raw)


# --------------------------------------------------------------------------- #
# fal.ai
# --------------------------------------------------------------------------- #
_upload_cache: dict[str, str] = {}


def upload(path: Path) -> str:
    """把本地文件上传到 fal CDN，返回 URL（同一文件只传一次）。"""
    key = f"{path.resolve()}:{path.stat().st_mtime_ns}"
    if key in _upload_cache:
        return _upload_cache[key]
    if MOCK:
        url = f"mock://{path.name}"
    else:
        import fal_client
        url = fal_client.upload_file(str(path))
    _upload_cache[key] = url
    return url


def fal_run(endpoint: str, arguments: dict, log_prefix: str = "") -> dict:
    """同步调用 fal 模型（内部走队列），带简单重试。"""
    if MOCK:
        return {"_mock": True, "endpoint": endpoint, "arguments": arguments}

    import fal_client

    def on_update(update):
        if isinstance(update, fal_client.InProgress):
            for log in update.logs or []:
                print(f"    {log_prefix}{log.get('message', '')}")

    last_err = None
    for attempt in range(3):
        try:
            return fal_client.subscribe(
                endpoint, arguments=arguments, with_logs=True, on_queue_update=on_update
            )
        except Exception as e:  # 网络抖动 / 排队超时
            last_err = e
            wait = 10 * (attempt + 1)
            print(f"    ! 调用失败（{e}），{wait}s 后重试…")
            time.sleep(wait)
    raise RuntimeError(f"fal 调用失败：{endpoint}: {last_err}")


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, dest)
    return dest


# --------------------------------------------------------------------------- #
# 生成图像 / 视频（真实或模拟）
# --------------------------------------------------------------------------- #
def generate_images(endpoint: str, args: dict, out_paths: list[Path], label: str) -> list[Path]:
    result = fal_run(endpoint, args, log_prefix=f"[{label}] ")
    if result.get("_mock"):
        for i, p in enumerate(out_paths):
            _mock_image(p, f"{label}\n#{i + 1}", args.get("aspect_ratio", "16:9"))
        return out_paths
    images = result.get("images", [])
    saved = []
    for img, p in zip(images, out_paths):
        saved.append(download(img["url"], p))
    return saved


def generate_video(endpoint: str, args: dict, out_path: Path, label: str, duration: int) -> Path:
    result = fal_run(endpoint, args, log_prefix=f"[{label}] ")
    if result.get("_mock"):
        return _mock_video(out_path, label, duration)
    return download(result["video"]["url"], out_path)


# --------------------------------------------------------------------------- #
# 模拟素材
# --------------------------------------------------------------------------- #
_ASPECT = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (1024, 1024), "21:9": (1680, 720)}

# 能显示中文的系统字体（按顺序尝试）
_CJK_FONTS = {
    "win32": ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simsun.ttc"],
    "darwin": ["/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
               "/System/Library/Fonts/Hiragino Sans GB.ttc"],
}.get(sys.platform, ["/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                     "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"])

# 烧录字幕时 libass 使用的字体名
SUBTITLE_FONT = "Microsoft YaHei" if IS_WIN else "PingFang SC" if IS_MAC else "Noto Sans CJK SC"


def _cjk_font(size: int):
    from PIL import ImageFont

    for f in _CJK_FONTS:
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def _mock_image(path: Path, text: str, aspect: str):
    from PIL import Image, ImageDraw

    w, h = _ASPECT.get(aspect, (1024, 1024))
    path.parent.mkdir(parents=True, exist_ok=True)
    hue = sum(map(ord, text)) % 255
    img = Image.new("RGB", (w, h), (40 + hue // 3, 60, 90 + hue // 2))
    d = ImageDraw.Draw(img)
    d.multiline_text((40, 40), text, fill="white", spacing=8, font=_cjk_font(48))
    img.save(path)


def _mock_video(path: Path, label: str, duration: int) -> Path:
    require_ffmpeg()
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=24:duration={duration}",
         "-f", "lavfi", "-i", f"sine=frequency={300 + len(label) * 20}:duration={duration}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )
    return path
