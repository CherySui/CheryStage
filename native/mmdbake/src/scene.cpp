// mmdbake scene mode: several MMD models + scene colliders in ONE shared Bullet world.
//
//   mmdbake --scene <spec.txt> [--warmup N] [--settle N] [--fps F] [--substeps N] [--threads N] [--dt S]
//
// spec.txt (UTF-8, one item per line, fields separated by TAB):
//   model      <model.pmx> <pose.mbp> <out.mbk>      (one line per model, pose file version 2)
//   colliders  <file.mbc>                             (optional)
//   fields     <file.mbf>                             (optional, Blender world; see fields.h)
//   inter_model 0|1                                   (collide rigid bodies of different models, default 0)
//   ground     0|1                                    (saba's infinite ground plane at Blender z=0, default 1)
//
// .mbp v2 (written by the Blender add-on): like v1, but each frame starts with f32[7] =
//   armature object world transform (qw,qx,qy,qz,tx,ty,tz; Blender world, rotation+translation, no scale).
//
// .mbc (scene colliders, Blender world):
//   char[8] "MMDCOLL1"; u32 version(=1); u32 count; u32 frames; i32 firstFrame
//   per collider: u32 nameLen; utf8 name; u8 shape (0 box, 1 sphere, 2 capsule, 3 cylinder, 4 cone,
//                 5 convex hull, 6 triangle mesh); f32 margin; f32[3] size; f32[3] offset;
//                 u32 vertexCount; f32[3*n] vertices; u32 triangleCount; u32[3*m] indices
//     size: box = full extents; sphere = (radius); capsule/cylinder/cone = (radius, -, height along local Z)
//     size/offset/vertices are object-local, already multiplied by the object scale (Blender units)
//   per frame, per collider: f32[7] = world transform without scale (qw,qx,qy,qz,tx,ty,tz)
//
// Each model's .mbk output is identical in layout to single-model mode and is expressed in the
// model's armature space (the armature's world transform is removed again on output).

#include <Saba/Model/MMD/PMXModel.h>
#include <Saba/Model/MMD/PMXFile.h>
#include <Saba/Model/MMD/MMDPhysics.h>
#include <Saba/Base/Path.h>
#include <Saba/Base/UnicodeUtil.h>

#include <btBulletCollisionCommon.h>
#include <btBulletDynamicsCommon.h>

#include <glm/glm.hpp>
#include <glm/gtc/quaternion.hpp>
#include <glm/gtc/matrix_transform.hpp>

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <memory>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

#include "stream_io.h"
#include "fields.h"

namespace
{
    // saba node space = MMD space with Z flipped; Blender (mmd_tools) = (x, -z, y) of saba space.
    const glm::mat3 C(glm::vec3(1, 0, 0), glm::vec3(0, 0, 1), glm::vec3(0, -1, 0)); // columns: saba -> Blender
    const glm::mat3 Ct = glm::transpose(C);
    // Bullet space = MMD space = Blender with Y/Z swapped (a reflection, so rotations are conjugated)
    const glm::mat3 P(glm::vec3(1, 0, 0), glm::vec3(0, 0, 1), glm::vec3(0, 1, 0));

    FILE* OpenW(const std::string& path, const wchar_t* mode)
    {
        std::wstring w; saba::TryToWString(path, w);
        return _wfopen(w.c_str(), mode);
    }

    template <class T> bool Rd(FILE* f, T& v) { return streamio::Read(&v, sizeof(T), 1, f) == 1; }

    glm::mat4 RT(const float* v, float invScale, const glm::mat3& A, const glm::mat3& At)
    {
        // v = (qw,qx,qy,qz,tx,ty,tz) in Blender axes; returns the same transform in another basis
        glm::mat3 R = At * glm::mat3_cast(glm::quat(v[0], v[1], v[2], v[3])) * A;
        glm::vec3 t = At * glm::vec3(v[4], v[5], v[6]) * invScale;
        glm::mat4 M(R);
        M[3] = glm::vec4(t, 1);
        return M;
    }

