# CheryStage

> 一站式 MMD 工作流 Blender 插件：关键帧缓动 · MMD 物理烘焙 · 渲染导出

CheryStage（前身 CheryTools）是一个面向 MMD 动画制作的 Blender 扩展，目标是把"从导入到出片"的流程收进一个插件里。

## 功能

| 模块 | 说明 |
|---|---|
| **关键帧缓动库** | 36 种可视化缓动预设（Bezier / Bounce / Elastic），曲线编辑器、动画摄影表、3D 视图侧栏都能用，`Alt + E` 呼出调色板 |
| **MMD 物理烘焙** | 用外置的 MMD 式 Bullet 物理引擎烘焙裙子、头发、飘带，结果直接写成关键帧，又快又接近 MMD 原版效果 |
| **渲染导出** | 视频 / 图片序列、分辨率与帧率（含时间拉伸）、透明背景保留辉光、输出位置一键设置 |

## MMD 物理烘焙：为什么要做它

一切始于一条没有物理的裙子。

在 MikuMikuMoving 里烘焙好物理、导出 VMD 再导入 Blender，飘带在动，裙子却纹丝不动。查下来发现 MMM 导出 VMD 时最多只写 512 根骨骼，骨骼多的模型后面的物理骨骼（正好是裙子）直接被丢掉了。而 MMD Tools 自带的 Blender 刚体物理又慢又不像 MMD：

- Blender 的刚体系统没有开放 MMD 依赖的 Bullet 底层参数（MMD Tools 源码注释里也承认了这一点），关节弹簧只能近似；
- MMD 的碰撞分组表达不了，只能给每对"不该碰撞"的刚体单独建约束对象，一个模型就能多出几千个对象；
- 没有预热，刚体又很小（导入比例 0.08），容易被甩飞、穿模、抖动。

所以 CheryStage 换了条路：**不用 Blender 的刚体系统**，而是在外部用和 MMD 同一套规则（[saba](https://github.com/benikabocha/saba) + [Bullet](https://github.com/bulletphysics/bullet3)）把物理算完，再把物理骨骼的结果写回 Blender 关键帧。

### 实测

同一个模型（750 根骨骼、620 个刚体、1040 个关节），16 核 CPU：

| | MMD Tools（Blender 刚体） | CheryStage |
|---|---|---|
| 准备工作 | Build 刚体，场景多出 6000+ 个对象 | 无 |
| 每帧物理 | ~220 ms | ~4–6 ms |
| 7000 帧 | ~26 分钟 | **~50 秒**（含写入关键帧） |

### 特点

- **MMD 式物理**：刚体、关节、碰撞组、"物理 + 骨骼位置合并"模式都按 MMD 的方式处理，开头自动预热，避免第一帧裙子被甩飞
- **不受 VMD 限制**：没有 512 根骨骼上限，骨骼名也不会被截断到 15 字节
- **多线程**：Bullet 多线程世界 + 岛内并行求解，默认 8 线程（单模型再多反而变慢）
- **自动填路径**：根据 MMD Tools 的导入日志、模型贴图位置和动作名自动找到 PMX / VMD
- **进度可见**：3D 视口底部弹出缓动库同款进度卡片，预热 → 烘焙 → 写入三段，带切换动画，`Esc` 随时取消

### 已知限制

- 只支持 Windows（x64）
- 只模拟模型自身的刚体，不和 Blender 场景里的其他物体碰撞
- 在 Blender 里改了身体动作后，需要重新烘焙一次
- 需要 [MMD Tools](https://mmd-blender.fandom.com/wiki/MMD_Tools) 导入模型

## 安装

1. 到 [Releases](../../releases) 下载 `CheryStage_vX.Y.Z.zip`
2. Blender → 编辑 → 偏好设置 → 扩展 → 右上角下拉 → **从磁盘安装**，选择 zip（或直接把 zip 拖进 Blender 窗口）
3. 需要 Blender 5.0+

## 使用 MMD 物理烘焙

1. 用 MMD Tools 导入模型和舞蹈动作 VMD（推荐用没烘焙过物理的原版动作，导入更快）
2. 选中模型，3D 视图按 `N` → **CheryStage** 标签 → **MMD 物理烘焙**
3. 确认 PMX / VMD 路径（通常已自动填好），点 **烘焙物理**

参数说明：

| 参数 | 说明 |
|---|---|
| 预热过渡帧 / 静置帧 | 开头从初始姿势过渡、静置让布料稳定的帧数；开头裙子乱飞就调大 |
| 物理步频 | 物理内部每秒步数，动作很快时提到 240 更稳 |
| 物理线程数 | 0 = 自动（最多 8）；1 = 单线程 |

## 从源码构建

需要：Git、CMake、Ninja、MinGW-w64 g++（例如 [WinLibs](https://winlibs.com/)）、Python 3。

```bash
git clone --recursive https://github.com/CherySui/CheryStage.git
cd CheryStage
python tools/build.py   # 拉取子模块、打补丁、编译 mmdbake.exe 并放进插件
python tools/pack.py    # 打包成 dist/CheryStage_vX.Y.Z.zip
```

### 目录结构

```
chery_stage/              Blender 插件源码
  easing/                 关键帧缓动库
  mmd_bake/               MMD 物理烘焙（Blender 端）
  render_export/          渲染导出
native/mmdbake/           外置物理烘焙程序（C++）
  src/main.cpp
  patches/                对 saba / Bullet 的补丁
  third_party/            saba、Bullet（git 子模块）
tools/                    构建与打包脚本
```

### 对第三方库的修改

- `saba-multithread.patch`：给 saba 的物理世界加上可选的 Bullet 多线程（`btDiscreteDynamicsWorldMt`），线程数 ≤ 1 时行为与原版完全一致
- `bullet-mt-island-fix.patch`：修复 Bullet 2.89 多线程岛屿管理器的崩溃——两端都不是动态刚体的关节会得到岛屿 ID −1，单线程版本会跳过，多线程版本会越界访问

## 许可证

[GPL-2.0-or-later](LICENSE)

第三方组件：

- [saba](https://github.com/benikabocha/saba)：MIT License
- [Bullet Physics](https://github.com/bulletphysics/bullet3)：zlib License

各自的许可证文本随插件一起分发（`chery_stage/mmd_bake/licenses/`）。
