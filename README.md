# CheryStage

一站式 MMD 工作流 Blender 插件。

> [!WARNING]
> 插件目前处于前期开发阶段，可能存在较多 BUG 和不便于操作的地方，包括但不限于：
>
> - 无法读取到模型和动作文件路径
> - 缓动异常
> - 无法运行
> - 物理效果与 MMD / MMM 原版存在差异
> - 部分模型的骨骼无法正确匹配，导致物理不生效或姿势错误
> - 烘焙会静音原动作中物理骨骼的通道（删除物理层即可恢复）
> - 长动画烘焙时内存占用较高
>
> 使用前请备份 .blend 文件。遇到问题欢迎在 [Issues](../../issues) 反馈。

## 功能

- **关键帧缓动库**：36 种缓动预设，可在曲线编辑器、动画摄影表、3D 视图中使用，`Alt + E` 呼出
- **MMD 物理烘焙**：用外置的 MMD 式 Bullet 物理（[saba](https://github.com/benikabocha/saba) + [Bullet](https://github.com/bulletphysics/bullet3)）烘焙物理骨骼，可按 Blender 中的动作烘焙，结果放在独立的 NLA 物理层
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

1. 用 MMD Tools 导入模型和动作（VMD 或在 Blender 中 K 帧）
2. 选中模型，打开 **MMD 物理烘焙** 面板
3. 选择动作来源，确认 PMX 路径（会自动检测），点击 **烘焙物理**

| 动作来源 | 说明 |
|---|---|
| Blender 动作（默认） | 逐帧读取 Blender 中的实际姿势，包含手 K 修改、IK、约束 |
| VMD 文件 | 读取原始 VMD，速度更快，但不包含 Blender 中的修改 |

#### 动画层

烘焙结果放在名为 `CheryStage 物理层` 的 NLA 轨道上，原动作不会被修改。

- 物理层可在面板中开关；关闭后回到原动作的效果
- 原动作中已有的物理骨骼通道会被静音，不会删除
- 重新烘焙会替换物理层
- 删除物理层会移除轨道和物理动作，并恢复被静音的通道

| 参数 | 说明 |
|---|---|
| 预热过渡帧 / 静置帧 | 开始前让物理稳定的帧数 |
| 物理步频 | 物理每秒步数，默认 120 |
| 最大子步 | 每帧最大物理子步数 |
| 物理线程数 | 0 = 自动（最多 8），1 = 单线程 |

限制：只模拟模型自身的刚体，不与场景中其他物体碰撞；修改动作后需重新烘焙；按 Blender 动作烘焙时，读取动作约 7 ms / 帧。

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
