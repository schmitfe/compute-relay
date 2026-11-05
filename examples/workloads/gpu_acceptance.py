"""Reviewed one-GPU probe: run a one-thread CUDA kernel and verify four bytes.

Requires the NVIDIA driver and nvidia-smi, no Python packages or toolkit install.
Run only inside an approved single-GPU Slurm allocation; declare result.json.
"""

import ctypes as c
import json
import os
from pathlib import Path
import re
import subprocess
import uuid


def main():
    assigned = os.environ.get("SLURM_JOB_GPUS", "")
    assert os.environ.get("SLURM_JOB_ID") and re.fullmatch(r"[0-9]+|GPU-[0-9a-fA-F-]{36}", assigned), "Expected one assigned Slurm GPU"
    result = subprocess.run(["nvidia-smi", "--id=" + assigned, "--query-gpu=uuid,name", "--format=csv,noheader"],
        capture_output=True, text=True, check=True, timeout=10)
    gpu_uuid, name = result.stdout.strip().split(",", 1)
    identifier = (c.c_ubyte * 16)(*uuid.UUID(gpu_uuid.removeprefix("GPU-")).bytes)
    cuda = c.CDLL("libcuda.so.1")

    def call(name, types, *arguments):
        function = getattr(cuda, name)
        function.argtypes, function.restype = types, c.c_int
        code = function(*arguments)
        assert code == 0, f"{name} failed with CUDA status {code}"

    device, context, module, kernel, pointer = c.c_int(), c.c_void_p(), c.c_void_p(), c.c_void_p(), c.c_uint64()
    call("cuInit", [c.c_uint], 0)
    count = c.c_int()
    call("cuDeviceGetCount", [c.POINTER(c.c_int)], c.byref(count))
    for ordinal in range(count.value):
        observed = (c.c_ubyte * 16)()
        call("cuDeviceGet", [c.POINTER(c.c_int), c.c_int], c.byref(device), ordinal)
        call("cuDeviceGetUuid", [c.c_void_p, c.c_int], c.byref(observed), device)
        if bytes(observed) == bytes(identifier):
            break
    else:
        raise RuntimeError("The allocated GPU UUID is not visible to CUDA")
    call("cuCtxCreate_v2", [c.POINTER(c.c_void_p), c.c_uint, c.c_int], c.byref(context), 0, device)
    try:
        ptx = b""".version 6.4
.target sm_75
.address_size 64
.visible .entry probe(.param .u64 output) {
    .reg .u64 %address;
    ld.param.u64 %address, [output];
    st.global.u32 [%address], 123;
    ret;
}
"""
        call("cuModuleLoadData", [c.POINTER(c.c_void_p), c.c_char_p], c.byref(module), ptx)
        call("cuModuleGetFunction", [c.POINTER(c.c_void_p), c.c_void_p, c.c_char_p], c.byref(kernel), module, b"probe")
        call("cuMemAlloc_v2", [c.POINTER(c.c_uint64), c.c_size_t], c.byref(pointer), 4)
        parameters = (c.c_void_p * 1)(c.cast(c.byref(pointer), c.c_void_p))
        call("cuLaunchKernel", [c.c_void_p, *([c.c_uint] * 7), c.c_void_p, c.c_void_p, c.c_void_p],
            kernel, 1, 1, 1, 1, 1, 1, 0, None, parameters, None)
        call("cuCtxSynchronize", [])
        output = c.c_uint32()
        call("cuMemcpyDtoH_v2", [c.c_void_p, c.c_uint64, c.c_size_t], c.byref(output), pointer, 4)
        assert output.value == 123, "GPU kernel result differs"
    finally:
        if pointer.value:
            call("cuMemFree_v2", [c.c_uint64], pointer)
        call("cuCtxDestroy_v2", [c.c_void_p], context)
    Path("result.json").write_text(json.dumps({"ok": True, "slurm_job_id": os.environ["SLURM_JOB_ID"],
        "gpu_uuid": gpu_uuid, "gpu_name": name.strip(), "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "kernel_result": output.value}) + "\n")
    print("COMPUTE_RELAY_GPU_ACCEPTANCE_OK")


if __name__ == "__main__":
    main()
