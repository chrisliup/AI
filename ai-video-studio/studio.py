#!/usr/bin/env python3
"""AI 故事短片工作流（半自动 + 人工把关）

流程：  创意 → 剧本/分镜 → 角色定妆照 → 关键帧 → 图生视频 → 合成成片
每一步都会把结果落在项目目录里，你检查、挑选、修改后再进入下一步。

用法速览（详见 README.md）：
  python studio.py new   myfilm --idea "一句话创意"   # 建项目 + 生成分镜
  python studio.py script     myfilm --feedback "…"  # 让 Claude 按意见修改分镜
  python studio.py characters myfilm                  # 生成角色定妆照候选
  python studio.py pick       myfilm character lin 2  # 选定候选
  python studio.py keyframes  myfilm                  # 生成每个镜头的关键帧候选
  python studio.py pick       myfilm keyframe 3 1
  python studio.py endframes  myfilm                  # （可选）生成每个镜头的尾帧候选，让镜头衔接更顺
  python studio.py pick       myfilm endframe 3 2
  python studio.py videos     myfilm                  # 图生视频（会先估算费用）；--end-frame own 使用尾帧
  python studio.py assemble   myfilm                  # 合成成片
  python studio.py review     myfilm                  # 生成审片页 review.html
  python studio.py status     myfilm
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

import providers as P

ROOT = Path(__file__).resolve().parent
PROJECTS = ROOT / "projects"

# Windows 终端 / 重定向输出时默认编码可能是 GBK，打印 ✓ 等字符会报错
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")


# =========================================================================== #
# 项目与配置
# =========================================================================== #
class Project:
    def __init__(self, name: str):
        self.name = name
        self.dir = PROJECTS / name
        if not self.dir.exists():
            sys.exit(f"项目不存在：{self.dir}（先运行 new）")
        self.cfg = yaml.safe_load((self.dir / "config.yaml").read_text("utf-8"))

    # --- 路径 ---
    @property
    def storyboard_path(self) -> Path:
        return self.dir / "storyboard.yaml"

    def char_dir(self, cid: str) -> Path:
        return self.dir / "characters" / cid

    def shot_dir(self, kind: str, sid: int) -> Path:
        return self.dir / kind / f"shot_{sid:02d}"

    # --- 分镜 ---
    def storyboard(self) -> dict:
        if not self.storyboard_path.exists():
            sys.exit("还没有分镜，先运行 script")
        return yaml.safe_load(self.storyboard_path.read_text("utf-8"))

    def save_storyboard(self, sb: dict):
        self.storyboard_path.write_text(
            yaml.safe_dump(sb, allow_unicode=True, sort_keys=False, width=1000), "utf-8"
        )

    def shots(self, only: str | None = None) -> list[dict]:
        shots = self.storyboard()["shots"]
        if only:
            want = {int(x) for x in only.split(",")}
            shots = [s for s in shots if s["id"] in want]
        return shots


def chosen(folder: Path, ext: str) -> Path | None:
    p = folder / f"chosen.{ext}"
    return p if p.exists() else None


def candidates(folder: Path, ext: str) -> list[Path]:
    return sorted(folder.glob(f"cand_*.{ext}"), key=lambda p: int(p.stem.split("_")[1]))


def next_index(folder: Path, ext: str) -> int:
    c = candidates(folder, ext)
    return int(c[-1].stem.split("_")[1]) + 1 if c else 1


def auto_pick(folder: Path, ext: str):
    """只有一个候选且还没选定时，自动选定。"""
    c = candidates(folder, ext)
    if len(c) == 1 and not chosen(folder, ext):
        shutil.copy(c[0], folder / f"chosen.{ext}")


# =========================================================================== #
# 1. 剧本 / 分镜
# =========================================================================== #
SCRIPT_SYSTEM = """You are a film director and storyboard artist creating short AI-generated story films.
Each shot will be produced by: (1) an image model that renders a single still keyframe using character reference photos,
then (2) an image-to-video model that animates that keyframe for 3-15 seconds, with native sound and lip-synced dialogue.

Design for what these models do well:
- One clear action per shot; avoid complex choreography, crowds, text on screen, or fast multi-step action.
- At most 2 named characters per shot. Keep wardrobe identical across shots unless the story requires a change.
- Vary shot sizes (wide / medium / close-up) and give each shot a purposeful camera move.
- Dialogue lines must be short enough to speak within the shot duration (about 3-4 Chinese characters per second).

