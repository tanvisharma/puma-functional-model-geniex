import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Function
from torch.nn.modules.utils import _pair
from torch.nn import init

import math
import numpy as np
from mvm import *

# Custom conv2d formvm function: Doesn't work for back-propagation
class Conv2d_mvm_function(Function):

    # Note that both forward and backward are @staticmethods
    @staticmethod
    # +--------------------------+
    # |            MVM           |   
    # +--------------------------+
    def forward(ctx, input, weight, bias=None, stride=1, padding=0, dilation=1, groups=1):
        
        weight_channels_out = weight.shape[0]
        weight_channels_in = weight.shape[1]
        weight_row = weight.shape[2]
        weight_col = weight.shape[3]

        length = weight_channels_in * weight_row * weight_col
        flatten_weight = weight.reshape((weight_channels_out, length))  ## flatten weights
        flatten_bit_slice_weight = bit_slice_weight(flatten_weight, 2)  ## flatten weights --> 16bit fixed point --> bit slice

        # bitsliced weight into 128x128 xbars 
        # xbar_row separates inputs --> results in a same column with different rows will be added later
        xbar_row = math.ceil(flatten_bit_slice_weight.shape[0]/128)
        xbar_col = math.ceil(flatten_bit_slice_weight.shape[1]/128)
        xbars = np.zeros((xbar_row,xbar_col), dtype=xbar)
        for i in range(xbar_row):
            if (i+1)*128 > flatten_bit_slice_weight.shape[0]:
                end_row = flatten_bit_slice_weight.shape[0]
            else:
                end_row = (i+1)*128
            for j in range(xbar_col):
                if (j+1)*128 > flatten_bit_slice_weight.shape[1]:
                    end_col = flatten_bit_slice_weight.shape[1]
                else:
                    end_col = (j+1)*128
                xbars[i,j] = xbar(flatten_bit_slice_weight[i*128:end_row, j*128:end_col])
        xbars_out = torch.zeros(math.ceil(weight_channels_out/16)*16)
        print(xbars.shape)

        input_batch = input.shape[0]
        input_channels = input.shape[1]     # weight_channels_in == input_channels
        input_row = input.shape[2] + padding[0]*2
        input_col = input.shape[3] + padding[1]*2
        input_pad = torch.zeros((input_batch, input_channels, input_row, input_col))
        input_pad[:,:,padding[0]:input_row-padding[0],padding[1]:input_col-padding[1]] = input
        
        output_row = input_row - weight_row + 1
        output_col = input_col - weight_col + 1 
        output = torch.zeros((weight_channels_out, output_row, output_col))


        for i in range(output_row):
            for j in range(output_col):
                flatten_input = input_pad[:,:, i:i+weight_row, j:j+weight_col].flatten()    ## one set of inputs --> flatten
                print(i,j)
                for x_i in range(xbar_row):
                    if (x_i+1)*128 > input_pad.shape[0]:
                        end_row = input_pad.shape[0]
                    else:
                        end_row = (x_i+1)*128

                    # input has 128 rows regardless of its original size cuz crossbar: 128 x 128
                    flatten_binary_input = torch.zeros((128,16))
                    for l in range(128):
                        if x_i*128+l>=flatten_input.shape[0]:
                            break
                        flatten_binary_input[l] = float_to_16bits(flatten_input[x_i*128+l])

                    for x_j in range(xbar_col):
                        xbars_out[x_j*16:(x_j+1)*16] += xbars[x_i,x_j].mvm(flatten_binary_input)
                output[:,i,j] = xbars_out[:weight_channels_out]
                xbars_out.fill_(0)


        ctx.save_for_backward(input, weight, bias)
        ctx.stride = stride
        ctx.padding = padding 
        ctx.dilation = dilation
        ctx.groups = groups

        return output

    # This function has only a single output, so it gets only one gradient
    @staticmethod
    def backward(ctx, grad_output):
        # This is a pattern that is very convenient - at the top of backward
        # unpack saved_tensors and initialize all gradients w.r.t. inputs to
        # None. Thanks to the fact that additional trailing Nones are
        # ignored, the return statement is simple even when the function has
        # optional inputs.
        input, weight, bias = ctx.saved_tensors
        
        stride = ctx.stride
        padding = ctx.padding 
        dilation = ctx.dilation
        groups = ctx.groups
        grad_input = grad_weight = grad_bias = None
        
        # These needs_input_grad checks are optional and there only to
        # improve efficiency. If you want to make your code simpler, you can
        # skip them. Returning gradients for inputs that don't require it is
        # not an error.
        if ctx.needs_input_grad[0]:
            grad_input = torch.nn.grad.conv2d_input(input.shape, weight, grad_output, stride, padding, dilation, groups)
        if ctx.needs_input_grad[1]:
            grad_weight = torch.nn.grad.conv2d_weight(input, weight.shape, grad_output, stride, padding, dilation, groups) 
        if bias is not None and ctx.needs_input_grad[2]:
            grad_bias = grad_output.sum((0,2,3)).squeeze(0)
            
        print(grad_weight)
        return grad_input, grad_weight, grad_bias, None, None, None, None


