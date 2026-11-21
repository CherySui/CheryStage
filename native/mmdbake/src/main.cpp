// mmdbake - bake MMD physics (Bullet, MMD-style) for a PMX model + VMD motion
// and write the per-frame bone transforms to a .mbk file for the Blender add-on.
//
// Usage: mmdbake <model.pmx> <motion.vmd> <out.mbk> [options]
//   --start N        first frame (default 0)
//   --end N          last frame (default: last key in the VMD)
//   --warmup N       frames used to blend from rest pose into the first pose (default 60)
//   --settle N       extra frames holding the first pose before recording (default 60)
//   --fps F          physics internal step rate, Hz (default 120)
//   --substeps N     max physics sub steps per frame (default 10)
//   --threads N      physics worker threads (default: min(8, CPU cores), 1 = single-threaded)
//
// .mbk layout (little endian):
//   char[8] "MMDBAKE1"; u32 version(=1); u32 boneCount; u32 frameCount; i32 firstFrame
//   per bone: u32 nameLen; utf8 name; u8 physicsType (0 none, 1 dynamic, 2 dynamic+bone); i32 parentIndex;
//             f32[3] rest head position (Blender axes, MMD units)
//   per frame, per bone: f32[7] = skinning transform in Blender axes, MMD units
//                        (qw, qx, qy, qz, tx, ty, tz)

#include <Saba/Model/MMD/PMXModel.h>
#include <Saba/Model/MMD/PMXFile.h>
#include <Saba/Model/MMD/VMDFile.h>
#include <Saba/Model/MMD/VMDAnimation.h>
#include <Saba/Model/MMD/MMDPhysics.h>
#include <Saba/Base/Path.h>
#include <Saba/Base/UnicodeUtil.h>

#include <glm/glm.hpp>
#include <glm/gtc/quaternion.hpp>

#include <cstdio>
#include <cstdint>
#include <cstring>
#include <memory>
#include <string>
#include <vector>
#include <algorithm>
#include <chrono>
#include <thread>
#include <LinearMath/btThreads.h>

namespace saba { extern int g_mmdPhysicsThreads; }

static std::string ToUtf8(const wchar_t* w)
{
    std::string out;
    saba::TryToUtf8String(std::wstring(w), out);
    return out;
}

static void Usage()
{
    std::fprintf(stderr,
        "usage: mmdbake <model.pmx> <motion.vmd> <out.mbk> [--start N] [--end N]\n"
        "               [--warmup N] [--settle N] [--fps F] [--substeps N] [--threads N]\n");
}