Return ONLY a JSON object (no prose) with this schema:
{
  "title": str,
  "logline": str,
  "style": str,              // English. Global visual style appended to every image prompt (lighting, palette, lens, film look)
  "characters": [
    {"id": str,              // short lowercase ascii id, e.g. "lin"
     "name": str,            // display name used in prompts and dialogue
     "description": str}     // English. Precise, fixed appearance: age, ethnicity, face, hair, outfit, accessories
  ],
  "shots": [
    {"id": int,              // 1..N
     "duration": int,        // seconds, 3-15
     "characters": [str],    // character ids visible in the shot
     "shot_type": str,       // wide / medium / close-up / insert ...
     "keyframe_prompt": str, // English. The FIRST frame as a still image: setting, composition, pose, expression. Refer to characters by their name.
     "motion_prompt": str,   // English. What moves during the shot: action, camera move, ambient motion, sound cues.
     "end_frame_prompt": str, // English. The LAST frame as a still image, after the action has played out; compose it so it leads naturally into the next shot's first frame (screen direction, positions, lighting).
     "dialogue": str}        // "<speaker name>：<line>" in the requested language, or "" if none
  ]
}"""


def cmd_new(args):
    pdir = PROJECTS / args.name
    if pdir.exists():
        sys.exit(f"项目已存在：{pdir}")
    pdir.mkdir(parents=True)
    shutil.copy(ROOT / "config.yaml", pdir / "config.yaml")
    idea = Path(args.idea_file).read_text("utf-8") if args.idea_file else args.idea
    if not idea:
        sys.exit("请用 --idea 或 --idea-file 提供创意")
    (pdir / "idea.txt").write_text(idea, "utf-8")
    meta = {"target_length": args.length, "shots": args.shots}
    (pdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), "utf-8")
    print(f"✓ 已创建项目 {pdir}")
    if not args.no_script:
        args.feedback = None
        cmd_script(args)


def cmd_script(args):
    proj = Project(args.name)
    idea = (proj.dir / "idea.txt").read_text("utf-8")
    meta = json.loads((proj.dir / "meta.json").read_text("utf-8"))
    lang = proj.cfg.get("dialogue_language", "中文")

    if args.feedback and proj.storyboard_path.exists():
        current = proj.storyboard_path.read_text("utf-8")
        user = (f"Here is the current storyboard (YAML):\n\n{current}\n\n"
                f"Revise it according to this feedback from the director:\n{args.feedback}\n\n"
                f"Keep everything that the feedback does not ask to change. Dialogue language: {lang}.")
        print("→ Claude 正在按意见修改分镜…")
    else:
        n = f"about {meta['shots']} shots" if meta.get("shots") else "an appropriate number of shots"
        user = (f"Story idea:\n{idea}\n\n"
                f"Target total length: about {meta.get('target_length', 45)} seconds, {n}. "
                f"Aspect ratio {proj.cfg['aspect_ratio']}. Dialogue language: {lang}.")
        print("→ Claude 正在写剧本和分镜…")

    sb = P.claude_json(SCRIPT_SYSTEM, user, proj.cfg["llm"]["model"], proj.cfg["llm"]["max_tokens"])
    if proj.storyboard_path.exists():
        bak = proj.dir / "storyboard.prev.yaml"
        shutil.copy(proj.storyboard_path, bak)
    proj.save_storyboard(sb)
    total = sum(s["duration"] for s in sb["shots"])
    print(f"✓ 《{sb['title']}》 {len(sb['shots'])} 个镜头，约 {total} 秒")
    print(f"  请检查并直接编辑：{proj.storyboard_path}")
    print(f"  满意后运行：python studio.py characters {proj.name}")


# =========================================================================== #
# 2. 角色定妆照
# =========================================================================== #
def cmd_characters(args):
    proj = Project(args.name)
    sb = proj.storyboard()
    icfg = proj.cfg["image"]
    for ch in sb["characters"]:
        if args.only and ch["id"] not in args.only.split(","):
            continue
        folder = proj.char_dir(ch["id"])
        folder.mkdir(parents=True, exist_ok=True)
        if chosen(folder, "png") and not args.force:
            print(f"· {ch['name']} 已有定妆照，跳过（--force 重新生成）")
            continue
        prompt = (f"Character design reference photo. {ch['description']}. "
                  f"Full body, standing in a relaxed neutral pose, facing the camera, face clearly visible, "
                  f"plain light grey studio background, soft even lighting. Visual style: {sb['style']}")
        if args.note:
            prompt += f" {args.note}"
        start = next_index(folder, "png")
        n = icfg["candidates"]
        outs = [folder / f"cand_{start + i}.png" for i in range(n)]
        print(f"→ 生成角色 {ch['name']} 的 {n} 张候选…")
        P.generate_images(icfg["t2i_model"], {
            "prompt": prompt, "num_images": n, "aspect_ratio": "3:4",
            "resolution": icfg["resolution"], "output_format": "png",
        }, outs, label=ch["name"])
        auto_pick(folder, "png")
    _write_review(proj)
    print(f"✓ 完成。打开 {proj.dir / 'review.html'} 挑选，然后：")
    print(f"  python studio.py pick {proj.name} character <角色id> <候选编号>")


def _ensure_side_ref(proj: Project, ch: dict) -> Path:
    """为 Kling elements 准备一张侧面参考图（基于选定的定妆照）。"""
    folder = proj.char_dir(ch["id"])
    side = folder / "ref_side.png"
    if side.exists():
        return side
    front = chosen(folder, "png")
    print(f"  → 为 {ch['name']} 生成侧面参考图…")
    P.generate_images(proj.cfg["image"]["edit_model"], {
        "prompt": "The exact same person with identical face, hair and outfit, shown in a three-quarter side view, "
                  "full body, plain light grey background, same lighting.",
        "image_urls": [P.upload(front)], "num_images": 1, "aspect_ratio": "3:4",
        "resolution": proj.cfg["image"]["resolution"],
    }, [side], label=f"{ch['name']}-side")
    return side


VOICE_EXTS = ("mp3", "wav", "mp4", "mov")
DEFAULT_VOICE_ENDPOINT = "fal-ai/kling-video/create-voice"  # 旧项目的 config 里没有 voice_endpoint 时使用


def voice_sample(proj: Project, cid: str) -> Path | None:
    folder = proj.char_dir(cid)
    return next((folder / f"voice.{e}" for e in VOICE_EXTS if (folder / f"voice.{e}").exists()), None)


def _ensure_voice(proj: Project, ch: dict, endpoint: str) -> str | None:
    """角色有录音 voice.* 时克隆声音，voice_id 缓存在 voice.json；换了录音会自动重建。"""
    sample = voice_sample(proj, ch["id"])
    if not sample:
        return None
    cache = proj.char_dir(ch["id"]) / "voice.json"
    stamp = {"source": sample.name, "mtime_ns": sample.stat().st_mtime_ns}
    if cache.exists():
        saved = json.loads(cache.read_text("utf-8"))
        if {k: saved.get(k) for k in stamp} == stamp:
            return saved["voice_id"]
    print(f"  → 用 {sample.name} 克隆 {ch['name']} 的声音…")
    vid = P.create_voice(endpoint, sample, label=f"{ch['name']}-voice")
    cache.write_text(json.dumps({**stamp, "voice_id": vid}, ensure_ascii=False, indent=2), "utf-8")
    return vid


def _warn_voice(proj: Project, shot: dict, cmap: dict, element_names: list[str], mname: str):
    """说话的角色有录音、但这一镜用不上克隆声音时提醒。"""
    if not shot.get("dialogue"):
        return
    speaker = _split_dialogue(shot["dialogue"])[0]
    ch = next((c for c in cmap.values() if c["name"] == speaker), None)
    if not ch or not voice_sample(proj, ch["id"]) or speaker in element_names:
        return
    if not proj.cfg["video"]["models"][mname].get("use_character_elements"):
        why = f"模型 {mname} 不支持声音克隆（请用 kling3）"
    elif ch["id"] not in shot.get("characters", []):
        why = f"{speaker} 不在这一镜的 characters 里"
    else:
        why = "这一镜角色超过 3 个，只有前 3 个能绑定"
    print(f"  ! 镜头 {shot['id']}：{why}，台词会用模型自动生成的声音")


# =========================================================================== #
# 3. 关键帧
# =========================================================================== #
def _char_map(sb: dict) -> dict:
    return {c["id"]: c for c in sb["characters"]}


def cmd_keyframes(args):
    proj = Project(args.name)
    sb = proj.storyboard()
    cmap = _char_map(sb)
    icfg = proj.cfg["image"]
    for shot in proj.shots(args.shots):
        folder = proj.shot_dir("keyframes", shot["id"])
        folder.mkdir(parents=True, exist_ok=True)
        if chosen(folder, "png") and not args.force:
            print(f"· 镜头 {shot['id']} 已有关键帧，跳过（--force 重新生成）")
            continue

        refs, ref_lines = [], []
        for i, cid in enumerate(shot.get("characters", [])):
            ch = cmap[cid]
            img = chosen(proj.char_dir(cid), "png")
            if not img:
                sys.exit(f"角色 {ch['name']}（{cid}）还没有选定定妆照，先运行 characters / pick")
            refs.append(P.upload(img))
            ref_lines.append(f"Reference image {i + 1} is {ch['name']}: {ch['description']}.")

        prompt = " ".join(ref_lines) + (
            f" Create a single cinematic film still: {shot['keyframe_prompt']}. "
            f"Shot size: {shot.get('shot_type', 'medium')}. Visual style: {sb['style']}. "
        )
        if refs:
            prompt += ("Keep every character's face, hairstyle and clothing exactly the same as in their reference image. "
                       "Do not include the grey reference background. ")
        prompt += "No text, no watermark, no split screen."
        if args.note:
            prompt += f" {args.note}"

        start = next_index(folder, "png")
        n = icfg["candidates"]
        outs = [folder / f"cand_{start + i}.png" for i in range(n)]
        endpoint = icfg["edit_model"] if refs else icfg["t2i_model"]
        payload = {"prompt": prompt, "num_images": n, "aspect_ratio": proj.cfg["aspect_ratio"],
                   "resolution": icfg["resolution"], "output_format": "png"}
        if refs:
            payload["image_urls"] = refs
        print(f"→ 镜头 {shot['id']}：生成 {n} 张关键帧候选…")
        P.generate_images(endpoint, payload, outs, label=f"shot{shot['id']}")
        (folder / "prompt.txt").write_text(prompt, "utf-8")
        auto_pick(folder, "png")
    _write_review(proj)
    print(f"✓ 完成。在 review.html 挑选：python studio.py pick {proj.name} keyframe <镜头号> <候选编号>")


# =========================================================================== #
# 3b. 尾帧（end_frame_mode: own）
# =========================================================================== #
def _end_frame_prompt(shot: dict) -> str:
    """分镜里没写 end_frame_prompt 的旧项目，用运动描述推出"动作结束时"的画面。"""
    if shot.get("end_frame_prompt"):
        return shot["end_frame_prompt"]
    return f"The final moment of this shot, after this action has fully played out: {shot['motion_prompt']}"


def cmd_endframes(args):
    proj = Project(args.name)
    sb = proj.storyboard()
    cmap = _char_map(sb)
    icfg = proj.cfg["image"]
    all_shots = sb["shots"]
    for shot in proj.shots(args.shots):
        folder = proj.shot_dir("endframes", shot["id"])
        folder.mkdir(parents=True, exist_ok=True)
        if chosen(folder, "png") and not args.force:
            print(f"· 镜头 {shot['id']} 已有尾帧，跳过（--force 重新生成）")
            continue
        start = chosen(proj.shot_dir("keyframes", shot["id"]), "png")
        if not start:
            sys.exit(f"镜头 {shot['id']} 还没有选定关键帧（首帧），先运行 keyframes / pick")

        # 参考图 1 = 本镜首帧（保证场景、光线、服装连贯），之后是角色定妆照
        refs = [P.upload(start)]
        lines = ["Reference image 1 is the FIRST frame of this same shot: keep the same location, lighting, "
                 "color grade, wardrobe and overall camera setup."]
        for cid in shot.get("characters", []):
            ch = cmap[cid]
            img = chosen(proj.char_dir(cid), "png")
            if not img:
                sys.exit(f"角色 {ch['name']}（{cid}）还没有选定定妆照，先运行 characters / pick")
            refs.append(P.upload(img))
            lines.append(f"Reference image {len(refs)} is {ch['name']}: {ch['description']}.")
        # 下一镜的首帧：让本镜结尾的构图自然过渡过去
        idx = [x["id"] for x in all_shots].index(shot["id"])
        nxt = chosen(proj.shot_dir("keyframes", all_shots[idx + 1]["id"]), "png") if idx + 1 < len(all_shots) else None
        if nxt and not args.no_bridge:
            refs.append(P.upload(nxt))
            lines.append(f"Reference image {len(refs)} is the FIRST frame of the NEXT shot: compose this final frame so "
                         "the cut into it feels smooth (consistent screen direction, character positions and lighting), "
                         "but do not copy it.")

        prompt = " ".join(lines) + (
            f" Create the LAST frame of this shot as a single cinematic film still: {_end_frame_prompt(shot)}. "
            f"Shot size: {shot.get('shot_type', 'medium')}. Visual style: {sb['style']}. "
            "Keep every character's face, hairstyle and clothing exactly the same as in their reference image. "
            "No text, no watermark, no split screen.")
        if args.note:
            prompt += f" {args.note}"

        first = next_index(folder, "png")
        n = icfg["candidates"]
        outs = [folder / f"cand_{first + i}.png" for i in range(n)]
        print(f"→ 镜头 {shot['id']}：生成 {n} 张尾帧候选{'（参考下一镜首帧）' if nxt and not args.no_bridge else ''}…")
        P.generate_images(icfg["edit_model"], {
            "prompt": prompt, "image_urls": refs, "num_images": n, "aspect_ratio": proj.cfg["aspect_ratio"],
            "resolution": icfg["resolution"], "output_format": "png"}, outs, label=f"shot{shot['id']}-end")
        (folder / "prompt.txt").write_text(prompt, "utf-8")
        auto_pick(folder, "png")
    _write_review(proj)
    print(f"✓ 完成。在 review.html 挑选：python studio.py pick {proj.name} endframe <镜头号> <候选编号>")
    print(f"  然后：python studio.py videos {proj.name} --end-frame own")


# =========================================================================== #
# 4. 图生视频
# =========================================================================== #
END_FRAME_MODES = ("none", "next", "own")


def _end_frame_mode(vcfg: dict, args) -> str:
    """命令行 --end-frame 优先；其次 config 的 end_frame_mode；旧配置 chain_end_frame: true 等同 next。"""
    mode = getattr(args, "end_frame", None) or vcfg.get("end_frame_mode") or (
        "next" if vcfg.get("chain_end_frame") else "none")
    if mode not in END_FRAME_MODES:
        sys.exit(f"end_frame_mode 只能是 {' / '.join(END_FRAME_MODES)}，当前是 {mode!r}")
    return mode


def _snap(d: int, allowed: list[int]) -> int:
    return min(allowed, key=lambda a: abs(a - d))


def _split_dialogue(line: str) -> tuple[str, str]:
    for sep in ("：", ":"):
        if sep in line:
            a, b = line.split(sep, 1)
            return a.strip(), b.strip()
    return "", line.strip()


_LANG_EN = {"中文": "Mandarin Chinese", "普通话": "Mandarin Chinese", "粤语": "Cantonese",
            "英文": "English", "英语": "English", "日语": "Japanese"}


def _video_prompt(shot: dict, cmap: dict, lang: str, element_names: list[str]) -> str:
    lang = _LANG_EN.get(lang, lang)
    parts = []
    if element_names:
        parts.append(" ".join(f"@Element{i + 1} is {n}." for i, n in enumerate(element_names)))
    motion = shot["motion_prompt"].strip()
    parts.append(motion if motion.endswith((".", "!", "?", "。")) else motion + ".")
    if shot.get("dialogue"):
        speaker, line = _split_dialogue(shot["dialogue"])
        if line.isascii():  # 纯英文台词（如英文咒语）不要按 dialogue_language 念
            lang = "English"
        who = speaker or "The character"
        if speaker in element_names:  # 用 @ElementN 指明说话人，绑定的克隆声音才会生效
            who = f"@Element{element_names.index(speaker) + 1} ({speaker})"
        parts.append(f'{who} speaks in {lang}, clearly and naturally, with accurate lip sync: "{line}"')
    else:
        parts.append("No dialogue.")
    return " ".join(parts)


def cmd_videos(args):
    proj = Project(args.name)
    sb = proj.storyboard()
    cmap = _char_map(sb)
    vcfg = proj.cfg["video"]
    mname = args.model or vcfg["default_model"]
    m = vcfg["models"][mname]
    all_shots = sb["shots"]
    todo = [s for s in proj.shots(args.shots)
            if args.force or args.takes > 1 or not chosen(proj.shot_dir("clips", s["id"]), "mp4")]
    if not todo:
        print("所有镜头都已有视频。要重拍请用 --shots 3 --force 或 --takes 2")
        return

    # 检查关键帧
    for s in todo:
        if not chosen(proj.shot_dir("keyframes", s["id"]), "png"):
            sys.exit(f"镜头 {s['id']} 还没有选定关键帧，先运行 keyframes / pick")

    end_mode = _end_frame_mode(vcfg, args)
    if end_mode != "none" and not m.get("end_image_field"):
        print(f"! 模型 {mname} 不支持尾帧，忽略 end_frame_mode={end_mode}")
        end_mode = "none"
    if end_mode == "own":
        missing = [s["id"] for s in todo if not chosen(proj.shot_dir("endframes", s["id"]), "png")]
        if missing:
            sys.exit(f"镜头 {','.join(map(str, missing))} 还没有选定尾帧，先运行 "
                     f"python studio.py endframes {proj.name} --shots {','.join(map(str, missing))}")

    secs = sum(_snap(s["duration"], m["durations"]) for s in todo) * args.takes
    est = secs * m.get("est_usd_per_sec", 0)
    print(f"模型 {mname}（{m['endpoint']}）  镜头 {len(todo)} 个 × {args.takes} 条  共 {secs} 秒")
    print(f"预估费用 ≈ ${est:.2f}（仅供参考，以 fal 账单为准）")
    if not args.yes and not P.MOCK:
        if input("确认开始生成？[y/N] ").strip().lower() != "y":
            print("已取消")
            return

    lang = proj.cfg.get("dialogue_language", "中文")
    for s in todo:
        folder = proj.shot_dir("clips", s["id"])
        folder.mkdir(parents=True, exist_ok=True)
        dur = _snap(s["duration"], m["durations"])
        kf = chosen(proj.shot_dir("keyframes", s["id"]), "png")

        payload = {m["image_field"]: P.upload(kf), "duration": str(dur),
                   "generate_audio": bool(vcfg.get("generate_audio", True))}
        if m.get("supports_aspect_ratio"):
            payload["aspect_ratio"] = proj.cfg["aspect_ratio"]
        payload.update(m.get("extra", {}))

        # 尾帧：next = 下一镜首帧；own = 本镜单独生成的尾帧
        end = None
        if end_mode == "next":
            idx = [x["id"] for x in all_shots].index(s["id"])
            if idx + 1 < len(all_shots):
                end = chosen(proj.shot_dir("keyframes", all_shots[idx + 1]["id"]), "png")
        elif end_mode == "own":
            end = chosen(proj.shot_dir("endframes", s["id"]), "png")
        if end:
            payload[m["end_image_field"]] = P.upload(end)

        # Kling：角色身份参考
        element_names = []
        if m.get("use_character_elements") and s.get("characters"):
            elements = []
            for cid in s["characters"][:3]:
                ch = cmap[cid]
                front = chosen(proj.char_dir(cid), "png")
                side = _ensure_side_ref(proj, ch)
                el = {"frontal_image_url": P.upload(front), "reference_image_urls": [P.upload(side)]}
                vid = _ensure_voice(proj, ch, m.get("voice_endpoint", DEFAULT_VOICE_ENDPOINT))
                if vid:
                    el["voice_id"] = vid
                elements.append(el)
                element_names.append(ch["name"])
            payload["elements"] = elements
        _warn_voice(proj, s, cmap, element_names, mname)

        payload["prompt"] = _video_prompt(s, cmap, lang, element_names)
        (folder / "request.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")

        for t in range(args.takes):
            idx = next_index(folder, "mp4")
            out = folder / f"cand_{idx}.mp4"
            print(f"→ 镜头 {s['id']} 第 {idx} 条（{dur}s）生成中，通常需要 1-5 分钟…")
            P.generate_video(m["endpoint"], payload, out, label=f"shot{s['id']}", duration=dur)
            print(f"  ✓ {out.relative_to(proj.dir)}")
        auto_pick(folder, "mp4")
    _write_review(proj)
    print(f"✓ 完成。在 review.html 审片，选择：python studio.py pick {proj.name} clip <镜头号> <候选编号>")
    print(f"  全部选好后：python studio.py assemble {proj.name}")


# =========================================================================== #
# 选定候选
# =========================================================================== #
def cmd_pick(args):
    proj = Project(args.name)
    if args.kind == "character":
        folder, ext = proj.char_dir(args.target), "png"
        # 换了定妆照 → 侧面参考图作废
        (folder / "ref_side.png").unlink(missing_ok=True)
    elif args.kind == "keyframe":
        folder, ext = proj.shot_dir("keyframes", int(args.target)), "png"
    elif args.kind == "endframe":
        folder, ext = proj.shot_dir("endframes", int(args.target)), "png"
    else:
        folder, ext = proj.shot_dir("clips", int(args.target)), "mp4"
    src = folder / f"cand_{args.index}.{ext}"
    if not src.exists():
        sys.exit(f"找不到候选：{src}")
    shutil.copy(src, folder / f"chosen.{ext}")
    print(f"✓ 已选定 {src.relative_to(proj.dir)}")
    _write_review(proj)


# =========================================================================== #
# 5. 合成
# =========================================================================== #
_RES = {"720p": 720, "1080p": 1080}


def _target_size(aspect: str, res: str) -> tuple[int, int]:
    short = _RES.get(res, 1080)
    a, b = map(int, aspect.split(":"))
    if a >= b:
        return int(round(short * a / b / 2) * 2), short
    return short, int(round(short * b / a / 2) * 2)


def _probe(path: Path) -> tuple[float, bool]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    return float(info["format"]["duration"]), has_audio


def _ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def cmd_assemble(args):
    P.require_ffmpeg()
    proj = Project(args.name)
    sb = proj.storyboard()
    acfg = proj.cfg["assemble"]
    fps = proj.cfg.get("fps", 24)
    W, H = _target_size(proj.cfg["aspect_ratio"], proj.cfg["resolution"])
    out_dir = proj.dir / "output"
    tmp = out_dir / "_norm"
    tmp.mkdir(parents=True, exist_ok=True)

    clips, missing = [], []
    for s in sb["shots"]:
        c = chosen(proj.shot_dir("clips", s["id"]), "mp4")
        (clips.append((s, c)) if c else missing.append(s["id"]))
    if missing:
        sys.exit(f"这些镜头还没有选定视频：{missing}")

    # 1) 统一分辨率 / 帧率 / 音轨
    norm = []
    for s, c in clips:
        _, has_audio = _probe(c)
        o = tmp / f"shot_{s['id']:02d}.mp4"
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps},format=yuv420p")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(c)]
        if not has_audio:
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest"]
        cmd += ["-vf", vf, "-c:v", "libx264", "-crf", "18", "-preset", "medium",
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k"]
        if not has_audio:
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        subprocess.run(cmd + [str(o)], check=True)
        norm.append((s, o, _probe(o)[0]))
    print(f"✓ 已统一 {len(norm)} 个镜头为 {W}x{H} @ {fps}fps")

    # 2) 拼接（硬切或淡入淡出）
    joined = tmp / "joined.mp4"
    xf = float(acfg.get("crossfade") or 0)
    offsets = []
    if xf <= 0 or len(norm) == 1:
        lst = tmp / "list.txt"
        lst.write_text("".join(f"file '{o.name}'\n" for _, o, _ in norm), "utf-8")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", str(lst), "-c", "copy", str(joined)], check=True)
        t = 0.0
        for _, _, d in norm:
            offsets.append((t, t + d))
            t += d
    else:
        inputs, fc = [], []
        for _, o, _ in norm:
            inputs += ["-i", str(o)]
        t, vprev, aprev = norm[0][2], "0:v", "0:a"
        offsets.append((0.0, norm[0][2]))
        for i in range(1, len(norm)):
            off = t - xf
            fc.append(f"[{vprev}][{i}:v]xfade=transition=fade:duration={xf}:offset={off:.3f}[v{i}]")
            fc.append(f"[{aprev}][{i}:a]acrossfade=d={xf}[a{i}]")
            vprev, aprev = f"v{i}", f"a{i}"
            offsets.append((off, off + norm[i][2]))
            t = off + norm[i][2]
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(fc),
                        "-map", f"[{vprev}]", "-map", f"[{aprev}]", "-c:v", "libx264", "-crf", "18",
                        "-c:a", "aac", "-b:a", "192k", str(joined)], check=True)

    # 3) 字幕
    srt = out_dir / "subtitles.srt"
    entries = []
    for (s, _, _), (a, b) in zip(norm, offsets):
        if s.get("dialogue"):
            entries.append((a + 0.3, max(a + 0.8, b - 0.3), _split_dialogue(s["dialogue"])[1]))
    srt.write_text("".join(f"{i}\n{_ts(a)} --> {_ts(b)}\n{txt}\n\n"
                           for i, (a, b, txt) in enumerate(entries, 1)), "utf-8")

    # 4) 背景音乐 + 烧录字幕
    final = out_dir / f"{proj.name}_final.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(joined)]
    afilter, vfilter = None, None
    bgm = acfg.get("bgm")
    if bgm:
        bgm_path = (proj.dir / bgm) if not Path(bgm).is_absolute() else Path(bgm)
        if not bgm_path.exists():
            bgm_path = ROOT / bgm
        cmd += ["-stream_loop", "-1", "-i", str(bgm_path)]
        vol = acfg.get("bgm_volume", 0.18)
        if acfg.get("bgm_ducking", True):
            # 台词/音效响起时自动压低背景音乐（侧链压缩），说完再恢复
            afilter = (f"[1:a]volume={vol},afade=t=in:d=1[bg];[0:a]asplit=2[main][sc];"
                       f"[bg][sc]sidechaincompress=threshold=0.02:ratio=8:attack=15:release=400[bgd];"
                       f"[main][bgd]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]")
        else:
            afilter = (f"[1:a]volume={vol},afade=t=in:d=1[bg];"
                       f"[0:a][bg]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]")
    if acfg.get("subtitles") and acfg.get("burn_subtitles") and entries:
        # ffmpeg 在 output/ 下运行，只写文件名，避免 Windows 盘符 "C:" 在滤镜里的转义问题
        vfilter = (f"subtitles={srt.name}:force_style="
                   f"'FontName={P.SUBTITLE_FONT},FontSize=18,Outline=1,MarginV=28'")

    if afilter or vfilter:
        fc = []
        if vfilter:
            fc.append(f"[0:v]{vfilter}[vout]")
        if afilter:
            fc.append(afilter)
        cmd += ["-filter_complex", ";".join(fc),
                "-map", "[vout]" if vfilter else "0:v", "-map", "[aout]" if afilter else "0:a",
                "-c:v", "libx264", "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-movflags", "+faststart", str(final)]
    subprocess.run(cmd, check=True, cwd=out_dir)
    dur, _ = _probe(final)
    print(f"✓ 成片：{final}（{dur:.1f} 秒）")
    if entries:
        print(f"  字幕文件：{srt}（可导入剪映 / Premiere 再精修）")


# =========================================================================== #
# 审片页 & 状态
# =========================================================================== #
def _write_review(proj: Project):
    sb = proj.storyboard()
    rel = lambda p: p.relative_to(proj.dir).as_posix()
    rows = []

    def gallery(folder: Path, ext: str, kind: str, target, media="img"):
        ch = chosen(folder, ext)
        items = []
        for c in candidates(folder, ext):
            idx = c.stem.split("_")[1]
            is_chosen = ch is not None and ch.read_bytes() == c.read_bytes()
            tag = (f'<video src="{rel(c)}" controls preload="metadata"></video>' if media == "video"
                   else f'<a href="{rel(c)}" target="_blank"><img src="{rel(c)}" loading="lazy"></a>')
            cmd = f"python studio.py pick {proj.name} {kind} {target} {idx}"
            items.append(f'<figure class="{"chosen" if is_chosen else ""}">{tag}'
                         f'<figcaption>#{idx}{" ✓ 已选" if is_chosen else ""}'
                         f'<code onclick="navigator.clipboard.writeText(this.innerText)">{cmd}</code></figcaption></figure>')
        return "".join(items) or '<p class="empty">尚未生成</p>'

    rows.append("<h2>角色</h2>")
    for c in sb["characters"]:
        rows.append(f'<section><h3>{html.escape(c["name"])} <small>{c["id"]}</small></h3>'
                    f'<p class="desc">{html.escape(c["description"])}</p>'
                    f'<div class="grid">{gallery(proj.char_dir(c["id"]), "png", "character", c["id"])}</div></section>')
    rows.append("<h2>镜头</h2>")
    for s in sb["shots"]:
        ef = proj.shot_dir("endframes", s["id"])
        end_html = (f'<h4>尾帧</h4><div class="grid">{gallery(ef, "png", "endframe", s["id"])}</div>'
                    if ef.exists() else "")
        rows.append(
            f'<section><h3>镜头 {s["id"]} <small>{s.get("shot_type", "")} · {s["duration"]}s · '
            f'{", ".join(s.get("characters", []))}</small></h3>'
            f'<p class="desc"><b>画面</b> {html.escape(s["keyframe_prompt"])}<br>'
            f'<b>运动</b> {html.escape(s["motion_prompt"])}<br>'
            f'<b>台词</b> {html.escape(s.get("dialogue") or "—")}</p>'
            f'<h4>关键帧</h4><div class="grid">{gallery(proj.shot_dir("keyframes", s["id"]), "png", "keyframe", s["id"])}</div>'
            f'{end_html}'
            f'<h4>视频</h4><div class="grid">{gallery(proj.shot_dir("clips", s["id"]), "mp4", "clip", s["id"], "video")}</div>'
            f'</section>')
    final = proj.dir / "output" / f"{proj.name}_final.mp4"
    final_html = (f'<h2>成片</h2><video class="final" src="{rel(final)}" controls></video>'
                  if final.exists() else "")
    page = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(sb['title'])} · 审片</title>
<style>
:root{{--bg:#f6f5f2;--fg:#1d1d1b;--muted:#6b6a66;--card:#fff;--line:#e3e1dc;--accent:#c2410c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe8;--muted:#9a9893;--card:#1f1f1d;--line:#33322f;--accent:#fb923c}}}}
body{{margin:0;padding:24px 16px 80px;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,"PingFang SC","Noto Sans CJK SC",sans-serif;max-width:1200px;margin-inline:auto}}
h1{{margin:0 0 4px}} .logline{{color:var(--muted);margin:0 0 24px}}
h2{{border-bottom:1px solid var(--line);padding-bottom:6px;margin-top:40px}}
section{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:16px 0}}
h3{{margin:0 0 6px}} h3 small{{color:var(--muted);font-weight:400;font-size:13px;margin-left:6px}}
h4{{margin:14px 0 6px;font-size:13px;color:var(--muted);font-weight:600}}
.desc{{color:var(--muted);font-size:13px;margin:0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}}
figure{{margin:0;border:2px solid transparent;border-radius:8px;overflow:hidden;background:var(--bg)}}
figure.chosen{{border-color:var(--accent)}}
img,video{{width:100%;display:block;background:#000}}
figcaption{{padding:6px 8px;font-size:12px;display:flex;flex-direction:column;gap:4px}}
code{{font-size:11px;color:var(--muted);cursor:copy;word-break:break-all}}
.empty{{color:var(--muted);font-size:13px}} .final{{max-width:100%;border-radius:8px}}
</style></head><body>
<h1>{html.escape(sb['title'])}</h1><p class="logline">{html.escape(sb.get('logline', ''))}</p>
<p class="desc">点击命令即可复制，粘贴到终端运行来选定候选。重新运行任一步骤后刷新本页。</p>
{final_html}{''.join(rows)}</body></html>"""
    (proj.dir / "review.html").write_text(page, "utf-8")