    struct SModel
    {
        std::string pmxPath, posePath, outPath;
        std::shared_ptr<saba::PMXModel> model;
        saba::PMXFile pmx;
        saba::MMDNodeManager* nm = nullptr;
        size_t nb = 0;
        std::vector<uint8_t> physType;
        FILE* pin = nullptr;
        FILE* out = nullptr;
        uint32_t pNb = 0, pNf = 0;
        int32_t first = 0;
        std::vector<int> pMap;
        float scale = 0.08f;
        std::vector<size_t> order;
        std::vector<glm::mat4> restGlobal, restLocal, G, S;
        std::vector<char> has;
        std::vector<float> cur, firstFrame;   // 7 (armature) + pNb * 7
        glm::mat4 W{1}, Winv{1};
        std::vector<float> obuf;
    };

    struct Collider
    {
        std::string name;
        std::unique_ptr<btCollisionShape> shape;
        std::unique_ptr<btTriangleIndexVertexArray> meshIface;
        std::vector<btScalar> verts;
        std::vector<int> idx;
        std::unique_ptr<btDefaultMotionState> ms;
        std::unique_ptr<btRigidBody> body;
        glm::vec3 offsetB{0};    // Bullet units, object local
    };

    // Collision filter for the shared world.
    //   userIndex2: >= 0 model index, -2 scene collider, anything else (ground) always collides.
    struct SceneFilter : public btOverlapFilterCallback
    {
        bool interModel = false;
        bool needBroadphaseCollision(btBroadphaseProxy* p0, btBroadphaseProxy* p1) const override
        {
            auto* o0 = static_cast<btCollisionObject*>(p0->m_clientObject);
            auto* o1 = static_cast<btCollisionObject*>(p1->m_clientObject);
            int k0 = o0 ? o0->getUserIndex2() : -1, k1 = o1 ? o1->getUserIndex2() : -1;
            if ((k0 < 0 && k0 != -2) || (k1 < 0 && k1 != -2)) return true;   // ground
            if (k0 == -2 && k1 == -2) return false;                          // collider vs collider
            if (k0 == -2 || k1 == -2) return true;                           // collider vs model body
            if (k0 != k1 && !interModel) return false;
            return (p0->m_collisionFilterGroup & p1->m_collisionFilterMask) != 0
                && (p1->m_collisionFilterGroup & p0->m_collisionFilterMask) != 0;
        }
    };

    bool ReadPoseHeader(SModel& m)
    {
        m.pin = OpenW(m.posePath, L"rb");
        char magic[8]; uint32_t ver = 0;
        if (!m.pin || streamio::Read(magic, 1, 8, m.pin) != 8 || std::memcmp(magic, "MMDPOSE1", 8) != 0
            || !Rd(m.pin, ver) || !Rd(m.pin, m.pNb) || !Rd(m.pin, m.pNf) || !Rd(m.pin, m.first) || ver != 2)
            return false;
        std::unordered_map<std::string, size_t> byName;
        for (size_t i = 0; i < m.nb; i++) byName.emplace(m.nm->GetMMDNode(i)->GetName(), i);
        m.pMap.assign(m.pNb, -1);
        std::vector<glm::vec3> heads(m.pNb);
        for (uint32_t i = 0; i < m.pNb; i++)
        {
            uint32_t len = 0; Rd(m.pin, len);
            std::string name(len, '\0'); streamio::Read(name.data(), 1, len, m.pin);
            streamio::Read(&heads[i], 4, 3, m.pin);
            auto it = byName.find(name);
            if (it != byName.end()) m.pMap[i] = (int)it->second;
        }
        glm::dvec3 mb(0), mm(0); size_t n = 0;
        std::vector<std::pair<glm::dvec3, glm::dvec3>> pairs;
        for (uint32_t i = 0; i < m.pNb; i++) if (m.pMap[i] >= 0)
        {
            glm::vec3 r = glm::vec3(glm::inverse(m.nm->GetMMDNode(m.pMap[i])->GetInverseInitTransform())[3]);
            glm::dvec3 a(C * r), b(heads[i]);
            pairs.emplace_back(b, a); mb += b; mm += a; n++;
        }
        if (n) { mb /= (double)n; mm /= (double)n; }
        double num = 0, den = 0;
        for (auto& pr : pairs) { glm::dvec3 db = pr.first - mb, dm = pr.second - mm; num += glm::dot(db, dm); den += glm::dot(dm, dm); }
        if (den > 0 && num > 0) m.scale = (float)(num / den);
        m.cur.resize(7 + m.pNb * 7); m.firstFrame.resize(7 + m.pNb * 7);
        return true;
    }

