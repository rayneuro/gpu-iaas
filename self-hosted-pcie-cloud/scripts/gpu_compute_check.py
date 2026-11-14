"""Run a CUDA kernel through the driver API; no CUDA toolkit required."""
import ctypes as c
import json

PTX = b""".version 6.0
.target sm_52
.address_size 64
.visible .entry fill(.param .u64 output) {
 .reg .b64 %ptr, %offset, %base;
 .reg .b32 %i, %value;
 ld.param.u64 %base, [output];
 mov.u32 %i, %tid.x;
 mul.wide.u32 %offset, %i, 4;
 add.u64 %ptr, %base, %offset;
 mul.lo.u32 %value, %i, 7;
 add.u32 %value, %value, 3;
 st.global.u32 [%ptr], %value;
 ret;
}
"""

def main():
    cuda = c.CDLL("libcuda.so.1")
    def call(name, types, *args):
        fn = getattr(cuda, name)
        fn.argtypes, fn.restype = types, c.c_int
        result = fn(*args)
        if result:
            raise RuntimeError(f"{name}: CUDA error {result}")
    ptr, integer = c.c_void_p, c.c_int
    context, module, kernel = ptr(), ptr(), ptr()
    memory, device = c.c_uint64(), integer()
    call("cuInit", [c.c_uint], 0)
    call("cuDeviceGet", [c.POINTER(integer), integer], c.byref(device), 0)
    call("cuCtxCreate_v2", [c.POINTER(ptr), c.c_uint, integer], c.byref(context), 0, device)
    try:
        call("cuModuleLoadData", [c.POINTER(ptr), ptr], c.byref(module), c.cast(c.c_char_p(PTX), ptr))
        call("cuModuleGetFunction", [c.POINTER(ptr), ptr, c.c_char_p], c.byref(kernel), module, b"fill")
        call("cuMemAlloc_v2", [c.POINTER(c.c_uint64), c.c_size_t], c.byref(memory), 1024)
        try:
            parameters = (ptr * 1)(c.cast(c.byref(memory), ptr))
            call("cuLaunchKernel", [ptr] + [c.c_uint]*7 + [ptr, c.POINTER(ptr), c.POINTER(ptr)],
                 kernel, 1, 1, 1, 256, 1, 1, 0, None, parameters, None)
            call("cuCtxSynchronize", [])
            result = (c.c_uint32 * 256)()
            call("cuMemcpyDtoH_v2", [ptr, c.c_uint64, c.c_size_t], result, memory, 1024)
            if list(result) != [i*7+3 for i in range(256)]:
                raise RuntimeError("CUDA result mismatch")
            print(json.dumps({"cuda_compute": "PASS", "elements": 256, "formula": "i*7+3"}))
        finally:
            call("cuMemFree_v2", [c.c_uint64], memory)
    finally:
        call("cuCtxDestroy_v2", [ptr], context)

if __name__ == "__main__":
    main()