def cmd_review(args):
    proj = Project(args.name)
    _write_review(proj)
    print(f"✓ {proj.dir / 'review.html'}")


def cmd_status(args):
    proj = Project(args.name)
    sb = proj.storyboard()
    mark = lambda ok: "✓" if ok else "·"
    print(f"《{sb['title']}》")
    for c in sb["characters"]:
        f = proj.char_dir(c["id"])
        voice = voice_sample(proj, c["id"])
        print(f"  角色 {c['name']:<8} 候选 {len(candidates(f, 'png'))}  选定 {mark(chosen(f, 'png'))}"
              f"  声音 {voice.name if voice else '自动'}")
    print(f"  尾帧模式 {_end_frame_mode(proj.cfg['video'], None)}")
    print("  镜头  关键帧(候选/选定)  尾帧(候选/选定)  视频(候选/选定)")
    for s in sb["shots"]:
        k, e, v = (proj.shot_dir(kind, s["id"]) for kind in ("keyframes", "endframes", "clips"))
        print(f"  {s['id']:>3}    {len(candidates(k, 'png'))}/{mark(chosen(k, 'png'))}"
              f"               {len(candidates(e, 'png'))}/{mark(chosen(e, 'png'))}"
              f"             {len(candidates(v, 'mp4'))}/{mark(chosen(v, 'mp4'))}")
    final = proj.dir / "output" / f"{proj.name}_final.mp4"
    print(f"  成片 {mark(final.exists())}")


