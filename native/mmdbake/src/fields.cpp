// Blender force fields applied to the dynamic rigid bodies, once per physics sub step.
// Formulas follow Blender's effector code (effect.cc: falloff, wind / force / vortex / turbulence).

#include "fields.h"
#include "stream_io.h"

#include <Saba/Base/UnicodeUtil.h>

#include <btBulletDynamicsCommon.h>

#include <glm/glm.hpp>
#include <glm/gtc/quaternion.hpp>

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>

namespace fields
{
namespace
{
    struct Field
    {
        uint8_t type = 0, shape = 0, falloff = 0, flags = 0;
        int8_t zdir = 0;
        float power = 0, minDist = 0, maxDist = 0, powerR = 0, minRad = 0, maxRad = 0, size = 1, flow = 0;
        // current frame
        glm::vec3 loc{0}, nor{0, 0, 1};
        glm::mat3 rotInv{1};
        float strength = 0;
    };

    std::vector<Field> g_fields;
    std::vector<float> g_frame;
    FILE* g_fp = nullptr;
    uint32_t g_nf = 0;
    unsigned g_read = 0;     // frames read so far
    float g_scale = 0.08f;   // Blender units per Bullet unit

    // Bullet (MMD) space = Blender with Y and Z swapped
    inline glm::vec3 Swap(const btVector3& v) { return glm::vec3(v.x(), v.z(), v.y()); }
    inline btVector3 Swap(const glm::vec3& v) { return btVector3(v.x, v.z, v.y); }

    // ---- noise (improved Perlin), for turbulence ----
    const int kPerm[256] = {
        151,160,137,91,90,15,131,13,201,95,96,53,194,233,7,225,140,36,103,30,69,142,8,99,37,240,21,10,23,
        190,6,148,247,120,234,75,0,26,197,62,94,252,219,203,117,35,11,32,57,177,33,88,237,149,56,87,174,20,
        125,136,171,168,68,175,74,165,71,134,139,48,27,166,77,146,158,231,83,111,229,122,60,211,133,230,220,
        105,92,41,55,46,245,40,244,102,143,54,65,25,63,161,1,216,80,73,209,76,132,187,208,89,18,169,200,196,
        135,130,116,188,159,86,164,100,109,198,173,186,3,64,52,217,226,250,124,123,5,202,38,147,118,126,255,
        82,85,212,207,206,59,227,47,16,58,17,182,189,28,42,223,183,170,213,119,248,152,2,44,154,163,70,221,
        153,101,155,167,43,172,9,129,22,39,253,19,98,108,110,79,113,224,232,178,185,112,104,218,246,97,228,
        251,34,242,193,238,210,144,12,191,179,162,241,81,51,145,235,249,14,239,107,49,192,214,31,181,199,106,
        157,184,84,204,176,115,121,50,45,127,4,150,254,138,236,205,93,222,114,67,29,24,72,243,141,128,195,78,
        66,215,61,156,180 };
    inline int Pm(int i) { return kPerm[i & 255]; }
    inline float Fade(float t) { return t * t * t * (t * (t * 6 - 15) + 10); }
    inline float Grad(int h, float x, float y, float z)
    {
        h &= 15;
        float u = h < 8 ? x : y, v = h < 4 ? y : (h == 12 || h == 14) ? x : z;
        return ((h & 1) ? -u : u) + ((h & 2) ? -v : v);
    }
    float Perlin(float x, float y, float z)
    {
        int X = (int)std::floor(x), Y = (int)std::floor(y), Z = (int)std::floor(z);
        x -= std::floor(x); y -= std::floor(y); z -= std::floor(z);
        float u = Fade(x), v = Fade(y), w = Fade(z);
        int A = Pm(X) + Y, AA = Pm(A) + Z, AB = Pm(A + 1) + Z, B = Pm(X + 1) + Y, BA = Pm(B) + Z, BB = Pm(B + 1) + Z;
        auto L = [](float t, float a, float b) { return a + t * (b - a); };
        return L(w, L(v, L(u, Grad(Pm(AA), x, y, z), Grad(Pm(BA), x - 1, y, z)),
                         L(u, Grad(Pm(AB), x, y - 1, z), Grad(Pm(BB), x - 1, y - 1, z))),
                    L(v, L(u, Grad(Pm(AA + 1), x, y, z - 1), Grad(Pm(BA + 1), x - 1, y, z - 1)),
                         L(u, Grad(Pm(AB + 1), x, y - 1, z - 1), Grad(Pm(BB + 1), x - 1, y - 1, z - 1))));
    }
    // like BLI_noise_generic_turbulence(size, x, y, z, 2 octaves, soft): about 0..1
    float Turbulence(float size, float x, float y, float z)
    {
        if (size != 0) { x /= size; y /= size; z /= size; }
        float sum = 0, amp = 1, f = 1;
        for (int i = 0; i <= 2; i++, amp *= 0.5f, f *= 2.0f) sum += (Perlin(f * x, f * y, f * z) * 0.5f + 0.5f) * amp;
        return sum * (4.0f / 7.0f);
    }

    // Blender falloff_func
    float FalloffFunc(float fac, bool useMin, float minV, bool useMax, float maxV, float power)
    {
        if (useMax && fac > maxV) return 0.0f;
        if (useMin && fac < minV) return 1.0f;
        if (!useMin) minV = 0.0f;
        return std::pow(1.0f + fac - minV, -power);
    }

