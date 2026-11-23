// Reading input files that Blender is still writing (pose / collider streams).
#pragma once
#include <cstddef>
#include <cstdio>

namespace streamio
{
    // Turn on waiting reads; `parentPid` is the writer (Blender): stop waiting if it goes away.
    void Enable(unsigned long parentPid);

    // fread that, in streaming mode, waits for data instead of stopping at the current end of file.
    std::size_t Read(void* dst, std::size_t size, std::size_t count, std::FILE* f);
}