# =========================================================================== #
# CLI
# =========================================================================== #
def main():
    ap = argparse.ArgumentParser(description="AI 故事短片工作流")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="新建项目并生成分镜")
    p.add_argument("name")
    p.add_argument("--idea", help="一句话或一段创意")
    p.add_argument("--idea-file", help="从文本文件读取创意 / 现成剧本")
    p.add_argument("--length", type=int, default=45, help="目标总时长（秒）")
    p.add_argument("--shots", type=int, help="期望镜头数")
    p.add_argument("--no-script", action="store_true", help="只建项目，不调用 Claude")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("script", help="（重新）生成或修改分镜")
    p.add_argument("name")
    p.add_argument("--feedback", help="修改意见；不填则从创意重新生成")
    p.set_defaults(func=cmd_script)

    p = sub.add_parser("characters", help="生成角色定妆照候选")
    p.add_argument("name")
    p.add_argument("--only", help="只处理这些角色 id，逗号分隔")
    p.add_argument("--note", help="追加到提示词的补充说明")
    p.add_argument("--force", action="store_true", help="已选定也再生成一批候选")
    p.set_defaults(func=cmd_characters)

    p = sub.add_parser("keyframes", help="生成关键帧候选")
    p.add_argument("name")
    p.add_argument("--shots", help="只处理这些镜头，如 1,3,5")
    p.add_argument("--note", help="追加到提示词的补充说明")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_keyframes)

    p = sub.add_parser("endframes", help="生成尾帧候选（配合 --end-frame own）")
    p.add_argument("name")
    p.add_argument("--shots", help="只处理这些镜头，如 1,3,5")
    p.add_argument("--note", help="追加到提示词的补充说明")
    p.add_argument("--no-bridge", action="store_true", help="不参考下一镜的首帧")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_endframes)

    p = sub.add_parser("videos", help="图生视频")
    p.add_argument("name")
    p.add_argument("--shots", help="只处理这些镜头，如 2,4")
    p.add_argument("--model", help="覆盖 config 中的 default_model，如 seedance2")
    p.add_argument("--takes", type=int, default=1, help="每个镜头生成几条")
    p.add_argument("--force", action="store_true", help="已选定也重新生成")
    p.add_argument("-y", "--yes", action="store_true", help="跳过费用确认")
    p.add_argument("--end-frame", choices=END_FRAME_MODES,
                   help="尾帧模式，覆盖 config：none 不用 / next 用下一镜首帧 / own 用 endframes 生成的尾帧")
    p.set_defaults(func=cmd_videos)

    p = sub.add_parser("pick", help="选定某个候选")
    p.add_argument("name")
    p.add_argument("kind", choices=["character", "keyframe", "endframe", "clip"])
    p.add_argument("target", help="角色 id 或镜头号")
    p.add_argument("index", type=int, help="候选编号")
    p.set_defaults(func=cmd_pick)

    p = sub.add_parser("assemble", help="合成成片")
    p.add_argument("name")
    p.set_defaults(func=cmd_assemble)

    p = sub.add_parser("review", help="刷新审片页 review.html")
    p.add_argument("name")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("status", help="查看进度")
    p.add_argument("name")
    p.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