    bool ReadPoseFrame(SModel& m, std::vector<float>& dst)
    {
        return streamio::Read(dst.data(), sizeof(float), dst.size(), m.pin) == dst.size();
    }

    // Set every node of a model from a pose frame (weight < 1 blends from the rest pose).
    // Nodes end up in world space: armature world transform W applied on top.
    void SetPose(SModel& m, const std::vector<float>& fr, float weight, float worldInvScale)
    {
        m.W = RT(fr.data(), worldInvScale, C, Ct);        // armature world -> saba space
        m.Winv = glm::inverse(m.W);
        std::fill(m.has.begin(), m.has.end(), 0);
        for (uint32_t i = 0; i < m.pNb; i++)
        {
            int ni = m.pMap[i];
            if (ni < 0) continue;
            const float* v = &fr[7 + i * 7];
            glm::quat qs = glm::quat_cast(Ct * glm::mat3_cast(glm::quat(v[0], v[1], v[2], v[3])) * C);
            glm::vec3 ts = Ct * glm::vec3(v[4], v[5], v[6]) / m.scale;
            if (weight < 1.0f) { qs = glm::slerp(glm::quat(1, 0, 0, 0), qs, weight); ts *= weight; }
            m.S[ni] = glm::translate(glm::mat4(1), ts) * glm::mat4_cast(glm::normalize(qs));
            m.has[ni] = 1;
        }
        for (size_t ni : m.order)
        {
            auto* nd = m.nm->GetMMDNode(ni);
            auto* par = nd->GetParent();
            glm::mat4 pg = par ? m.G[par->GetIndex()] : m.W;
            m.G[ni] = m.has[ni] ? m.W * m.S[ni] * m.restGlobal[ni] : pg * m.restLocal[ni];
            nd->SetLocalTransform(par ? glm::inverse(pg) * m.G[ni] : m.G[ni]);
            nd->SetGlobalTransform(m.G[ni]);
        }
    }

    void WriteHeader(SModel& m, int frameCount, int firstFrame)
    {
        auto w32 = [&](uint32_t v) { std::fwrite(&v, 4, 1, m.out); };
        std::fwrite("MMDBAKE1", 1, 8, m.out);
        w32(1); w32((uint32_t)m.nb); w32((uint32_t)frameCount);
        int32_t ff = firstFrame; std::fwrite(&ff, 4, 1, m.out);
        for (size_t i = 0; i < m.nb; i++)
        {
            auto* node = m.nm->GetMMDNode(i);
            const std::string& name = node->GetName();
            w32((uint32_t)name.size());
            std::fwrite(name.data(), 1, name.size(), m.out);
            std::fwrite(&m.physType[i], 1, 1, m.out);
            int32_t parent = node->GetParent() ? (int32_t)node->GetParent()->GetIndex() : -1;
            std::fwrite(&parent, 4, 1, m.out);
            glm::vec3 p = glm::vec3(glm::inverse(node->GetInverseInitTransform())[3]);
            float rest[3] = { p.x, -p.z, p.y };
            std::fwrite(rest, sizeof(float), 3, m.out);
        }
        m.obuf.resize(m.nb * 7);
    }

    void WriteFrame(SModel& m)
    {
        for (size_t i = 0; i < m.nb; i++)
        {
            auto* node = m.nm->GetMMDNode(i);
            glm::mat4 S = m.Winv * node->GetGlobalTransform() * node->GetInverseInitTransform();  // armature space
            glm::mat3 R = C * glm::mat3(S) * Ct;
            glm::vec3 t = C * glm::vec3(S[3]);
            glm::quat q = glm::normalize(glm::quat_cast(R));
            float* o = &m.obuf[i * 7];
            o[0] = q.w; o[1] = q.x; o[2] = q.y; o[3] = q.z;
            o[4] = t.x; o[5] = t.y; o[6] = t.z;
        }
        std::fwrite(m.obuf.data(), sizeof(float), m.obuf.size(), m.out);
    }

