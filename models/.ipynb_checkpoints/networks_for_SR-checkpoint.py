import torch
import torch.nn as nn
from torch.nn import init
import functools
from torch.optim import lr_scheduler
# from .EDSR import EDSR 
import math
import torch
import torch.nn as nn
import torch.nn.functional as F
###############################################################################
# Helper Functions
###############################################################################


class Identity(nn.Module):
    def forward(self, x):
        return x


def get_norm_layer(norm_type='instance'):
    """Return a normalization layer

    Parameters:
        norm_type (str) -- the name of the normalization layer: batch | instance | none

    For BatchNorm, we use learnable affine parameters and track running statistics (mean/stddev).
    For InstanceNorm, we do not use learnable affine parameters. We do not track running statistics.
    """
    if norm_type == 'batch':
        norm_layer = functools.partial(nn.BatchNorm2d, affine=True, track_running_stats=True)
    elif norm_type == 'instance':
        norm_layer = functools.partial(nn.InstanceNorm2d, affine=False, track_running_stats=False)
    elif norm_type == 'group':
        norm_layer = functools.partial(nn.GroupNorm, affine=False)        
    elif norm_type == 'none':
        def norm_layer(x): return Identity()
    else:
        raise NotImplementedError('normalization layer [%s] is not found' % norm_type)
    return norm_layer


def get_scheduler(optimizer, opt):
    """Return a learning rate scheduler

    Parameters:
        optimizer          -- the optimizer of the network
        opt (option class) -- stores all the experiment flags; needs to be a subclass of BaseOptions．　
                              opt.lr_policy is the name of learning rate policy: linear | step | plateau | cosine

    For 'linear', we keep the same learning rate for the first <opt.n_epochs> epochs
    and linearly decay the rate to zero over the next <opt.n_epochs_decay> epochs.
    For other schedulers (step, plateau, and cosine), we use the default PyTorch schedulers.
    See https://pytorch.org/docs/stable/optim.html for more details.
    """
    if opt.lr_policy == 'linear':
        def lambda_rule(epoch):
            lr_l = 1.0 - max(0, epoch + opt.epoch_count - opt.n_epochs) / float(opt.n_epochs_decay + 1)
            return lr_l
        scheduler = lr_scheduler.LambdaLR(optimizer, lr_lambda=lambda_rule)
    elif opt.lr_policy == 'step':
        scheduler = lr_scheduler.StepLR(optimizer, step_size=opt.lr_decay_iters, gamma=0.1)
    elif opt.lr_policy == 'plateau':
        scheduler = lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.2, threshold=0.01, patience=5)
    elif opt.lr_policy == 'cosine':
        scheduler = lr_scheduler.CosineAnnealingLR(optimizer, T_max=opt.n_epochs, eta_min=0)
    else:
        return NotImplementedError('learning rate policy [%s] is not implemented', opt.lr_policy)
    return scheduler


def init_weights(net, init_type='normal', init_gain=0.02):
    """Initialize network weights.

    Parameters:
        net (network)   -- network to be initialized
        init_type (str) -- the name of an initialization method: normal | xavier | kaiming | orthogonal
        init_gain (float)    -- scaling factor for normal, xavier and orthogonal.

    We use 'normal' in the original pix2pix and CycleGAN paper. But xavier and kaiming might
    work better for some applications. Feel free to try yourself.
    """
    def init_func(m):  # define the initialization function
        classname = m.__class__.__name__
        if hasattr(m, 'weight') and (classname.find('Conv') != -1 or classname.find('Linear') != -1):
            if init_type == 'normal':
                init.normal_(m.weight.data, 0.0, init_gain)
            elif init_type == 'xavier':
                init.xavier_normal_(m.weight.data, gain=init_gain)
            elif init_type == 'kaiming':
                init.kaiming_normal_(m.weight.data, a=0, mode='fan_in')
            elif init_type == 'orthogonal':
                init.orthogonal_(m.weight.data, gain=init_gain)
            else:
                raise NotImplementedError('initialization method [%s] is not implemented' % init_type)
            if hasattr(m, 'bias') and m.bias is not None:
                init.constant_(m.bias.data, 0.0)
        elif classname.find('BatchNorm2d') != -1:  # BatchNorm Layer's weight is not a matrix; only normal distribution applies.
            init.normal_(m.weight.data, 1.0, init_gain)
            init.constant_(m.bias.data, 0.0)

    print('initialize network with %s' % init_type)
    net.apply(init_func)  # apply the initialization function <init_func>