int wmain(int argc, wchar_t** argv)
{
    if (argc < 4) { Usage(); return 2; }

    std::string modelPath = ToUtf8(argv[1]);
    std::string vmdPath   = ToUtf8(argv[2]);
    std::string outPath   = ToUtf8(argv[3]);
    int startFrame = 0, endFrame = -1, warmup = 60, settle = 60, substeps = 10;
    // more than ~8 threads only adds overhead for one model (measured)
    int threads = (int)std::min(8u, std::max(1u, std::thread::hardware_concurrency()));
    float fps = 120.0f;

    for (int i = 4; i < argc; i++)
    {
        std::string a = ToUtf8(argv[i]);
        auto next = [&](void) -> std::string {
            if (i + 1 >= argc) { Usage(); std::exit(2); }
            return ToUtf8(argv[++i]);
        };
        if (a == "--start") startFrame = std::stoi(next());
        else if (a == "--end") endFrame = std::stoi(next());
        else if (a == "--warmup") warmup = std::max(1, std::stoi(next()));
        else if (a == "--settle") settle = std::max(0, std::stoi(next()));
        else if (a == "--fps") fps = std::stof(next());
        else if (a == "--substeps") substeps = std::max(1, std::stoi(next()));
        else if (a == "--threads") threads = std::max(1, std::stoi(next()));
        else { std::fprintf(stderr, "unknown option: %s\n", a.c_str()); Usage(); return 2; }
    }

    auto t0 = std::chrono::steady_clock::now();
    std::printf("STAGE load\n");
    std::fflush(stdout);

    // Raw PMX, for rigid body operation types per bone.
    saba::PMXFile pmx;
    if (!saba::ReadPMXFile(&pmx, modelPath.c_str()))
    {
        std::fprintf(stderr, "ERROR: failed to read PMX: %s\n", modelPath.c_str());
        return 1;
    }

    // Multithreaded Bullet world (must be set up before the model creates its physics).
    if (threads > 1)
    {
        threads = std::min(threads, (int)BT_MAX_THREAD_COUNT);
        if (btITaskScheduler* ts = btCreateDefaultTaskScheduler())
        {
            ts->setNumThreads(threads);
            btSetTaskScheduler(ts);
            saba::g_mmdPhysicsThreads = ts->getNumThreads();
        }
    }
    std::printf("physics threads: %d\n", std::max(1, saba::g_mmdPhysicsThreads));

    auto model = std::make_shared<saba::PMXModel>();
    std::string dataDir = saba::PathUtil::GetDirectoryName(modelPath);
    if (!model->Load(modelPath, dataDir))
    {
        std::fprintf(stderr, "ERROR: failed to load PMX model: %s\n", modelPath.c_str());
        return 1;
    }
    model->GetMMDPhysics()->SetFPS(fps);
    model->GetMMDPhysics()->SetMaxSubStepCount(substeps);
    model->InitializeAnimation();

    saba::VMDFile vmd;
    if (!saba::ReadVMDFile(&vmd, vmdPath.c_str()))
    {
        std::fprintf(stderr, "ERROR: failed to read VMD: %s\n", vmdPath.c_str());
        return 1;
    }
    auto anim = std::make_unique<saba::VMDAnimation>();
    if (!anim->Create(model) || !anim->Add(vmd))
    {
        std::fprintf(stderr, "ERROR: failed to build animation from VMD\n");
        return 1;
    }
    if (endFrame < 0) endFrame = anim->GetMaxKeyTime();
    if (endFrame < startFrame) endFrame = startFrame;
    const int frameCount = endFrame - startFrame + 1;

    auto* nodeMan = model->GetNodeManager();
    const size_t boneCount = nodeMan->GetNodeCount();

    std::vector<uint8_t> physType(boneCount, 0);
    for (const auto& rb : pmx.m_rigidbodies)
    {
        if (rb.m_boneIndex < 0 || (size_t)rb.m_boneIndex >= boneCount) continue;
        uint8_t t = 0;
        if (rb.m_op == saba::PMXRigidbody::Operation::Dynamic) t = 1;
        else if (rb.m_op == saba::PMXRigidbody::Operation::DynamicAndBoneMerge) t = 2;
        if (t == 1 || (t == 2 && physType[rb.m_boneIndex] == 0)) physType[rb.m_boneIndex] = t;
    }
    size_t physCount = std::count_if(physType.begin(), physType.end(), [](uint8_t t) { return t != 0; });

    std::printf("model bones: %zu, physics bones: %zu, rigid bodies: %zu, joints: %zu\n",
        boneCount, physCount, pmx.m_rigidbodies.size(), pmx.m_joints.size());
    std::printf("frames: %d..%d (%d)\n", startFrame, endFrame, frameCount);
    std::fflush(stdout);

    FILE* fp = nullptr;
    {
        std::wstring wout;
        saba::TryToWString(outPath, wout);
        fp = _wfopen(wout.c_str(), L"wb");
    }
    if (!fp)
    {
        std::fprintf(stderr, "ERROR: cannot open output: %s\n", outPath.c_str());
        return 1;
    }
    auto w32 = [&](uint32_t v) { std::fwrite(&v, 4, 1, fp); };
    std::fwrite("MMDBAKE1", 1, 8, fp);
    w32(1);
    w32((uint32_t)boneCount);
    w32((uint32_t)frameCount);
    int32_t ff = startFrame; std::fwrite(&ff, 4, 1, fp);
    for (size_t i = 0; i < boneCount; i++)
    {
        auto* node = nodeMan->GetMMDNode(i);
        const std::string& name = node->GetName();
        w32((uint32_t)name.size());
        std::fwrite(name.data(), 1, name.size(), fp);
        std::fwrite(&physType[i], 1, 1, fp);
        int32_t parent = node->GetParent() ? (int32_t)node->GetParent()->GetIndex() : -1;
        std::fwrite(&parent, 4, 1, fp);
        // rest head position, Blender axes, MMD units
        glm::vec3 p = glm::vec3(glm::inverse(node->GetInverseInitTransform())[3]);
        float rest[3] = { p.x, -p.z, p.y };
        std::fwrite(rest, sizeof(float), 3, fp);
    }

    // Warm up: blend from the rest pose into the first pose, then hold it so cloth settles.
    // Per-phase timers (seconds)
    using clk = std::chrono::steady_clock;
    double tEval = 0, tMorph = 0, tNode = 0, tPhys = 0, tOut = 0;
    auto since = [](clk::time_point a) { return std::chrono::duration<double>(clk::now() - a).count(); };
    double tLoad = since(t0);

    // One animation + physics step (same as MMDModel::UpdateAllAnimation, but timed).
    // weight < 1 blends from the saved base pose (used for warm-up, like SyncPhysics).
    auto step = [&](float frame, float weight) {
        auto a = clk::now();
        model->BeginAnimation();
        anim->Evaluate(frame, weight);
        auto b = clk::now(); tEval += std::chrono::duration<double>(b - a).count();
        model->UpdateMorphAnimation();
        auto c = clk::now(); tMorph += std::chrono::duration<double>(c - b).count();
        model->UpdateNodeAnimation(false);
        auto d = clk::now(); tNode += std::chrono::duration<double>(d - c).count();
        model->UpdatePhysicsAnimation(1.0f / 30.0f);
        auto e = clk::now(); tPhys += std::chrono::duration<double>(e - d).count();
        model->UpdateNodeAnimation(true);
        model->EndAnimation();
        tNode += since(e);
    };

    // Warm up: blend from the rest pose into the first pose, then hold it so cloth settles.
    const int warmTotal = warmup + settle;
    model->SaveBaseAnimation();
    for (int i = 0; i < warmTotal; i++)
    {
        step((float)startFrame, i < warmup ? float(1 + i) / float(warmup) : 1.0f);
        std::printf("WARMUP %d/%d\n", i + 1, warmTotal);
        std::fflush(stdout);
    }

    // saba node space = MMD space with Z flipped. Blender (mmd_tools) = (x, -z, y) of that.
    const glm::mat3 C(glm::vec3(1, 0, 0), glm::vec3(0, 0, 1), glm::vec3(0, -1, 0)); // columns
    const glm::mat3 Ct = glm::transpose(C);

    std::vector<float> buf(boneCount * 7);
    int lastPct = -1;
    for (int f = 0; f < frameCount; f++)
    {
        step((float)(startFrame + f), 1.0f);

        auto o0 = clk::now();
        for (size_t i = 0; i < boneCount; i++)
        {
            auto* node = nodeMan->GetMMDNode(i);
            glm::mat4 S = node->GetGlobalTransform() * node->GetInverseInitTransform();
            glm::mat3 R = C * glm::mat3(S) * Ct;
            glm::vec3 t = C * glm::vec3(S[3]);
            glm::quat q = glm::normalize(glm::quat_cast(R));
            float* o = &buf[i * 7];
            o[0] = q.w; o[1] = q.x; o[2] = q.y; o[3] = q.z;
            o[4] = t.x; o[5] = t.y; o[6] = t.z;
        }
        std::fwrite(buf.data(), sizeof(float), buf.size(), fp);
        tOut += since(o0);

        int pct = (int)((f + 1) * 200LL / frameCount);
        if (pct != lastPct)
        {
            lastPct = pct;
            std::printf("PROGRESS %d/%d\n", f + 1, frameCount);
            std::fflush(stdout);
        }
    }
    std::fclose(fp);

    double sec = since(t0);
    std::printf("TIMING load=%.2f eval=%.2f morph=%.2f node=%.2f physics=%.2f output=%.2f\n",
        tLoad, tEval, tMorph, tNode, tPhys, tOut);
    std::printf("DONE %d frames in %.1f s -> %s\n", frameCount, sec, outPath.c_str());
    return 0;
}
