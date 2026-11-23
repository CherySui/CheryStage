// mmdbake - bake MMD physics (Bullet, MMD-style) for a PMX model + VMD motion (or a pose
// sampled from Blender) and write the per-frame bone transforms to a .mbk file for the add-on.
//
// Usage: mmdbake <model.pmx> <motion.vmd | -> <out.mbk> [options]
//   --pose F.mbp     drive the model with poses sampled from Blender instead of the VMD
//                    (frames come from the file; --start/--end are ignored)
//   --stream PID     pose / collider files are still being written by process PID: wait for data
//   --dt S           seconds per frame (default 1/30); use 1/scene_fps for Blender input
//   --start N        first frame (default 0)
//   --end N          last frame (default: last key in the VMD)
//   --warmup N       frames used to blend from rest pose into the first pose (default 60)
//   --settle N       extra frames holding the first pose before recording (default 60)
//   --fps F          physics internal step rate, Hz (default 120)
//   --substeps N     max physics sub steps per frame (default 10)
//   --threads N      physics worker threads (default: min(8, CPU cores), 1 = single-threaded)
//   --fields F.mbf   Blender force fields (see fields.h), armature object space
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
#include <glm/gtc/matrix_transform.hpp>

#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <memory>
#include <string>
#include <vector>
#include <algorithm>
#include <chrono>
#include <thread>
#include <unordered_map>
#include <LinearMath/btThreads.h>
#include "stream_io.h"
#include "fields.h"

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
        "               [--warmup N] [--settle N] [--fps F] [--substeps N] [--threads N]\n"
        "               [--pose file.mbp] [--dt seconds] [--stream pid]\n");
}

int RunScene(const std::string& specPath, int warmup, int settle, float fps, int substeps, float dt, const std::string& fieldsPath);

