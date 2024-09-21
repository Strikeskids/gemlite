#OMP_NUM_THREADS=16 CUDA_VISIBLE_DEVICES=0 ipython3 benchmark_triton.py #select the right number of threads based on your machine
#You can change the matmul_dtype: GEMM, GEMV or AUTO
#Note: bfloat16 only supported in GEMM mode with float32 accumulation
#################################################################################################################################
import torch
import numpy as np

device = 'cuda:0'
compute_dtype = torch.float16

#in_features, out_features = 4096, 4096
#in_features, out_features = 4096*2, 4096*2
#in_features, out_features = 4096*4, 4096*4 
in_features, out_features = 4096*8, 4096*8 

#W_nbits, group_size = 8, in_features 
W_nbits, group_size = 4, 128 
#W_nbits, group_size = 2, 128

matmul_type = "AUTO" #GEMM, GEMV, "AUTO"

#################################################################################################################################
from triton.testing import do_bench
def eval_time(fct, params): 
    return do_bench(lambda: fct(**params), warmup=25, rep=200, fast_flush=True, return_mode='min') 

def check_valid(x, W, quant_linear, tol=1e-3):
    y_ref = torch.matmul(x, W.T)
    y_q   = quant_linear(x)
    assert (y_ref - y_q).abs().mean() < tol

#################################################################################################################################
#TorchAO Int8 settings
torch._dynamo.config.capture_scalar_outputs = True
torch._inductor.config.coordinate_descent_tuning = True

@torch.compile()
def matmul_torch_A16W8SYM(x, W_q, scales, out_features):
    out_shape = x.shape[:-1] + (out_features,)
    out = ((x.view((-1, x.shape[-1])) @ W_q.T.to(x.dtype)) / scales.view(1, -1)).view(out_shape)
    return out

class Torch_A16W8SYM(torch.nn.Module):
    def __init__(self, in_features, out_features, W_q, scales, bias=None):
        super().__init__() 
        self.W_q           = W_q
        self.in_features   = in_features
        self.out_features  = out_features
        self.scales        = scales 
        self.bias          = bias 
        self.device        = W_q.device
        self.compute_dtype = scales.dtype
     
    def forward(self, x):
        out = matmul_torch_A16W8SYM(x.to(self.device), self.W_q, self.scales, self.out_features)
        if(self.bias is not None):
            out += self.bias
        return out

#HQQ
from hqq.core.quantize import *
from hqq.backends.bitblas import patch_hqq_to_bitblas, HQQLinearBitBlas
from hqq.backends.torchao import patch_hqq_to_aoint4

HQQLinearBitBlas.check = lambda hqq_layer: True
HQQLinearBitBlas.BIT_TO_DTYPE = {8:"uint8", 4: "uint4", 2: "uint2", 1: "uint1"}

#GemLite
from gemlite.core import GemLiteLinearTriton, DType

class empty_linear(torch.nn.Module):
    def __init__(self, in_features, out_features, compute_dtype, device):
        super().__init__()
        self.in_features   = in_features
        self.out_features  = out_features
        self.device        = device
        self.compute_dtype = compute_dtype