def init_net(net, init_type='normal', init_gain=0.02, gpu_ids=[]):
    """Initialize a network: 1. register CPU/GPU device (with multi-GPU support); 2. initialize the network weights
    Parameters:
        net (network)      -- the network to be initialized
        init_type (str)    -- the name of an initialization method: normal | xavier | kaiming | orthogonal
        gain (float)       -- scaling factor for normal, xavier and orthogonal.
        gpu_ids (int list) -- which GPUs the network runs on: e.g., 0,1,2

    Return an initialized network.
    """
    if len(gpu_ids) > 0:
        assert(torch.cuda.is_available())
        net.to(gpu_ids[0])
        net = torch.nn.DataParallel(net, gpu_ids)  # multi-GPUs
    init_weights(net, init_type, init_gain=init_gain)
    return net


def define_net(input_nc, output_nc, ngf, net_type, norm='batch', use_dropout=False, init_type='normal', init_gain=0.02, gpu_ids=[], replace_transpose=False, n_down = 2):
    """Create a generator

    Parameters:
        input_nc (int) -- the number of channels in input images
        output_nc (int) -- the number of channels in output images
        ngf (int) -- the number of filters in the last conv layer
        netG (str) -- the architecture's name: resnet_9blocks | resnet_6blocks | unet_256 | unet_128
        norm (str) -- the name of normalization layers used in the network: batch | instance | none
        use_dropout (bool) -- if use dropout layers.
        init_type (str)    -- the name of our initialization method.
        init_gain (float)  -- scaling factor for normal, xavier and orthogonal.
        gpu_ids (int list) -- which GPUs the network runs on: e.g., 0,1,2

    Returns a generator

    Our current implementation provides two types of generators:
        U-Net: [unet_128] (for 128x128 input images) and [unet_256] (for 256x256 input images)
        The original U-Net paper: https://arxiv.org/abs/1505.04597

        Resnet-based generator: [resnet_6blocks] (with 6 Resnet blocks) and [resnet_9blocks] (with 9 Resnet blocks)
        Resnet-based generator consists of several Resnet blocks between a few downsampling/upsampling operations.
        We adapt Torch code from Justin Johnson's neural style transfer project (https://github.com/jcjohnson/fast-neural-style).


    The generator has been initialized by <init_net>. It uses RELU for non-linearity.
    """
    net = None
    norm_layer = get_norm_layer(norm_type=norm)

    if net_type == 'EncoderDepth':
        net = ResnetGenerator(input_nc, output_nc, ngf, norm_layer=norm_layer, use_dropout=use_dropout, n_blocks=9, replace_transpose=replace_transpose, n_downsampling = n_down)
    elif net_type == 'DenseNet':
        net = ResnetGenerator(input_nc, output_nc, ngf, norm_layer=norm_layer, use_dropout=use_dropout, n_blocks=6, replace_transpose=replace_transpose, n_downsampling = n_down)
    elif net_type == 'unet_128':
        net = UnetGenerator(input_nc, output_nc, 7, ngf, norm_layer=norm_layer, use_dropout=use_dropout)
    else:
        raise NotImplementedError('Generator model name [%s] is not recognized' % netG)
    return init_net(net, init_type, init_gain, gpu_ids)




##############################################################################
# Classes
##############################################################################



    
class Convs(nn.Module):

    def __init__(self, input_nc, output_nc, ngf=32, norm_layer=nn.BatchNorm2d, use_bias = False):

        super(Convs, self).__init__()


        model = [
                 nn.Conv2d(input_nc, ngf, kernel_size=3, padding=1, bias=use_bias),
                 norm_layer(ngf),
                 nn.ReLU(True)]
        
        self.model = nn.Sequential(*model)

    def forward(self, input):
        return self.model(input)