int wmain(int argc, wchar_t** argv)
{
    if (argc < 3) { Usage(); return 2; }
    const bool sceneMode = ToUtf8(argv[1]) == "--scene";
    if (!sceneMode && argc < 4) { Usage(); return 2; }

    std::string modelPath = ToUtf8(argv[1]);
    std::string vmdPath   = ToUtf8(argv[2]);
    std::string outPath   = sceneMode ? std::string() : ToUtf8(argv[3]);
    int startFrame = 0, endFrame = -1, warmup = 60, settle = 60, substeps = 10;
    // more than ~8 threads only adds overhead for one model (measured)
    int threads = (int)std::min(8u, std::max(1u, std::thread::hardware_concurrency()));
    float fps = 120.0f;
    float dt = 1.0f / 30.0f;
    std::string posePath, fieldsPath;

    for (int i = sceneMode ? 3 : 4; i < argc; i++)
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
        else if (a == "--pose") posePath = next();
        else if (a == "--dt") dt = std::stof(next());
        else if (a == "--stream") streamio::Enable(std::stoul(next()));
        else if (a == "--fields") fieldsPath = next();
        else { std::fprintf(stderr, "unknown option: %s\n", a.c_str()); Usage(); return 2; }
    }

    if (sceneMode)
    {
        // Multithreaded Bullet world (must be set up before the models create their physics).
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
        int rc = RunScene(vmdPath /* = spec file */, warmup, settle, fps, substeps, dt, fieldsPath);
        // Rigid bodies were moved between worlds: skip teardown, all output is already on disk.
        std::fflush(stdout); std::fflush(stderr);
        std::_Exit(rc);
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

    auto* nodeMan = model->GetNodeManager();
    const size_t boneCount = nodeMan->GetNodeCount();

    // saba node space = MMD space with Z flipped. Blender (mmd_tools) = (x, -z, y) of that.
    const glm::mat3 C(glm::vec3(1, 0, 0), glm::vec3(0, 0, 1), glm::vec3(0, -1, 0)); // columns
    const glm::mat3 Ct = glm::transpose(C);

    const bool poseMode = !posePath.empty();
    std::unique_ptr<saba::VMDAnimation> anim;

    // ---- pose input (sampled in Blender) ----
    FILE* pfp = nullptr;
    uint32_t pNb = 0, pNf = 0;
    std::vector<int> pMap;                 // pose bone -> model node (-1: unmatched)
    std::vector<float> pFrame, pFirst;     // current / first frame, pNb * 7
    float pScale = 0.08f;
    std::vector<size_t> order;             // nodes, parents first
    std::vector<glm::mat4> restGlobal(boneCount), restLocal(boneCount);
    if (poseMode)
    {
        std::wstring wp; saba::TryToWString(posePath, wp);
        pfp = _wfopen(wp.c_str(), L"rb");
        char magic[8];
        uint32_t ver = 0; int32_t first = 0;
        if (!pfp || streamio::Read(magic, 1, 8, pfp) != 8 || std::memcmp(magic, "MMDPOSE1", 8) != 0
            || streamio::Read(&ver, 4, 1, pfp) != 1 || streamio::Read(&pNb, 4, 1, pfp) != 1
            || streamio::Read(&pNf, 4, 1, pfp) != 1 || streamio::Read(&first, 4, 1, pfp) != 1 || ver != 1)
        {
            std::fprintf(stderr, "ERROR: bad pose file: %s\n", posePath.c_str());
            return 1;
        }
        std::unordered_map<std::string, size_t> byName;
        for (size_t i = 0; i < boneCount; i++) byName.emplace(nodeMan->GetMMDNode(i)->GetName(), i);
        pMap.assign(pNb, -1);
        std::vector<glm::vec3> heads(pNb);
        size_t matched = 0;
        for (uint32_t i = 0; i < pNb; i++)
        {
            uint32_t len = 0; streamio::Read(&len, 4, 1, pfp);
            std::string name(len, '\0'); streamio::Read(name.data(), 1, len, pfp);
            streamio::Read(&heads[i], 4, 3, pfp);
            auto it = byName.find(name);
            if (it != byName.end()) { pMap[i] = (int)it->second; matched++; }
        }
        // scale (Blender units per MMD unit) from rest heads, least squares
        glm::dvec3 mb(0), mm(0); size_t n = 0;
        std::vector<std::pair<glm::dvec3, glm::dvec3>> pairs;
        for (uint32_t i = 0; i < pNb; i++) if (pMap[i] >= 0)
        {
            glm::vec3 r = glm::vec3(glm::inverse(nodeMan->GetMMDNode(pMap[i])->GetInverseInitTransform())[3]);
            glm::dvec3 m(C * r), b(heads[i]);
            pairs.emplace_back(b, m); mb += b; mm += m; n++;
        }
        if (n) { mb /= (double)n; mm /= (double)n; }
        double num = 0, den = 0;
        for (auto& pr : pairs) { glm::dvec3 db = pr.first - mb, dm = pr.second - mm; num += glm::dot(db, dm); den += glm::dot(dm, dm); }
        if (den > 0 && num > 0) pScale = (float)(num / den);
        startFrame = first;
        endFrame = first + (int)pNf - 1;
        pFrame.resize(pNb * 7); pFirst.resize(pNb * 7);
        std::printf("pose input: %u bones (%zu matched), %u frames from %d, scale %.5f, dt %.5f\n",
            pNb, matched, pNf, first, pScale, dt);
        // parents-first order and rest transforms
        std::vector<int> depth(boneCount, 0);
        for (size_t i = 0; i < boneCount; i++)
            for (auto* p = nodeMan->GetMMDNode(i)->GetParent(); p; p = p->GetParent()) depth[i]++;
        order.resize(boneCount);
        for (size_t i = 0; i < boneCount; i++) order[i] = i;
        std::stable_sort(order.begin(), order.end(), [&](size_t a, size_t b) { return depth[a] < depth[b]; });
        for (size_t i = 0; i < boneCount; i++)
        {
            auto* nd = nodeMan->GetMMDNode(i);
            restGlobal[i] = glm::inverse(nd->GetInverseInitTransform());
            restLocal[i] = glm::translate(glm::mat4(1), nd->GetInitialTranslate()) * glm::mat4_cast(nd->GetInitialRotate());
        }
    }
    else
    {
        saba::VMDFile vmd;
        if (!saba::ReadVMDFile(&vmd, vmdPath.c_str()))
        {
            std::fprintf(stderr, "ERROR: failed to read VMD: %s\n", vmdPath.c_str());
            return 1;
        }
        anim = std::make_unique<saba::VMDAnimation>();
        if (!anim->Create(model) || !anim->Add(vmd))
        {
            std::fprintf(stderr, "ERROR: failed to build animation from VMD\n");
            return 1;
        }
        if (endFrame < 0) endFrame = anim->GetMaxKeyTime();
        if (endFrame < startFrame) endFrame = startFrame;
    }
    const int frameCount = endFrame - startFrame + 1;

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
        model->UpdatePhysicsAnimation(dt);
        auto e = clk::now(); tPhys += std::chrono::duration<double>(e - d).count();
        model->UpdateNodeAnimation(true);
        model->EndAnimation();
        tNode += since(e);
    };

    // Pose mode: set every node from a Blender pose (weight < 1 blends from the rest pose), then physics.
    std::vector<glm::mat4> G(boneCount);
    std::vector<char> hasPose(boneCount, 0);
    std::vector<glm::mat4> S_b(boneCount);
    auto stepPose = [&](const std::vector<float>& fr, float weight) {
        auto a = clk::now();
        std::fill(hasPose.begin(), hasPose.end(), 0);
        for (uint32_t i = 0; i < pNb; i++)
        {
            int ni = pMap[i];
            if (ni < 0) continue;
            const float* v = &fr[i * 7];
            glm::quat qb(v[0], v[1], v[2], v[3]);
            glm::mat3 Rs = Ct * glm::mat3_cast(qb) * C;
            glm::vec3 ts = Ct * glm::vec3(v[4], v[5], v[6]) / pScale;
            glm::quat qs = glm::quat_cast(Rs);
            if (weight < 1.0f) { qs = glm::slerp(glm::quat(1, 0, 0, 0), qs, weight); ts *= weight; }
            glm::mat4 S = glm::translate(glm::mat4(1), ts) * glm::mat4_cast(glm::normalize(qs));
            S_b[ni] = S; hasPose[ni] = 1;
        }
        for (size_t ni : order)
        {
            auto* nd = nodeMan->GetMMDNode(ni);
            auto* par = nd->GetParent();
            glm::mat4 pg = par ? G[par->GetIndex()] : glm::mat4(1);
            G[ni] = hasPose[ni] ? S_b[ni] * restGlobal[ni] : pg * restLocal[ni];
            nd->SetLocalTransform(par ? glm::inverse(pg) * G[ni] : G[ni]);
            nd->SetGlobalTransform(G[ni]);
        }
        auto d = clk::now(); tNode += std::chrono::duration<double>(d - a).count();
        model->UpdatePhysicsAnimation(dt);
        tPhys += since(d);
    };
    auto readPoseFrame = [&](std::vector<float>& dst) -> bool {
        return streamio::Read(dst.data(), sizeof(float), dst.size(), pfp) == dst.size();
    };

    // Force fields: VMD input has no Blender pose to measure the scale, MMD Tools imports at 0.08
    if (!fieldsPath.empty())
    {
        if (!fields::Load(fieldsPath, poseMode ? pScale : 0.08f)) { std::fprintf(stderr, "ERROR: bad force field file: %s\n", fieldsPath.c_str()); return 1; }
        fields::Install(model->GetMMDPhysics()->GetDynamicsWorld());
        std::printf("force fields: %u\n", fields::Count());
        std::fflush(stdout);
    }

    // Warm up: blend from the rest pose into the first pose, then hold it so cloth settles.
    const int warmTotal = warmup + settle;
    if (poseMode)
    {
        if (!readPoseFrame(pFirst)) { std::fprintf(stderr, "ERROR: pose file truncated\n"); return 1; }
        for (int i = 0; i < warmTotal; i++)
        {
            stepPose(pFirst, i < warmup ? float(1 + i) / float(warmup) : 1.0f);
            std::printf("WARMUP %d/%d\n", i + 1, warmTotal);
            std::fflush(stdout);
        }
    }
    else
    {
        model->SaveBaseAnimation();
        for (int i = 0; i < warmTotal; i++)
        {
            step((float)startFrame, i < warmup ? float(1 + i) / float(warmup) : 1.0f);
            std::printf("WARMUP %d/%d\n", i + 1, warmTotal);
            std::fflush(stdout);
        }
    }

    std::vector<float> buf(boneCount * 7);
    int lastPct = -1;
    for (int f = 0; f < frameCount; f++)
    {
        if (!fields::SetFrame((unsigned)f)) { std::fprintf(stderr, "ERROR: force field file truncated\n"); return 1; }
        if (poseMode)
        {
            if (f == 0) pFrame = pFirst;
            else if (!readPoseFrame(pFrame)) { std::fprintf(stderr, "ERROR: pose file truncated at frame %d\n", f); return 1; }
            stepPose(pFrame, 1.0f);
        }
        else step((float)(startFrame + f), 1.0f);

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
    if (pfp) std::fclose(pfp);

    double sec = since(t0);
    std::printf("TIMING load=%.2f eval=%.2f morph=%.2f node=%.2f physics=%.2f output=%.2f\n",
        tLoad, tEval, tMorph, tNode, tPhys, tOut);
    std::printf("DONE %d frames in %.1f s -> %s\n", frameCount, sec, outPath.c_str());
    return 0;
}
