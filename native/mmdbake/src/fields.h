#pragma once
// Blender force fields (wind, force, vortex, turbulence) acting on the dynamic rigid bodies.
//
// .mbf (written by the Blender add-on, little endian, Blender units):
//   char[8] "MMDFILD1"; u32 version(=1); u32 count; u32 frames; i32 firstFrame
//   per field: u8 type (0 wind, 1 force, 2 vortex, 3 turbulence); u8 shape (0 point, 1 plane);
//              u8 falloff (0 sphere, 1 tube, 2 cone); i8 zDirection (0 both, 1 +Z, -1 -Z);
//              u8 flags (1 use min distance, 2 use max distance, 4 use min radial, 8 use max radial,
//                        16 turbulence in global coordinates);
//              f32 power, minDist, maxDist, radialPower, minRadial, maxRadial, noiseSize, flow
//   per frame, per field: f32[8] = qw,qx,qy,qz,tx,ty,tz (field transform without scale), strength
//   frames == 1: the fields do not change over time.
//   Coordinates: Blender world (scene mode) or the armature's object space (single mode).
// strength is in Blender newtons with the rigid body world's effector weights already applied;
// the PMX rigid body mass is used as kilograms.

#include <string>

class btDiscreteDynamicsWorld;

namespace fields
{
    // scale: Blender units per Bullet (MMD) unit.
    bool Load(const std::string& path, float scale);
    bool Active();
    // frame index relative to the file's first frame; frames are read in order (streamed files)
    bool SetFrame(unsigned frame);
    void Install(btDiscreteDynamicsWorld* world);
    unsigned Count();
}
