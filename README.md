# AI 工作空间

各个 AI use case 各占一个子目录，彼此独立（各自的依赖、配置和 README）。代码在 macOS 和 Windows 上通用。

| 目录 | 说明 |
|---|---|
| [ai-video-studio](ai-video-studio/) | AI 故事短片流水线：一句话创意 → 分镜 → 角色 → 关键帧 → 图生视频 → 成片 |

## 约定

- 新的 use case 在根目录下建一个平级文件夹，并附带它自己的 `README.md`，然后在上表中加一行。
- 密钥写在各子目录的 `.env`（已被 `.gitignore` 忽略），只提交 `.env.example`。
- 生成的大文件（图片、视频等）不进仓库，比如 `projects/`。
