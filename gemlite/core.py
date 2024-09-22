# Written by Dr. Hicham Badri @Mobius Labs GmbH - 2024
# ********************************************************
import torch
import numpy as np
from enum import Enum

# CUDA extension
import gemlite_lib

# Triton
import triton.language as tl
from triton.testing import do_bench
from .triton_kernels import *

class DType(Enum):
    FP16 = "FP16"
    BF16 = "BF16"
    FP32 = "FP32"
    INT8 = "INT8"
    INT32 = "INT32"
    FP16D8 = "FP16D8i"  # dynamic quantization

###################################################################################################################################
# CUDA backend
###################################################################################################################################
GEMLITE_GEMV_FP16_INPUT_FP16_OUTPUT = {
    8: gemlite_lib.gemv_A16fW8iO16f,  # (x, W, w_shift, w_scale)
    4: gemlite_lib.gemv_A16fW4iO16f,
    2: gemlite_lib.gemv_A16fW2iO16f,
}

GEMLITE_GEMV_INT8_INPUT_INT32_OUTPUT = {
    8: gemlite_lib.gemv_A8iW8iO32i,  # (x, W, w_shift)
    4: gemlite_lib.gemv_A8iW4iO32i,
    2: gemlite_lib.gemv_A8iW2iO32i,
}

