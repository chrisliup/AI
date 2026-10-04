# AI 故事短片工作流

半自动流水线：**一句话创意 → 剧本分镜 → 角色定妆照 → 关键帧 → 图生视频 → 合成成片**。
每一步都把结果存到项目目录，你在审片页里挑选、修改，满意了再进下一步，花钱最多的视频环节只对你确认过的画面下手。

```
创意 ──Claude──▶ storyboard.yaml ──Nano Banana 2──▶ 角色定妆照 ──Nano Banana 2 Edit──▶ 关键帧
  (你编辑)              (你挑选)                         (以定妆照为参考, 你挑选)
                                                                     │
成片 ◀──ffmpeg── 选定的镜头 ◀── Kling 3.0 Pro / Seedance 2.0 图生视频（原生对白+音效）
(字幕/BGM/转场)     (你挑选，可多拍几条)
```

## 为什么这样设计

| 环节 | 选型 | 理由 |
|---|---|---|
| 剧本/分镜 | Claude（`claude-sonnet-5-5`，可改 Opus） | 输出结构化分镜，按"AI 视频擅长什么"来设计镜头 |
| 角色 & 关键帧 | `fal-ai/nano-banana-2` + `/edit` | 多参考图编辑，把同一张定妆照放进不同场景，角色一致性最稳；约 $0.08/张 |
| 图生视频（默认） | `fal-ai/kling-video/v3/pro/image-to-video` | 支持 `elements` 角色身份参考、中文原生对白，3–15 秒；约 $0.14/秒 |
| 图生视频（备选） | `bytedance/seedance-2.0/image-to-video` | 运动和画质出色，支持首尾帧、4–15 秒 |
| 合成 | ffmpeg | 统一规格、转场、字幕、背景音乐 |

全部媒体模型都走 **fal.ai** 一个 Key，换模型只改 `config.yaml` 里一行。核心思路是**先图后视频**：关键帧便宜、可控，挑好再动起来，比直接文生视频省钱得多，角色也更一致。

## 安装

同一份代码在 macOS 和 Windows 上都能运行。需要 Python 3.10+ 和 ffmpeg。

**macOS**（终端）

```bash
brew install python ffmpeg
cd ai-video-studio
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # 填入 ANTHROPIC_API_KEY 和 FAL_KEY
```

**Windows**（PowerShell）

```powershell
winget install --id Python.Python.3.12 -e
winget install --id Gyan.FFmpeg -e        # 装完重开终端，让 PATH 生效
cd ai-video-studio
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env               # 填入 ANTHROPIC_API_KEY 和 FAL_KEY
```

`.env` 会在程序启动时自动读取，不需要 `source`。终端里已设置的同名环境变量优先。

- Anthropic Key：console.anthropic.com
- fal Key：fal.ai → Dashboard → Keys（按量付费，先充少量额度）

**先空跑一遍**（不花钱，用占位图/占位视频验证流程）：把 `.env` 里的 `STUDIO_MOCK` 设为 `1`，然后

```bash
python studio.py new test --idea "随便写"
python studio.py characters test
```

也可以只对当前终端临时开启：macOS 用 `export STUDIO_MOCK=1`，Windows PowerShell 用 `$env:STUDIO_MOCK="1"`。

> 下文命令统一写作 `python`。macOS 上如果没有激活虚拟环境，请用 `python3`。

## 一部片子的完整流程

```bash
# 1. 建项目，Claude 写分镜（也可 --idea-file 剧本.txt 导入现成剧本）
python studio.py new subway --idea "深夜末班地铁上，疲惫的女孩遇见一位知道她名字的老人" --length 45

# 2. 审分镜：直接编辑 projects/subway/storyboard.yaml，或者让 Claude 改
python studio.py script subway --feedback "第三镜改成老人递给她一张旧车票，结尾留悬念"

# 3. 角色定妆照（每个角色出 3 张候选）→ 打开 review.html 挑
python studio.py characters subway
python studio.py pick subway character lin 2
#    不满意：python studio.py characters subway --only lin --force --note "更瘦一点，短发"
#    也可以直接把你自己的图放到 characters/lin/chosen.png

# 4. 关键帧（每镜 3 张候选）→ 挑
python studio.py keyframes subway
python studio.py pick subway keyframe 2 3
#    单独重做：python studio.py keyframes subway --shots 2 --force --note "低机位，更暗"

# 5. 图生视频（先显示预估费用，确认后开跑）
python studio.py videos subway                  # 每镜 1 条
python studio.py videos subway --shots 3 --takes 2   # 某镜多拍两条挑
python studio.py videos subway --shots 1 --model seedance2 --force  # 换模型重拍
python studio.py pick subway clip 3 2

# 6. 合成
python studio.py assemble subway   # → projects/subway/output/subway_final.mp4 + subtitles.srt
```

随时可用 `python studio.py status subway` 看进度，`review` 刷新审片页。

## 审片页 review.html

每一步都会自动刷新 `projects/<项目>/review.html`。用浏览器打开就能看到所有角色、关键帧、视频候选，以及已选中的项（橙色边框）。点击下面的命令即可复制，粘贴到终端完成挑选。

## 项目目录

```
projects/subway/
├── config.yaml          # 本项目配置（从全局复制，可单独改）
├── idea.txt
├── storyboard.yaml      # 分镜 ← 最主要的人工编辑点
├── characters/lin/      # cand_*.png 候选, chosen.png 选定, ref_side.png 侧面参考
├── keyframes/shot_01/   # cand_*.png, chosen.png, prompt.txt（实际发送的提示词）
├── clips/shot_01/       # cand_*.mp4, chosen.mp4, request.json（实际请求参数）
├── output/              # 成片 + 字幕
└── review.html
```

## 常用调整（config.yaml）

- **竖屏**：`aspect_ratio: "9:16"`
- **省钱打样**：`image.resolution: "1K"`、`image.candidates: 2`、Seedance 的 `resolution: "720p"`；定稿后再 `--force` 用高规格重跑
- **镜头衔接更顺**：`video.chain_end_frame: true`，用下一镜关键帧作本镜尾帧（动作会更受限）
- **背景音乐**：把 mp3 放进项目目录，`assemble.bgm: "assets/bgm.mp3"`
- **转场**：`assemble.crossfade: 0.4`
- **换模型**：在 `video.models` 下照格式加一项（fal 上的 endpoint + 图片字段名），例如 Veo 3.1、MiniMax H3

## 成片质量的经验

1. **分镜是一切的基础**：每镜只做一件事，同框最多 2 个角色；台词短（约每秒 3–4 个字）。
2. **定妆照多花时间**：选正面、五官清楚、服装完整的那张；后面所有一致性都靠它。
3. **关键帧决定构图，提示词决定运动**：视频提示词写"谁做什么动作 + 镜头怎么动"，别再描述外貌。
4. **重要镜头多拍几条**（`--takes 3`），AI 视频的随机性很大，挑比改划算。
5. 原生对白如果口型或发音不理想，可以关掉 `generate_audio`，后期用配音 + 字幕。
6. 合成后的 `subtitles.srt` 和各镜头 `chosen.mp4` 都可以导入剪映 / Premiere 精剪。

## 费用参考（以 fal 实时价格为准）

一部 45 秒、8 个镜头的短片，典型花费：角色 2×3 张 + 关键帧 8×3 张 ≈ $3–4；视频 45 秒 × Kling 3 Pro ≈ $6–7（每镜多拍一条则翻倍）。剧本调用 Claude 通常不到 $0.1。