class _ConvNd_mvm(nn.Module):

    __constants__ = ['stride', 'padding', 'dilation', 'groups', 'bias', 'padding_mode']

    def __init__(self, in_channels, out_channels, kernel_size, stride,
                 padding, dilation, transposed, output_padding,
                 groups, bias, padding_mode, check_grad=False):
        super(_ConvNd_mvm, self).__init__()
        if in_channels % groups != 0:
            raise ValueError('in_channels must be divisible by groups')
        if out_channels % groups != 0:
            raise ValueError('out_channels must be divisible by groups')
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size
        self.stride = stride
        self.padding = padding
        self.dilation = dilation
        self.transposed = transposed
        self.output_padding = output_padding
        self.groups = groups
        self.padding_mode = padding_mode

        if check_grad:
            tensor_constructor = torch.DoubleTensor # double precision required to check grad
        else:
            tensor_constructor = torch.Tensor # In PyTorch torch.Tensor is alias torch.FloatTensor

        if transposed:
            self.weight = nn.Parameter(tensor_constructor(
                in_channels, out_channels // groups, *kernel_size))
        else:
            self.weight = nn.Parameter(tensor_constructor(
                out_channels, in_channels // groups, *kernel_size))
        if bias:
            self.bias = nn.Parameter(tensor_constructor(out_channels))
        else:
            self.register_parameter('bias', None)
        self.reset_parameters()

    def reset_parameters(self):
        init.kaiming_uniform_(self.weight, a=math.sqrt(5))
        
        if self.bias is not None:
            fan_in, _ = init._calculate_fan_in_and_fan_out(self.weight)
            bound = 1 / math.sqrt(fan_in)
            init.uniform_(self.bias, -bound, bound)

    def extra_repr(self):
        s = ('{in_channels}, {out_channels}, kernel_size={kernel_size}'
             ', stride={stride}')
        if self.padding != (0,) * len(self.padding):
            s += ', padding={padding}'
        if self.dilation != (1,) * len(self.dilation):
            s += ', dilation={dilation}'
        if self.output_padding != (0,) * len(self.output_padding):
            s += ', output_padding={output_padding}'
        if self.groups != 1:
            s += ', groups={groups}'
        if self.bias is None:
            s += ', bias=False'
        return s.format(**self.__dict__)

class Conv2d_mvm(_ConvNd_mvm):
    def __init__(self, in_channels, out_channels, kernel_size, stride=1,
                 padding=0, dilation=1, groups=1,
                 bias=True, padding_mode='zeros', check_grad=False):
        kernel_size = _pair(kernel_size)
        stride = _pair(stride)
        padding = _pair(padding)
        dilation = _pair(dilation)
        super(Conv2d_mvm, self).__init__(
            in_channels, out_channels, kernel_size, stride, padding, dilation,
            False, _pair(0), groups, bias, padding_mode)
    #@weak_script_method
    def forward(self, input):
        if self.padding_mode == 'circular':
            expanded_padding = ((self.padding[1] + 1) // 2, self.padding[1] // 2,
                                (self.padding[0] + 1) // 2, self.padding[0] // 2)
            return Conv2d_mvm_function.apply(F.pad(input, expanded_padding, mode='circular'),
                            self.weight, self.bias, self.stride,
                            _pair(0), self.dilation, self.groups)
        else:
            return Conv2d_mvm_function.apply(input, self.weight, self.bias, self.stride,
                            self.padding, self.dilation, self.groups)




