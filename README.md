# CheryStage

一站式 MMD 工作流 Blender 插件。

> [!WARNING]
> 插件目前处于前期开发阶段，可能存在较多 BUG 和不便于操作的地方，包括但不限于：
>
> - 无法读取到 VMD 路径
> - 缓动异常
> - 无法运行
>
> 使用前请备份 .blend 文件。遇到问题欢迎在 [Issues](../../issues) 反馈。

## 功能

- **关键帧缓动库**：36 种缓动预设，可在曲线编辑器、动画摄影表、3D 视图中使用，`Alt + E` 呼出
- **MMD 物理烘焙**：用外置的 MMD 式 Bullet 物理（[saba](https://github.com/benikabocha/saba) + [Bullet](https://github.com/bulletphysics/bullet3)）烘焙物理骨骼，结果写为关键帧
- **渲染导出**：输出类型、分辨率、帧率（含时间拉伸）、透明背景、输出位置

## 要求

- Blender 5.0+
- Windows x64
- [MMD Tools](https://mmd-blender.fandom.com/wiki/MMD_Tools)（用于导入模型和动作）

## 安装

从 [Releases](../../releases) 下载 `CheryStage_vX.Y.Z.zip`，在 Blender 中：编辑 → 偏好设置 → 扩展 → 从磁盘安装。

## 使用

插件面板位于 3D 视图侧栏（`N`）的 **CheryStage** 标签页。

### MMD 物理烘焙

1. 用 MMD Tools 导入模型和 VMD 动作
2. 选中模型，打开 **MMD 物理烘焙** 面板
3. 确认 PMX / VMD 路径（会自动检测），点击 **烘焙物理**

| 参数 | 说明 |
|---|---|
| 预热过渡帧 / 静置帧 | 开始前让物理稳定的帧数 |
| 物理步频 | 物理每秒步数，默认 120 |
| 最大子步 | 每帧最大物理子步数 |
| 物理线程数 | 0 = 自动（最多 8），1 = 单线程 |

限制：只模拟模型自身的刚体，不与场景中其他物体碰撞；修改动作后需重新烘焙。

## 构建

需要 Git、CMake、Ninja、MinGW-w64 g++、Python 3。

```bash
git clone https://github.com/CherySui/CheryStage.git
cd CheryStage
python tools/build.py
python tools/pack.py
```

`build.py` 会拉取子模块、应用 `native/mmdbake/patches/` 中的补丁并编译 `mmdbake.exe`；`pack.py` 生成 `dist/CheryStage_vX.Y.Z.zip`。

## 目录

```
chery_stage/          Blender 插件
native/mmdbake/       物理烘焙程序（C++）
  patches/            对 saba / Bullet 的补丁
  third_party/        saba、Bullet（子模块）
tools/                构建与打包脚本
```

## 许可证

[GPL-2.0-or-later](LICENSE)。第三方组件：saba（MIT）、Bullet（zlib）。