def gen_data(in_features, out_features, W_nbits, group_size, device=device):
    linear = torch.nn.Linear(in_features=in_features, out_features=out_features, bias=False, device='cpu')

    quant_config = BaseQuantizeConfig(nbits=W_nbits, group_size=group_size, quant_zero=False, quant_scale=False, axis=1)
    quant_config['weight_quant_params']['optimize'] = False

    hqq_layer    = HQQLinear(linear, quant_config=quant_config, compute_dtype=compute_dtype, device=device, del_orig=False) #bfloat16
    orig_shape   = (out_features, in_features)
    W            = hqq_layer.dequantize().reshape(orig_shape)

    gemlite_linear, torchao_linear, bitblas_linear, marlin_linear = [None]*4

    #GemLite
    if(W_nbits in [8, 4, 2, 1]):

        input_dtype, output_dtype, acc_dtype = None, None, None
        if(compute_dtype == torch.float16):
            input_dtype, output_dtype, acc_dtype = DType.FP16, DType.FP16, DType.FP16

        if(compute_dtype == torch.bfloat16):
            input_dtype, output_dtype, acc_dtype = DType.BF16, DType.BF16, DType.FP32 #FP16 acc not supported with bfloat16

        if(None in (input_dtype, output_dtype, acc_dtype)):
            raise Exception('Unsupported compute config', (input_dtype, output_dtype, acc_dtype))

        gemlite_linear = GemLiteLinearTriton(W_nbits=W_nbits, 
                                            group_size=group_size, in_features=in_features, out_features=out_features, 
                                            input_dtype=input_dtype, output_dtype=output_dtype, acc_dtype=acc_dtype)

        gemlite_linear.pack(hqq_layer.unpack().view(orig_shape), hqq_layer.meta['scale'], hqq_layer.meta['zero'], None);

    # #TorchAO
    # if(W_nbits==8):
    #     torchao_linear = Torch_A16W8SYM(in_features, out_features, (W_q.int() - 127).to(torch.int8), scales, bias=None)
    # if(W_nbits==4):
    #     hqq_layer.compute_dtype = torch.bfloat16
    #     hqq_layer.meta['scale'] = hqq_layer.meta['scale'].to(torch.bfloat16).view((-1, 1))
    #     hqq_layer.meta['zero']  = hqq_layer.meta['zero'].to(torch.bfloat16).view((-1, 1))
    #     torchao_linear          = patch_hqq_to_aoint4(hqq_layer, None)

    # # torch.cuda.empty_cache()

    # # Bitblas
    # if(W_nbits in [8, 4, 2]):
    #     bitblas_linear = patch_hqq_to_bitblas(HQQLinear(linear, quant_config=quant_config, compute_dtype=torch.float16, device=device, del_orig=False), None)

    # # torch.cuda.empty_cache()

    # #################################################################
    # #Marlin
    # from vllm.model_executor.layers.quantization.awq_marlin import AWQMarlinLinearMethod as MarlinLinearMethod
    # from vllm.model_executor.layers.quantization.awq_marlin import AWQMarlinConfig as MarlinConfig

    # if(W_nbits==4):
    #     _marlin_linear = MarlinLinearMethod(MarlinConfig(weight_bits=W_nbits, group_size=group_size, has_zp=True, lm_head_quantized=False))

    #     marlin_linear = empty_linear(in_features, out_features, compute_dtype=torch.float16, device='cuda:0')
    #     _marlin_linear.create_weights(layer=marlin_linear,
    #             input_size_per_partition=in_features,
    #             output_partition_sizes=[out_features],
    #             input_size=in_features,
    #             output_size=out_features,
    #             params_dtype=torch.float16)

    #     marlin_linear = marlin_linear.cuda()
    #     _marlin_linear.process_weights_after_loading(marlin_linear)

    #     marlin_linear.scales.data = torch.zeros_like(marlin_linear.scales.data) + 1
    #     marlin_linear.bias = None
    #     marlin_linear.forward = lambda x: _marlin_linear.apply(layer=marlin_linear, x=x, bias=marlin_linear.bias)

    # torch.cuda.empty_cache()
    # #################################################################

    return W, gemlite_linear, torchao_linear, bitblas_linear, marlin_linear


#############################################################################################################
W, gemlite_linear, torchao_linear, bitblas_linear, marlin_linear = gen_data(in_features, out_features, W_nbits, group_size)

if(matmul_type == "AUTO"):
    BATCH_SIZES = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]
    HQQLinearBitBlas.DEFAULT_BATCHSIZE = [1, 16]

if(matmul_type == "GEMV"):
    BATCH_SIZES = [1, 2, 4, 8]
    gemlite_linear.forward = lambda x: gemlite_linear.forward_manual(x, matmul_type="GEMV")
    HQQLinearBitBlas.DEFAULT_BATCHSIZE = [1]

if(matmul_type == "GEMM"):
    BATCH_SIZES = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]
    gemlite_linear.forward = lambda x: gemlite_linear.forward_manual(x, matmul_type="GEMM")
    HQQLinearBitBlas.DEFAULT_BATCHSIZE = [16]

print("W_nbits", W_nbits, "group_size", group_size, "matmul_type", matmul_type)
for batch_size in BATCH_SIZES:

    x = torch.randn((batch_size, in_features), dtype=gemlite_linear.compute_dtype, device='cuda:0')/10.
    check_valid(x, W, gemlite_linear)

    ref_time = eval_time(lambda x: torch.matmul(x, W.T), {'x':x.to(W.dtype)}) 
    print("ref_time", ref_time)
    
    if(gemlite_linear is not None):
        triton_time  = eval_time(lambda x: gemlite_linear(x), {'x':x.to(gemlite_linear.compute_dtype)}) 
        print((batch_size, in_features, out_features), 'Triton Speed-up vs. torch.matmul', np.round(ref_time/triton_time, 2), 'time', triton_time)

    if(torchao_linear is not None):
        torchao_time = eval_time(lambda x: torchao_linear(x), {'x':x.to(torchao_linear.compute_dtype)}) 
        print((batch_size, in_features, out_features), 'Torchao Speed-up vs. torch.matmul', np.round(ref_time/torchao_time, 2))

    if(bitblas_linear is not None):
        bitblas_time = eval_time(lambda x: bitblas_linear(x), {'x':x.to(bitblas_linear.compute_dtype)}) 
        print((batch_size, in_features, out_features), 'Bitblas Speed-up vs. torch.matmul', np.round(ref_time/bitblas_time, 2))

    if(marlin_linear is not None):
        marlin_time = eval_time(lambda x: marlin_linear.forward(x), {'x':x.to(marlin_linear.compute_dtype)}) 
        print((batch_size, in_features, out_features), 'Marlin Speed-up vs. torch.matmul', np.round(ref_time/marlin_time, 2))

    print('----------------------------------------------')