class EncoderDepth(nn.Module):
    def __init__(self, input_nc, output_nc, ngf=16, norm_layer=nn.BatchNorm2d, use_bias = False, use_dropout=False,  padding_type='reflect',  down=5):
        super().__init__()


        self.first_convs = nn.Sequential(nn.Conv2d(input_nc, ngf, kernel_size=3, padding=1),
                 norm_layer(ngf),
                 nn.ReLU(True),
                 nn.Conv2d(input_nc, ngf, kernel_size=3, padding=1),
                 norm_layer(ngf),
                 nn.ReLU(True))

        self.blocks = []
        mult = 1
        for i in range(down):
            mult*= 2
            new_block = nn.Sequential(nn.Conv2d(ngf * mult, ngf * mult, kernel_size=3, stride=1, padding=1, bias=use_bias),
                                      norm_layer(ngf * mult),
                                      nn.ReLU(True),
                                      
                                      nn.Conv2d(ngf * mult, ngf * mult, kernel_size=3, stride=1, padding=1, bias=use_bias),
                                      norm_layer(ngf * mult),
                                      nn.ReLU(True),
                                      nn.MaxPool2d(kernel_size=2))
            self.blocks.append(new_block) 


    def forward(self, input):

        outputs = []
        outputs.append(self.first_convs(input))
        for block in self.blocks:
            outputs.append(block(outputs[-1]))
        
        return outputs

    