    bool LoadColliders(const std::string& path, std::vector<Collider>& out, FILE*& fp,
                       uint32_t& nc, uint32_t& nf, int32_t& first, float invScale)
    {
        fp = OpenW(path, L"rb");
        char magic[8]; uint32_t ver = 0;
        if (!fp || streamio::Read(magic, 1, 8, fp) != 8 || std::memcmp(magic, "MMDCOLL1", 8) != 0
            || !Rd(fp, ver) || !Rd(fp, nc) || !Rd(fp, nf) || !Rd(fp, first) || ver != 1)
            return false;
        out.resize(nc);
        auto toB = [&](const glm::vec3& v) { return P * v * invScale; };   // Blender local -> Bullet local
        for (uint32_t i = 0; i < nc; i++)
        {
            Collider& c = out[i];
            uint32_t len = 0; Rd(fp, len);
            c.name.resize(len); streamio::Read(c.name.data(), 1, len, fp);
            uint8_t shape = 0; float margin = 0; glm::vec3 size, offset;
            Rd(fp, shape); Rd(fp, margin); streamio::Read(&size, 4, 3, fp); streamio::Read(&offset, 4, 3, fp);
            uint32_t nv = 0; Rd(fp, nv);
            std::vector<float> v(nv * 3); if (nv) streamio::Read(v.data(), 4, v.size(), fp);
            uint32_t nt = 0; Rd(fp, nt);
            std::vector<uint32_t> tri(nt * 3); if (nt) streamio::Read(tri.data(), 4, tri.size(), fp);
            c.offsetB = toB(offset);
            float r = size.x * invScale, h = size.z * invScale;
            switch (shape)
            {
            case 0: { glm::vec3 he = glm::abs(toB(size)) * 0.5f; c.shape = std::make_unique<btBoxShape>(btVector3(he.x, he.y, he.z)); break; }
            case 1: c.shape = std::make_unique<btSphereShape>(r); break;
            case 2: c.shape = std::make_unique<btCapsuleShape>(r, std::max(0.0f, h - 2 * r)); break;
            case 3: c.shape = std::make_unique<btCylinderShape>(btVector3(r, h * 0.5f, r)); break;
            case 4: c.shape = std::make_unique<btConeShape>(r, h); break;
            case 5:
            {
                auto hull = std::make_unique<btConvexHullShape>();
                for (uint32_t k = 0; k < nv; k++) { glm::vec3 p = toB(glm::vec3(v[k * 3], v[k * 3 + 1], v[k * 3 + 2])); hull->addPoint(btVector3(p.x, p.y, p.z), false); }
                hull->recalcLocalAabb();
                c.shape = std::move(hull);
                break;
            }
            default:
            {
                c.verts.resize(nv * 3);
                for (uint32_t k = 0; k < nv; k++) { glm::vec3 p = toB(glm::vec3(v[k * 3], v[k * 3 + 1], v[k * 3 + 2])); c.verts[k * 3] = p.x; c.verts[k * 3 + 1] = p.y; c.verts[k * 3 + 2] = p.z; }
                c.idx.assign(tri.begin(), tri.end());
                if (nt == 0) { c.shape = std::make_unique<btSphereShape>(0.01f); break; }
                c.meshIface = std::make_unique<btTriangleIndexVertexArray>((int)nt, c.idx.data(), 3 * (int)sizeof(int),
                    (int)nv, c.verts.data(), 3 * (int)sizeof(btScalar));
                c.shape = std::make_unique<btBvhTriangleMeshShape>(c.meshIface.get(), true);
                break;
            }
            }
            if (margin > 0 && shape != 1) c.shape->setMargin(margin * invScale);
            c.ms = std::make_unique<btDefaultMotionState>();
            btRigidBody::btRigidBodyConstructionInfo info(0, c.ms.get(), c.shape.get(), btVector3(0, 0, 0));
            info.m_friction = 0.5f;
            c.body = std::make_unique<btRigidBody>(info);
            c.body->setCollisionFlags(c.body->getCollisionFlags() | btCollisionObject::CF_KINEMATIC_OBJECT);
            c.body->setActivationState(DISABLE_DEACTIVATION);
            c.body->setUserIndex2(-2);
        }
        return true;
    }

    void SetColliderTransform(Collider& c, const float* v, float invScale)
    {
        glm::mat3 R = P * glm::mat3_cast(glm::quat(v[0], v[1], v[2], v[3])) * P;
        glm::vec3 t = P * glm::vec3(v[4], v[5], v[6]) * invScale + R * c.offsetB;
        glm::quat q = glm::normalize(glm::quat_cast(R));
        btTransform T(btQuaternion(q.x, q.y, q.z, q.w), btVector3(t.x, t.y, t.z));
        c.ms->setWorldTransform(T);
        c.body->setWorldTransform(T);
    }
}

