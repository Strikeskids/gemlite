import torch 
torch.manual_seed(1337)

def check_valid(x, W, quant_linear, tol=3e-3):
    y_ref = torch.matmul(x, W.T)
    y_q   = quant_linear(x)
    print((y_ref - y_q).abs().max())
    print((y_ref - y_q).abs().mean())
    assert torch.allclose(y_ref, y_q, atol=tol)

############################################################################################
from hqq.core.quantize import HQQLinear, BaseQuantizeConfig

in_features, out_features = 4096*4, 4096*2
# W_nbits, group_size = 8, in_features 
W_nbits, group_size = 4, 128 
W_nbits, group_size = 2, 128

linear       = torch.nn.Linear(in_features=in_features, out_features=out_features, bias=False, device='cpu')
quant_config = BaseQuantizeConfig(nbits=W_nbits, group_size=group_size, quant_zero=False, quant_scale=False, axis=1)
hqq_layer    = HQQLinear(linear, quant_config=quant_config, compute_dtype=torch.float16, device='cuda:0', del_orig=False) 

orig_shape   = (out_features, in_features)
W            = hqq_layer.dequantize().reshape(orig_shape)
############################################################################################

from gemlite.core import GemLiteLinearCUDA, DType
gemlite_linear = GemLiteLinearCUDA(W_nbits, group_size=group_size, in_features=in_features, out_features=out_features, input_dtype=DType.FP16, output_dtype=DType.FP16, acc_dtype=DType.FP16)

W_q           = hqq_layer.unpack().view(orig_shape)
scales        = hqq_layer.meta['scale']
zeros         = hqq_layer.meta['zero']
print(scales.shape, scales.device, zeros.shape)
print(scales[:4, 0])
print(zeros[:4, 0])
gemlite_linear.pack(W_q, scales, zeros, None)

batch_size = 1
x = torch.randn((batch_size, in_features), dtype=gemlite_linear.compute_dtype, device='cuda:0')/10.
check_valid(x, W, gemlite_linear)

