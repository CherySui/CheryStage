#include "stream_io.h"

#define NOMINMAX
#include <windows.h>

namespace streamio
{
    static bool g_on = false;
    static HANDLE g_parent = nullptr;

    void Enable(unsigned long parentPid)
    {
        g_parent = parentPid ? OpenProcess(SYNCHRONIZE, FALSE, (DWORD)parentPid) : nullptr;
        g_on = g_parent != nullptr;   // writer already gone (or unknown): plain reads, never wait
    }

    std::size_t Read(void* dst, std::size_t size, std::size_t count, std::FILE* f)
    {
        if (!g_on) return std::fread(dst, size, count, f);
        const std::size_t want = size * count;
        std::size_t got = 0;
        char* p = static_cast<char*>(dst);
        while (got < want)
        {
            got += std::fread(p + got, 1, want - got, f);
            if (got == want || std::ferror(f)) break;
            std::clearerr(f);   // at the current end of file: wait for the writer
            if (g_parent && WaitForSingleObject(g_parent, 0) == WAIT_OBJECT_0) break;
            Sleep(1);
        }
        return size ? got / size : 0;
    }
}