int RunScene(const std::string& specPath, int warmup, int settle, float fps, int substeps, float dt, const std::string& fieldsArg)
{
    using clk = std::chrono::steady_clock;
    auto t0 = clk::now();
    auto since = [](clk::time_point a) { return std::chrono::duration<double>(clk::now() - a).count(); };
    std::printf("STAGE load\n"); std::fflush(stdout);

    // ---- spec ----
    std::vector<SModel> models;
    std::string collPath, fieldsPath = fieldsArg;
    bool interModel = false, ground = true;
    {
        FILE* f = OpenW(specPath, L"rb");
        if (!f) { std::fprintf(stderr, "ERROR: cannot open scene spec: %s\n", specPath.c_str()); return 1; }
        std::string text; char buf[4096]; size_t n;
        while ((n = std::fread(buf, 1, sizeof(buf), f)) > 0) text.append(buf, n);
        std::fclose(f);
        std::istringstream ss(text); std::string line;
        while (std::getline(ss, line))
        {
            if (!line.empty() && line.back() == '\r') line.pop_back();
            std::vector<std::string> fld; std::string cur; std::istringstream ls(line);
            while (std::getline(ls, cur, '\t')) fld.push_back(cur);
            if (fld.empty()) continue;
            if (fld[0] == "model" && fld.size() >= 4) { SModel m; m.pmxPath = fld[1]; m.posePath = fld[2]; m.outPath = fld[3]; models.push_back(std::move(m)); }
            else if (fld[0] == "colliders" && fld.size() >= 2) collPath = fld[1];
            else if (fld[0] == "fields" && fld.size() >= 2) fieldsPath = fld[1];
            else if (fld[0] == "inter_model" && fld.size() >= 2) interModel = fld[1] == "1";
            else if (fld[0] == "ground" && fld.size() >= 2) ground = fld[1] == "1";
        }
    }
    if (models.empty()) { std::fprintf(stderr, "ERROR: scene spec has no model\n"); return 1; }

    // ---- models ----
    for (auto& m : models)
    {
        if (!saba::ReadPMXFile(&m.pmx, m.pmxPath.c_str())) { std::fprintf(stderr, "ERROR: failed to read PMX: %s\n", m.pmxPath.c_str()); return 1; }
        m.model = std::make_shared<saba::PMXModel>();
        if (!m.model->Load(m.pmxPath, saba::PathUtil::GetDirectoryName(m.pmxPath))) { std::fprintf(stderr, "ERROR: failed to load PMX: %s\n", m.pmxPath.c_str()); return 1; }
        m.model->InitializeAnimation();
        m.nm = m.model->GetNodeManager();
        m.nb = m.nm->GetNodeCount();
        m.physType.assign(m.nb, 0);
        for (const auto& rb : m.pmx.m_rigidbodies)
        {
            if (rb.m_boneIndex < 0 || (size_t)rb.m_boneIndex >= m.nb) continue;
            uint8_t t = rb.m_op == saba::PMXRigidbody::Operation::Dynamic ? 1 : rb.m_op == saba::PMXRigidbody::Operation::DynamicAndBoneMerge ? 2 : 0;
            if (t == 1 || (t == 2 && m.physType[rb.m_boneIndex] == 0)) m.physType[rb.m_boneIndex] = t;
        }
        if (!ReadPoseHeader(m)) { std::fprintf(stderr, "ERROR: bad pose file (v2 expected): %s\n", m.posePath.c_str()); return 1; }
        std::vector<int> depth(m.nb, 0);
        for (size_t i = 0; i < m.nb; i++) for (auto* p = m.nm->GetMMDNode(i)->GetParent(); p; p = p->GetParent()) depth[i]++;
        m.order.resize(m.nb);
        for (size_t i = 0; i < m.nb; i++) m.order[i] = i;
        std::stable_sort(m.order.begin(), m.order.end(), [&](size_t a, size_t b) { return depth[a] < depth[b]; });
        m.restGlobal.resize(m.nb); m.restLocal.resize(m.nb); m.G.resize(m.nb); m.S.resize(m.nb); m.has.resize(m.nb);
        for (size_t i = 0; i < m.nb; i++)
        {
            auto* nd = m.nm->GetMMDNode(i);
            m.restGlobal[i] = glm::inverse(nd->GetInverseInitTransform());
            m.restLocal[i] = glm::translate(glm::mat4(1), nd->GetInitialTranslate()) * glm::mat4_cast(nd->GetInitialRotate());
        }
    }
    const float worldScale = models[0].scale;
    const float invW = 1.0f / worldScale;
    const uint32_t nf = models[0].pNf;
    const int32_t first = models[0].first;
    for (auto& m : models)
    {
        if (m.pNf != nf || m.first != first) { std::fprintf(stderr, "ERROR: pose files have different frame ranges\n"); return 1; }
        if (std::abs(m.scale - worldScale) > worldScale * 0.02f)
            std::printf("WARNING: model scales differ (%.5f vs %.5f); collisions between them will be off\n", m.scale, worldScale);
    }

    // ---- shared world: everything goes into model 0's physics ----
    saba::MMDPhysics* phys = models[0].model->GetMMDPhysics();
    phys->SetFPS(fps);
    phys->SetMaxSubStepCount(substeps);
    btDiscreteDynamicsWorld* world = phys->GetDynamicsWorld();
    size_t rbCount = 0, jointCount = 0;
    for (size_t mi = 0; mi < models.size(); mi++)
    {
        auto* pm = models[mi].model->GetPhysicsManager();
        saba::MMDPhysics* own = models[mi].model->GetMMDPhysics();
        if (mi > 0)
        {
            for (auto& j : *pm->GetJoints()) own->RemoveJoint(j.get());
            for (auto& rb : *pm->GetRigidBodys()) own->RemoveRigidBody(rb.get());
            for (auto& rb : *pm->GetRigidBodys()) phys->AddRigidBody(rb.get());
            for (auto& j : *pm->GetJoints()) phys->AddJoint(j.get());
        }
        for (auto& rb : *pm->GetRigidBodys()) rb->GetRigidBody()->setUserIndex2((int)mi);
        rbCount += pm->GetRigidBodys()->size();
        jointCount += pm->GetJoints()->size();
    }
    SceneFilter filter; filter.interModel = interModel;
    world->getPairCache()->setOverlapFilterCallback(&filter);
    if (!ground)
    {
        auto& objs = world->getCollisionObjectArray();
        for (int i = objs.size() - 1; i >= 0; i--)
            if (objs[i]->getCollisionShape() && objs[i]->getCollisionShape()->getShapeType() == STATIC_PLANE_PROXYTYPE)
            {
                if (auto* b = btRigidBody::upcast(objs[i])) world->removeRigidBody(b);
                else world->removeCollisionObject(objs[i]);
            }
    }

    // ---- colliders ----
    std::vector<Collider> colliders;
    FILE* cfp = nullptr; uint32_t cN = 0, cNf = 0; int32_t cFirst = 0;
    std::vector<float> cFrame, cFirstFrame;
    if (!collPath.empty())
    {
        if (!LoadColliders(collPath, colliders, cfp, cN, cNf, cFirst, invW)) { std::fprintf(stderr, "ERROR: bad collider file: %s\n", collPath.c_str()); return 1; }
        if (cNf != nf || cFirst != first) { std::fprintf(stderr, "ERROR: collider frames do not match pose frames\n"); return 1; }
        for (auto& c : colliders) world->addRigidBody(c.body.get(), (short)0x7FFF, (short)0x7FFF);
        cFrame.resize(cN * 7); cFirstFrame.resize(cN * 7);
    }
    if (!fieldsPath.empty())
    {
        if (!fields::Load(fieldsPath, worldScale)) { std::fprintf(stderr, "ERROR: bad force field file: %s\n", fieldsPath.c_str()); return 1; }
        fields::Install(world);
        std::printf("force fields: %u\n", fields::Count());
    }
    std::printf("scene: %zu models, %zu rigid bodies, %zu joints, %u colliders, inter-model %s, ground %s, scale %.5f\n",
        models.size(), rbCount, jointCount, cN, interModel ? "on" : "off", ground ? "on" : "off", worldScale);
    std::printf("frames: %d..%d (%u)\n", first, first + (int)nf - 1, nf);
    std::fflush(stdout);

    for (auto& m : models)
    {
        m.out = OpenW(m.outPath, L"wb");
        if (!m.out) { std::fprintf(stderr, "ERROR: cannot open output: %s\n", m.outPath.c_str()); return 1; }
        WriteHeader(m, (int)nf, first);
        if (!ReadPoseFrame(m, m.firstFrame)) { std::fprintf(stderr, "ERROR: pose file truncated\n"); return 1; }
    }
    if (cfp && streamio::Read(cFirstFrame.data(), sizeof(float), cFirstFrame.size(), cfp) != cFirstFrame.size()) { std::fprintf(stderr, "ERROR: collider file truncated\n"); return 1; }

    double tPhys = 0, tPose = 0;
    auto setColliders = [&](const std::vector<float>& fr) { for (uint32_t i = 0; i < cN; i++) SetColliderTransform(colliders[i], &fr[i * 7], invW); };
    auto syncBack = [&]() {
        for (auto& m : models)
        {
            auto* rbs = m.model->GetPhysicsManager()->GetRigidBodys();
            for (auto& rb : *rbs) rb->ReflectGlobalTransform();
            for (auto& rb : *rbs) rb->CalcLocalTransform();
            for (size_t i = 0; i < m.nb; i++) { auto* nd = m.nm->GetMMDNode(i); if (!nd->GetParent()) nd->UpdateGlobalTransform(); }
        }
    };
    auto step = [&](bool useFirst, float weight) {
        auto a = clk::now();
        for (auto& m : models) SetPose(m, useFirst ? m.firstFrame : m.cur, weight, invW);
        if (cN) setColliders(useFirst ? cFirstFrame : cFrame);
        auto b = clk::now(); tPose += std::chrono::duration<double>(b - a).count();
        for (auto& m : models) for (auto& rb : *m.model->GetPhysicsManager()->GetRigidBodys()) rb->SetActivation(true);
        phys->Update(dt);
        syncBack();
        tPhys += since(b);
    };

    // reset: put every body at the rest pose placed in the scene (first frame's armature transforms)
    for (auto& m : models) SetPose(m, m.firstFrame, 0.0f, invW);
    if (cN) setColliders(cFirstFrame);
    for (auto& m : models) for (auto& rb : *m.model->GetPhysicsManager()->GetRigidBodys())
    {
        rb->SetActivation(false); rb->ResetTransform(); rb->Reset(phys);
    }
    phys->Update(1.0f / 60.0f);
    syncBack();
    double tLoad = since(t0);

    const int warmTotal = warmup + settle;
    for (int i = 0; i < warmTotal; i++)
    {
        step(true, i < warmup ? float(1 + i) / float(warmup) : 1.0f);
        std::printf("WARMUP %d/%d\n", i + 1, warmTotal); std::fflush(stdout);
    }

    int lastPct = -1;
    for (uint32_t f = 0; f < nf; f++)
    {
        for (auto& m : models)
        {
            if (f == 0) m.cur = m.firstFrame;
            else if (!ReadPoseFrame(m, m.cur)) { std::fprintf(stderr, "ERROR: pose file truncated at frame %u\n", f); return 1; }
        }
        if (cN)
        {
            if (f == 0) cFrame = cFirstFrame;
            else if (streamio::Read(cFrame.data(), sizeof(float), cFrame.size(), cfp) != cFrame.size()) { std::fprintf(stderr, "ERROR: collider file truncated\n"); return 1; }
        }
        if (!fields::SetFrame(f)) { std::fprintf(stderr, "ERROR: force field file truncated\n"); return 1; }
        step(false, 1.0f);
        for (auto& m : models) WriteFrame(m);
        int pct = (int)((f + 1) * 200LL / nf);
        if (pct != lastPct) { lastPct = pct; std::printf("PROGRESS %u/%u\n", f + 1, nf); std::fflush(stdout); }
    }
    for (auto& m : models) { std::fclose(m.out); std::fclose(m.pin); }
    if (cfp) std::fclose(cfp);
    std::printf("TIMING load=%.2f pose=%.2f physics=%.2f\n", tLoad, tPose, tPhys);
    std::printf("DONE %u frames in %.1f s (%zu models)\n", nf, since(t0), models.size());
    // Rigid bodies were moved between worlds: skip all destructors (output is complete on disk).
    std::fflush(stdout); std::fflush(stderr);
    std::_Exit(0);
}