    glm::vec3 Force(const Field& f, const glm::vec3& p)
    {
        glm::vec3 d = p - f.loc;
        glm::vec3 vec = f.shape == 1 ? f.nor * glm::dot(d, f.nor) : d;   // plane: distance along the normal
        float dist = glm::length(vec);

        // falloff (effector_falloff)
        float fac = glm::dot(f.nor, vec);
        if ((f.zdir > 0 && fac < 0) || (f.zdir < 0 && fac > 0)) return glm::vec3(0);
        float falloff = 1.0f;
        if (f.falloff == 0)
            falloff = FalloffFunc(dist, f.flags & 1, f.minDist, f.flags & 2, f.maxDist, f.power);
        else
        {
            falloff = FalloffFunc(std::fabs(fac), f.flags & 1, f.minDist, f.flags & 2, f.maxDist, f.power);
            if (falloff == 0.0f) return glm::vec3(0);
            float r;
            if (f.falloff == 1) r = glm::length(d - f.nor * glm::dot(d, f.nor));   // tube: distance from the axis
            else
            {
                float len = glm::length(d);
                r = len > 0 ? glm::degrees(std::acos(glm::clamp(glm::dot(f.nor, d) / len, -1.0f, 1.0f))) : 0.0f;
            }
            falloff *= FalloffFunc(r, f.flags & 4, f.minRad, f.flags & 8, f.maxRad, f.powerR);
        }
        float s = f.strength * falloff;
        if (s == 0.0f) return glm::vec3(0);

        switch (f.type)
        {
        case 0:   // wind: along the field's Z axis
            return f.nor * s;
        case 1:   // force: away from the field (negative strength attracts)
            return dist > 1e-6f ? vec / dist * s : glm::vec3(0);
        case 2:   // vortex: around the field's Z axis, optional inflow
        {
            glm::vec3 c = glm::cross(f.nor, d);
            float cl = glm::length(c);
            glm::vec3 out = cl > 1e-6f ? c / cl * s : glm::vec3(0);
            if (f.flow != 0.0f)
            {
                glm::vec3 radial = d - f.nor * glm::dot(d, f.nor);
                float rl = glm::length(radial);
                if (rl > 1e-6f) out -= radial / rl * (s * f.flow);
            }
            return out;
        }
        default:  // turbulence
        {
            glm::vec3 q = (f.flags & 16) ? p : f.rotInv * d;
            return glm::vec3(-1 + 2 * Turbulence(f.size, q.x, q.y, q.z),
                             -1 + 2 * Turbulence(f.size, q.y, q.z, q.x),
                             -1 + 2 * Turbulence(f.size, q.z, q.x, q.y)) * s;
        }
        }
    }

    void PreTick(btDynamicsWorld* world, btScalar timeStep)
    {
        if (g_fields.empty()) return;
        auto& objs = world->getCollisionObjectArray();
        for (int i = 0; i < objs.size(); i++)
        {
            btRigidBody* b = btRigidBody::upcast(objs[i]);
            if (!b || b->isStaticOrKinematicObject() || b->getInvMass() <= 0) continue;
            glm::vec3 p = Swap(b->getCenterOfMassPosition()) * g_scale;   // Blender units
            glm::vec3 F(0);
            for (const Field& f : g_fields) F += Force(f, p);
            if (F == glm::vec3(0)) continue;
            // newtons in Blender units -> Bullet units (same mass, lengths / scale)
            b->applyCentralImpulse(Swap(F / g_scale) * timeStep);
        }
    }

    void ApplyFrame()
    {
        for (size_t i = 0; i < g_fields.size(); i++)
        {
            const float* v = &g_frame[i * 8];
            glm::mat3 R = glm::mat3_cast(glm::normalize(glm::quat(v[0], v[1], v[2], v[3])));
            g_fields[i].loc = glm::vec3(v[4], v[5], v[6]);
            g_fields[i].nor = R[2];
            g_fields[i].rotInv = glm::transpose(R);
            g_fields[i].strength = v[7];
        }
    }
}

bool Load(const std::string& path, float scale)
{
    std::wstring w; saba::TryToWString(path, w);
    g_fp = _wfopen(w.c_str(), L"rb");
    char magic[8]; uint32_t ver = 0, n = 0; int32_t first = 0;
    auto rd = [](void* d, size_t sz) { return streamio::Read(d, sz, 1, g_fp) == 1; };
    if (!g_fp || streamio::Read(magic, 1, 8, g_fp) != 8 || std::memcmp(magic, "MMDFILD1", 8) != 0
        || !rd(&ver, 4) || !rd(&n, 4) || !rd(&g_nf, 4) || !rd(&first, 4) || ver != 1 || g_nf == 0)
        return false;
    g_fields.resize(n);
    for (auto& f : g_fields)
    {
        float prm[8];
        if (!rd(&f.type, 1) || !rd(&f.shape, 1) || !rd(&f.falloff, 1) || !rd(&f.zdir, 1) || !rd(&f.flags, 1)
            || streamio::Read(prm, 4, 8, g_fp) != 8)
            return false;
        f.power = prm[0]; f.minDist = prm[1]; f.maxDist = prm[2]; f.powerR = prm[3];
        f.minRad = prm[4]; f.maxRad = prm[5]; f.size = prm[6]; f.flow = prm[7];
    }
    g_scale = scale;
    g_frame.resize(n * 8);
    g_read = 0;
    return SetFrame(0);
}

bool Active() { return !g_fields.empty(); }
unsigned Count() { return (unsigned)g_fields.size(); }

bool SetFrame(unsigned frame)
{
    if (!g_fp) return true;
    if (frame >= g_nf) frame = g_nf - 1;
    while (g_read <= frame)
    {
        if (streamio::Read(g_frame.data(), sizeof(float), g_frame.size(), g_fp) != g_frame.size()) return false;
        g_read++;
        ApplyFrame();
    }
    return true;
}

void Install(btDiscreteDynamicsWorld* world)
{
    if (Active()) world->setInternalTickCallback(PreTick, nullptr, true);
}
}