class Decoder(nn.Module):
    def __init__(self, input_nc, output_nc=3, ngf=64, norm_layer=nn.BatchNorm2d, use_bias = False, use_dropout=False,  padding_type='reflect',  up_layers=4, start_dim = 32,  growth_rate=32, ns = [6,12,24,16],
                 reduction=0.5):
        super().__init__()


        a= start_dims
        k= growth_rate
        n = ns
        l = 4
        r = reduction
        dims_im =[3, start_dims]
        for i in range(0,l):
            a = a+k*n[i]
            a=int(a*r)
            dims.append(a)
            
        dims_d = [8, 16, 32, 64, 128, 256]    
        
        curr_dim = dims_d[0] + dims_im[0]
        self.first_convs = nn.Sequential(nn.Conv2d(curr_dim, curr_dim//2, kernel_size=3, padding=1),
                                         norm_layer(ngf),
                                         nn.ReLU(True),
                                         nn.Upsample(scale_factor = 2, mode='nearest'))
        curr_dim = curr_dim//2
        self.blocks = [self.first_convs]
        mult = 1
        for i in range(up_layers):
            curr_dim = curr_dim + dims_d[i+1] + dims_d[i+1]
            new_block = nn.Sequential(nn.Conv2d(curr_dim, curr_dim, kernel_size=3, stride=1, padding=1, bias=use_bias),
                                      norm_layer(ngf),
                                      nn.ReLU(True),
                
                                      nn.Conv2d(curr_dim, curr_dim//2, kernel_size=3, stride=1, padding=1, bias=use_bias),
                                      norm_layer(ngf * mult),
                                      nn.ReLU(True),                
                                      nn.Upsample(scale_factor = 2, mode='nearest'))
            curr_dim=curr_dim//2
            self.blocks.append(new_block) 


    def forward(self, x):

        outputs = [input]
        for block in self.blocks:
            outputs.append(block(outputs[-1]))

        return outputs   

    
    
    
nn.Upsample(scale_factor = 2, mode='nearest')
nn.ReflectionPad2d(1),
nn.ReplicationPad2d(1)
    

class BasicBlock(nn.Module):
    def __init__(self, in_planes, out_planes, dropRate=0.0):
        super(BasicBlock, self).__init__()
        self.bn1 = nn.BatchNorm2d(in_planes)
        self.relu = nn.ReLU(inplace=True)
        self.conv1 = nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=1,
                               padding=1, bias=False)
        self.droprate = dropRate
    def forward(self, x):
        out = self.conv1(self.relu(self.bn1(x)))
        if self.droprate > 0:
            out = F.dropout(out, p=self.droprate, training=self.training)
        return torch.cat([x, out], 1)

class BottleneckBlock(nn.Module):
    def __init__(self, in_planes, out_planes, dropRate=0.0):
        super(BottleneckBlock, self).__init__()
        inter_planes = out_planes * 4
        self.bn1 = nn.BatchNorm2d(in_planes)
        self.relu = nn.ReLU(inplace=True)
        self.conv1 = nn.Conv2d(in_planes, inter_planes, kernel_size=1, stride=1,
                               padding=0, bias=False)
        self.bn2 = nn.BatchNorm2d(inter_planes)
        self.conv2 = nn.Conv2d(inter_planes, out_planes, kernel_size=3, stride=1,
                               padding=1, bias=False)
        self.droprate = dropRate
    def forward(self, x):
        out = self.conv1(self.relu(self.bn1(x)))
        if self.droprate > 0:
            out = F.dropout(out, p=self.droprate, inplace=False, training=self.training)
        out = self.conv2(self.relu(self.bn2(out)))
        if self.droprate > 0:
            out = F.dropout(out, p=self.droprate, inplace=False, training=self.training)
        return torch.cat([x, out], 1)

class TransitionBlock(nn.Module):
    def __init__(self, in_planes, out_planes, dropRate=0.0):
        super(TransitionBlock, self).__init__()
        self.bn1 = nn.BatchNorm2d(in_planes)
        self.relu = nn.ReLU(inplace=True)
        self.conv1 = nn.Conv2d(in_planes, out_planes, kernel_size=1, stride=1,
                               padding=0, bias=False)
        self.droprate = dropRate
        self.maxpool = nn.MaxPool2d(kernel_size=2)
    def forward(self, x):
        out = self.conv1(self.relu(self.bn1(x)))
        if self.droprate > 0:
            out = F.dropout(out, p=self.droprate, inplace=False, training=self.training)
        out = self.maxpool(out)
#         return F.avg_pool2d(out, 2)
        return out

class DenseBlock(nn.Module):
    def __init__(self, nb_layers, in_planes, growth_rate, block, dropRate=0.0):
        super(DenseBlock, self).__init__()
        self.layer = self._make_layer(block, in_planes, growth_rate, nb_layers, dropRate)
    def _make_layer(self, block, in_planes, growth_rate, nb_layers, dropRate):
        layers = []
        for i in range(nb_layers):
            layers.append(block(in_planes+i*growth_rate, growth_rate, dropRate))
        return nn.Sequential(*layers)
    def forward(self, x):
        return self.layer(x)

class DenseNet(nn.Module):
    def __init__(self, depth, num_classes, start_dims=32, growth_rate=32,
                 reduction=0.5, bottleneck=True, dropRate=0.0):
        super(DenseNet, self).__init__()
        in_planes = start_dims
#         n = (depth - 5) / 4
        n = depth
        if bottleneck == True:
            block = BottleneckBlock
        else:
            block = BasicBlock
        n = int(n)


        # 1st conv before any dense block
        self.conv1 = nn.Conv2d(3, in_planes, kernel_size=3, stride=1,
                               padding=1, bias=False)
        self.maxpool1 = nn.MaxPool2d(kernel_size=2)
        
        # 1st block
        self.block1 = DenseBlock(n, in_planes, growth_rate, block, dropRate)
        in_planes = int(in_planes+n*growth_rate)
        self.trans1 = TransitionBlock(in_planes, int(math.floor(in_planes*reduction)), dropRate=dropRate)
        in_planes = int(math.floor(in_planes*reduction))
        
        # 2nd block
        self.block2 = DenseBlock(n, in_planes, growth_rate, block, dropRate)
        in_planes = int(in_planes+n*growth_rate)
        self.trans2 = TransitionBlock(in_planes, int(math.floor(in_planes*reduction)), dropRate=dropRate)
        in_planes = int(math.floor(in_planes*reduction))
        
        # 3rd block
        self.block3 = DenseBlock(n, in_planes, growth_rate, block, dropRate)
        in_planes = int(in_planes+n*growth_rate)
        self.trans3 = TransitionBlock(in_planes, int(math.floor(in_planes*reduction)), dropRate=dropRate)
        in_planes = int(math.floor(in_planes*reduction))
        
        # 4th block
        self.block4 = DenseBlock(n, in_planes, growth_rate, block, dropRate)
        in_planes = int(in_planes+n*growth_rate)
        self.trans4 = TransitionBlock(in_planes, int(math.floor(in_planes*reduction)), dropRate=dropRate)
        in_planes = int(math.floor(in_planes*reduction))

        self.in_planes = in_planes

#         for m in self.modules():
#             if isinstance(m, nn.Conv2d):
#                 n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
#                 m.weight.data.normal_(0, math.sqrt(2. / n))
#             elif isinstance(m, nn.BatchNorm2d):
#                 m.weight.data.fill_(1)
#                 m.bias.data.zero_()
#             elif isinstance(m, nn.Linear):
#                 m.bias.data.zero_()

    def forward(self, x):
        outputs = [x]
        out = self.maxpool1(self.conv1(x))
        outputs.append(out)
        
        out = self.trans1(self.block1(out))
        outputs.append(out)
        
        out = self.trans2(self.block2(out))
        outputs.append(out)
        
        out = self.trans3(self.block3(out))
        outputs.append(out)
        
        out = self.trans4(self.block4(out))
        outputs.append(out)
        
        return outputs
    
    
    
    
    