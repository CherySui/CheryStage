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
> - 场景物理（多模型、场景碰撞体）目前处于测试阶段
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

### 从扩展仓库安装（推荐，可自动更新）

1. Blender：编辑 → 偏好设置 → 获取扩展 → 右上角 **存储库** 下拉菜单 → **+** → **添加远程存储库**
2. URL 填 `https://cherysui.github.io/CheryStage/index.json`，勾选 **启动时检查更新**
3. 在获取扩展中搜索 **CheryStage**，点击安装

之后有新版本时，获取扩展页面会提示更新。

需要在 偏好设置 → 系统 中开启 **允许联机访问**。

### 从文件安装

从 [Releases](../../releases) 下载 `CheryStage_vX.Y.Z.zip`，在 Blender 中：编辑 → 偏好设置 → 获取扩展 → 右上角下拉菜单 → 从磁盘安装。这种方式不会自动更新。

如果之前是从文件安装的，改用扩展仓库前先在 偏好设置 → 插件 中卸载旧版本。

## 使用

插件面板位于 3D 视图侧栏（`N`）的 **CheryStage** 标签页。

### MMD 物理烘焙

面板顶部有三个页签：

| 页签 | 说明 |
|---|---|
| 单人 | 烘焙选中的模型 |
| 场景 | 一次烘焙场景中的多个模型，并与场景碰撞体互动（测试阶段） |
| 配置 | 物理参数 |

#### 单人

1. 用 MMD Tools 导入模型和动作（VMD 或在 Blender 中 K 帧）
2. 选中模型，打开 **MMD 物理烘焙** 面板的 **单人** 页签
3. 选择动作来源，确认 PMX 路径（会自动检测），点击 **烘焙物理**

| 动作来源 | 说明 |
|---|---|
| Blender 动作（默认） | 逐帧读取 Blender 中的实际姿势，包含手 K 修改、IK、约束 |
| VMD 文件 | 读取原始 VMD，速度更快，但不包含 Blender 中的修改 |

#### 场景（测试阶段）

所有模型和碰撞体放在同一个物理世界中计算，各模型按在场景中的位置模拟，结果分别写入各自的物理层。动作来源固定为 Blender 动作。

- **模型**：列出场景中所有 MMD 模型，可逐个勾选是否参与烘焙；每个模型需要有 PMX 路径
- **碰撞体**：在 Blender 的刚体面板中设为 **被动** 的网格物体（MMD Tools 生成的刚体除外）。碰撞形状按刚体面板设置（盒、球、胶囊、圆柱、圆锥、凸包、网格），支持动画
- **模型之间碰撞**：默认关闭；开启后不同模型的刚体会相互碰撞
- **默认地面**：是否保留 MMD 的地面

碰撞体只会推开模型的刚体，不会被模型推动。

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

限制：单人模式只模拟模型自身的刚体，与场景物体的碰撞需使用场景页签；修改动作后需重新烘焙；按 Blender 动作烘焙时，读取动作约 7 ms / 帧。

## 构建

需要 Git、CMake、Ninja、MinGW-w64 g++、Python 3。

```bash
git clone https://github.com/CherySui/CheryStage.git
cd CheryStage
python tools/build.py
python tools/pack.py
```

`build.py` 会拉取子模块、应用 `native/mmdbake/patches/` 中的补丁并编译 `mmdbake.exe`；`pack.py` 生成 `dist/CheryStage_vX.Y.Z.zip`。

发布到扩展仓库（GitHub Pages，`gh-pages` 分支）：

```bash
python tools/repo.py --blender C:/path/to/blender.exe          # 生成 dist/repo/
python tools/repo.py --blender C:/path/to/blender.exe --push   # 生成并推送到 gh-pages
```

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