class GemLiteLinearCUDA(torch.nn.Module):
    warp_size = 32
    warps_per_block = 32
    cols_per_warp = 1
    threads_per_group = warp_size // cols_per_warp

    # Input weights W_uint should be uint8 [0, ...]
    def __init__(
        self,
        W_nbits,
        group_size,
        in_features,
        out_features,
        input_dtype=DType.FP16,
        output_dtype=DType.FP16,
        acc_dtype=None,
    ):
        super().__init__()

        self.in_features = in_features
        self.out_features = out_features
        self.orig_shape = (out_features, in_features)
        self.W_nbits = W_nbits
        self.group_size = group_size if group_size is not None else in_features
        self.input_dtype = input_dtype
        self.output_dtype = output_dtype
        self.signature = (in_features, out_features, W_nbits, group_size)
        self.compute_dtype = torch.float16
        self.forward_raw = None

        if input_dtype == DType.FP16 and output_dtype == DType.FP16:
            self.kernel_fct = GEMLITE_GEMV_FP16_INPUT_FP16_OUTPUT[self.W_nbits]
            if self.in_features * self.out_features % group_size == 0:
                self.zero_is_tensor = True
                self.has_scales = True
                self.forward_raw = lambda x, W_q, zeros, scales: self.kernel_fct(x, W_q, zeros, scales)

        if input_dtype == DType.INT8 and output_dtype == DType.INT32:
            self.kernel_fct = GEMLITE_GEMV_INT8_INPUT_INT32_OUTPUT[self.W_nbits]
            if group_size == self.in_features * self.out_features:
                self.zero_is_tensor = False
                self.has_scales = False
                self.forward_raw = lambda x, W_q, zeros, scales: self.kernel_fct(x, W_q, zeros)

        if self.forward_raw is None:
            raise NotImplementedError(
                "Unsupport configuration: ",
                (
                    ("input_dtype", self.input_dtype),
                    ("output_dtype", self.output_dtype),
                    ("W_nbits", self.W_nbits),
                    ("group_size", self.group_size),
                ),
            )

        self.acc_dtype = None

    # Universal bitpacking with int32
    def pack(self, W_q, scales, zeros, bias=None):
        tile_size = self.threads_per_group

        step = 32 // self.W_nbits
        pad = int(step * np.ceil(W_q.shape[1] / step) - W_q.shape[1])
        # pad  += int(tile_size*np.ceil(W_q.shape[1]/tile_size) - W_q.shape[1])
        if pad > 0:
            W_q = torch.nn.functional.pad(W_q, pad=(0, pad), value=0)

        W_shape = W_q.shape
        W_q     = W_q.to(torch.int32).reshape(-1, tile_size)

        i, shift = 0, 32
        shift -= self.W_nbits
        W_q_packed = W_q[i::step, :] << shift
        for i in range(1, step):
            shift -= self.W_nbits
            W_q_packed |= W_q[i::step, :] << shift

        self.W_q = W_q_packed.reshape(W_shape[0], W_shape[1] // step)
        assert self.has_scales == (scales is not None), "existence of scales did not match expectation"

        if scales is not None:
            if self.zero_is_tensor:
                self.scales = scales if isinstance(scales, torch.Tensor) else torch.tensor([[scales]], dtype=torch.float16, device=W_q.device)
            else:
                assert not isinstance(scales, torch.Tensor)
                self.scales = scales
        else:
            self.scales = None # supports the int kernel

        if self.zero_is_tensor:
            self.zeros = zeros if isinstance(zeros, torch.Tensor) else torch.tensor([[zeros]], dtype=torch.float16, device=W_q.device)
        else:
            assert not isinstance(zeros, torch.Tensor)
            self.zeros = zeros

        self.bias = None if (bias is None) else torch.nn.Parameter(bias.to(device=self.W_q.device, dtype=self.compute_dtype))
        self.device = self.W_q.device

        return self

    def unpack(self, W_q_packed, dtype=torch.uint8):
        tile_size  = self.threads_per_group
        step       = 32 // self.W_nbits
        W_shape    = [self.W_q_packed.shape[0], self.W_q_packed.shape[1] * step]
        W_q_packed = self.W_q_packed.view((-1, tile_size))
        W_r        = torch.empty([step * self.W_q_packed.numel() // tile_size, tile_size], dtype=dtype, device=self.W_q_packed.device)
        mask       = 2**self.W_nbits - 1

        shift = 32
        for i in range(0, step):
            shift -= self.W_nbits
            W_r[i::step, :] = (W_q_packed >> shift) & mask

        return W_r.view(W_shape)

    # Main forward pass
    def forward(self, x):
        out_shape = x.shape[:-1] + (self.out_features,)
        out = self.forward_raw(x.view(-1, x.shape[-1]), self.W_q, self.zeros, self.scales).view(out_shape)

        if self.bias is not None:
            out += self.bias
        return out


###################################################################################################################################
# Triton backend
###################################################################################################################################
def eval_time(fct, params, warmup=25, rep=200, fast_flush=True, return_mode="min"):
    if isinstance(params, dict):
        return do_bench(
            lambda: fct(**params),
            warmup=warmup,
            rep=rep,
            fast_flush=fast_flush,
            return_mode=return_mode,
        )
    if isinstance(params, list):
        return do_bench(
            lambda: fct(*params),
            warmup=warmup,
            rep=rep,
            fast_flush=fast_flush,
            return_mode=return_mode,
        )


GEMLITE_TRITON_CACHE= {}

GEMLITE_TRITON_MAPPING = {
    ("FP16", "GEMV"): gemv_A16fWnO16f_int32packing,
    ("FP16", "GEMM"): gemm_A16fWnO16f_int32packing,
    ("BF16", "GEMM"): gemm_A16fWnO16f_int32packing,
}

def cdiv(a, b):
    return (a + b - 1) // b

# Triton
class GemLiteLinearTriton(torch.nn.Module):
    def __init__(
        self,
        W_nbits,
        group_size,
        in_features,
        out_features,
        input_dtype=DType.FP16,
        output_dtype=DType.FP16,
        acc_dtype=None,
    ):
        self._SUPPORTED_BITS_TRITON = [1, 2, 4, 8]

        super().__init__()
        if W_nbits not in self._SUPPORTED_BITS_TRITON:
            raise NotImplementedError("Only 2,4,8 W_nbits are supported.")
        if in_features % 128 != 0 or out_features % 128 != 0:
            raise NotImplementedError("Invalid input shapes")

        self.in_features  = in_features
        self.out_features = out_features
        self.orig_shape   = (out_features, in_features)
        self.W_nbits      = W_nbits
        self.group_size   = group_size if group_size is not None else in_features
        self.unpack_mask  = 2**self.W_nbits - 1
        self.elements_per_sample = 32 // self.W_nbits
        self.signature = (in_features, out_features, W_nbits, group_size)

        self.input_dtype  = input_dtype
        self.output_dtype = output_dtype

        self.compute_dtype = None
        if input_dtype == DType.FP16 and output_dtype == DType.FP16:
            self.kernels = [gemm_A16fWnO16f_int32packing, gemv_A16fWnO16f_int32packing]
            self.compute_dtype = torch.float16
        if input_dtype == DType.BF16 and output_dtype == DType.BF16:
            self.kernels = [gemm_A16fWnO16f_int32packing]
            self.compute_dtype = torch.bfloat16

        if self.compute_dtype is None:
            raise NotImplementedError(
                "Unsupport settings: ",
                (self.input_dtype, self.output_dtype, self.W_nbits),
            )

        if acc_dtype is None:
            acc_dtype = DType.FP16 if (self.compute_dtype == torch.float16) else DType.FP32

        self.acc_dtype = tl.float16 if (acc_dtype == DType.FP16) else tl.float32

        num_groups = cdiv(self.in_features, self.group_size)

        with torch.device("meta"):
            self.register_buffer(
                "W_q",
                torch.zeros(
                    (self.in_features // 32 * self.W_nbits, self.out_features),
                    dtype=torch.int32,
                ),
            )
            self.register_buffer(
                "scales",
                torch.zeros(
                    (num_groups, self.out_features),
                    dtype=self.compute_dtype,
                ),
            )
            self.register_buffer(
                "zeros",
                torch.zeros(
                    (num_groups, self.out_features),
                    dtype=self.compute_dtype,
                ),
            )

        self.forward = self.forward_auto

    # Pack data: following the same logic as: https://github.com/LeiWang1999/AutoGPTQ.tvm/blob/dcd135b9784b9f98235fc91467fe3c3c8afa34fc/auto_gptq/nn_modules/qlinear_triton.py#L413-L419
    def pack(self, W_q, scales, zeros, bias=None):
        W_q      = W_q.reshape(self.orig_shape).t().contiguous().to(torch.int32)
        self.W_q = torch.zeros((W_q.shape[0] // 32 * self.W_nbits, W_q.shape[1]), dtype=torch.int32, device=W_q.device)

        step = 32 // self.W_nbits
        i, row = 0, 0
        while row < self.W_q.shape[0]:
            shift = 0
            for j in range(i, i + step):
                self.W_q[row] |= W_q[j] << shift
                shift += self.W_nbits
            i += step
            row += 1

        self.W_q    = self.W_q.contiguous()
        num_groups  = self.in_features // self.group_size
        self.scales = scales.reshape((self.out_features, num_groups)).t().contiguous()
        self.zeros  = zeros.reshape((self.out_features, num_groups)).t().contiguous()
        self.bias   = None if (bias is None) else torch.nn.Parameter(bias.to(device=self.W_q.device, dtype=self.compute_dtype))
        self.device = self.W_q.device
        return self

    # Warm up all the selected kernels
    def warmup(self, signature, args):
        global GEMLITE_TRITON_CACHE
        t = []
        for _kernel in self.kernels:
            if signature[0] > 8 and _kernel.matmul_type == "GEMV":
                continue  # skip gemvs for larger batch-sizes
            t.append(eval_time(_kernel.forward, args))
        indx = np.argmin(t)
        GEMLITE_TRITON_CACHE[signature] = {
            "forward": self.kernels[indx].forward,
            "time": t[indx],
        }

    # Main forward pass
    def forward_auto(self, x):
        global GEMLITE_TRITON_CACHE
        out_shape = x.shape[:-1] + (self.out_features,)
        args = [
            x.view(-1, x.shape[-1]),
            self.W_q,
            self.scales,
            self.zeros,
            self.W_nbits,
            self.group_size,
            self.unpack_mask,
            self.elements_per_sample,
            self.acc_dtype,
        ]

        _signature = (x.shape[0],) + self.signature
        if _signature not in GEMLITE_TRITON_CACHE:
            self.warmup(_signature, args)

        out = GEMLITE_TRITON_CACHE[_signature]["forward"](*args).view(out_shape)

        if self.bias is not None:
            out += self.bias
        return out

    def forward_manual(self, x, matmul_type="GEMM"):
        out_shape = x.shape[:-1] + (self.out_features,)

        out = (
            GEMLITE_TRITON_MAPPING[(self.input_dtype.value, matmul_type)]
            .forward(
                x.view(-1, x.shape[-1]),
                self.W_q,
                self.scales,
                self.zeros,
                self.W_nbits,
                self.group_size,
                self.unpack_mask,
                self.elements_per_sample,
                self.acc_dtype,
            )
            .view(out_shape)
        )

        if self.bias is not None:
            out += self.bias
        return out

###################################################################################################################################
###################################################################################################################################
GemLiteLinear = GemLiteLinearTriton  # Triton by default
