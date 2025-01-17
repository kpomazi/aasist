from collections import OrderedDict

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor
import numpy as np
from torch.utils import data
from torch.nn.parameter import Parameter

___author__ = "Hemlata Tak"
__email__ = "tak@eurecom.fr"


class SincConv(nn.Module):
    @staticmethod
    def to_mel(hz):
        return 2595 * np.log10(1 + hz / 700)

    @staticmethod
    def to_hz(mel):
        return 700 * (10**(mel / 2595) - 1)

    def __init__(
        self,
        #device,
        out_channels,
        kernel_size,
        in_channels=1,
        sample_rate=16000,
        stride=1,
        padding=0,
        dilation=1,
        bias=False,
        groups=1,
    ):
        super().__init__()

        if in_channels != 1:

            msg = (
                "SincConv only support one input channel (here, in_channels = {%i})"
                % (in_channels))
            raise ValueError(msg)

        self.out_channels = out_channels
        self.kernel_size = kernel_size
        self.sample_rate = sample_rate

        # Forcing the filters to be odd (i.e, perfectly symmetrics)
        if kernel_size % 2 == 0:
            self.kernel_size = self.kernel_size + 1

        #self.device = device
        self.stride = stride
        self.padding = padding
        self.dilation = dilation

        if bias:
            raise ValueError("SincConv does not support bias.")
        if groups > 1:
            raise ValueError("SincConv does not support groups.")

        # initialize filterbanks using Mel scale
        NFFT = 512
        f = int(self.sample_rate / 2) * np.linspace(0, 1, int(NFFT / 2) + 1)
        fmel = self.to_mel(f)  # Hz to mel conversion
        fmelmax = np.max(fmel)
        fmelmin = np.min(fmel)
        filbandwidthsmel = np.linspace(fmelmin, fmelmax, self.out_channels + 1)
        filbandwidthsf = self.to_hz(filbandwidthsmel)  # Mel to Hz conversion
        self.mel = filbandwidthsf
        self.hsupp = torch.arange(-(self.kernel_size - 1) / 2,
                                  (self.kernel_size - 1) / 2 + 1)
        self.band_pass = torch.zeros(self.out_channels, self.kernel_size)

    def forward(self, x):
        for i in range(len(self.mel) - 1):
            fmin = self.mel[i]
            fmax = self.mel[i + 1]
            hHigh = (2 * fmax / self.sample_rate) * np.sinc(
                2 * fmax * self.hsupp / self.sample_rate)
            hLow = (2 * fmin / self.sample_rate) * np.sinc(
                2 * fmin * self.hsupp / self.sample_rate)
            hideal = hHigh - hLow

            self.band_pass[i, :] = Tensor(np.hamming(
                self.kernel_size)) * Tensor(hideal)

        band_pass_filter = self.band_pass.to(x.device)

        self.filters = (band_pass_filter).view(self.out_channels, 1,
                                               self.kernel_size)

        return F.conv1d(
            x,
            self.filters,
            stride=self.stride,
            padding=self.padding,
            dilation=self.dilation,
            bias=None,
            groups=1,
        )


class Residual_block(nn.Module):
    def __init__(self, nb_filts, first=False):
        super(Residual_block, self).__init__()
        self.first = first

        if not self.first:
            self.bn1 = nn.BatchNorm1d(num_features=nb_filts[0])

        self.lrelu = nn.LeakyReLU(negative_slope=0.3)

        self.conv1 = nn.Conv1d(
            in_channels=nb_filts[0],
            out_channels=nb_filts[1],
            kernel_size=3,
            padding=1,
            stride=1,
        )

        self.bn2 = nn.BatchNorm1d(num_features=nb_filts[1])
        self.conv2 = nn.Conv1d(
            in_channels=nb_filts[1],
            out_channels=nb_filts[1],
            padding=1,
            kernel_size=3,
            stride=1,
        )

        if nb_filts[0] != nb_filts[1]:
            self.downsample = True
            self.conv_downsample = nn.Conv1d(
                in_channels=nb_filts[0],
                out_channels=nb_filts[1],
                padding=0,
                kernel_size=1,
                stride=1,
            )

        else:
            self.downsample = False
        self.mp = nn.MaxPool1d(3)

    def forward(self, x):
        identity = x
        if not self.first:
            out = self.bn1(x)
            out = self.lrelu(out)
        else:
            out = x

        out = self.conv1(x)
        out = self.bn2(out)
        out = self.lrelu(out)
        out = self.conv2(out)

        if self.downsample:
            identity = self.conv_downsample(identity)

        out += identity
        out = self.mp(out)
        return out
    

class BasicBlock(nn.Module):
    def __init__(self, in_channels, out_channels, stride=1):
        super(BasicBlock, self).__init__()
        self.conv1 = nn.Conv1d(in_channels, out_channels, kernel_size=3, stride=stride, padding=1, bias=False)
        self.bn1 = nn.BatchNorm1d(out_channels)
        self.relu = nn.ReLU(inplace=True)
        self.conv2 = nn.Conv1d(out_channels, out_channels, kernel_size=3, stride=1, padding=1, bias=False)
        self.bn2 = nn.BatchNorm1d(out_channels)
        
        self.shortcut = nn.Sequential()
        if stride != 1 or in_channels != out_channels:
            self.shortcut = nn.Sequential(
                nn.Conv1d(in_channels, out_channels, kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm1d(out_channels)
            )

    def forward(self, x):
        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)
        out = self.conv2(out)
        out = self.bn2(out)
        out += self.shortcut(x)
        out = self.relu(out)
        return out
    
class Model2(nn.Module):
    def __init__(self, d_args):
        super().__init__()

        self.Sinc_conv = SincConv(
            #device=self.device,
            out_channels=d_args["filts"][0],
            kernel_size=d_args["first_conv"],
            in_channels=d_args["in_channels"],
        )

        self.in_channels = d_args["in_channels"]

        self.first_bn = nn.BatchNorm1d(num_features=d_args["filts"][0])
        self.selu = nn.SELU(inplace=True)
        
        self.block0 = nn.Sequential(
            Residual_block(nb_filts=d_args["filts"][1], first=True))
        self.block1 = nn.Sequential(
            Residual_block(nb_filts=d_args["filts"][1]))
        self.block2 = nn.Sequential(
            Residual_block(nb_filts=d_args["filts"][2]))
        d_args["filts"][2][0] = d_args["filts"][2][1]
        self.block3 = nn.Sequential(
            Residual_block(nb_filts=d_args["filts"][2]))
        
        self.avgpool = nn.AdaptiveAvgPool1d(1)

        self.bn_before_gru = nn.BatchNorm1d(
            num_features=d_args["filts"][2][-1])
        self.gru = nn.GRU(
            input_size=d_args["filts"][2][-1],
            hidden_size=d_args["gru_node"],
            num_layers=d_args["nb_gru_layer"],
            batch_first=True,
        )

        self.fc1_gru = nn.Linear(in_features=d_args["gru_node"],
                                 out_features=d_args["nb_fc_node"])

        self.fc2_gru = nn.Linear(
            in_features=d_args["nb_fc_node"],
            out_features=d_args["nb_classes"],
            bias=True,
        )

        self.sig = nn.Sigmoid()
        self.logsoftmax = nn.LogSoftmax(dim=1)

    def _make_layer(self, block, out_channels, num_blocks, stride):
        strides = [stride] + [1] * (num_blocks - 1)
        layers = []
        for stride in strides:
            layers.append(block(self.in_channels, out_channels, stride))
            self.in_channels = out_channels
        return nn.Sequential(*layers)

    def forward(self, x, Freq_aug=None):
        nb_samp = x.shape[0]
        len_seq = x.shape[1]
        x = x.view(nb_samp, 1, len_seq)

        x = self.Sinc_conv(x)
        x = F.max_pool1d(torch.abs(x), 3)
        x = self.first_bn(x)
        x = self.selu(x)
      
        x = self.block0(x)
        x = self.block1(x)
        x = self.block2(x)
        x = self.block3(x)
        
        x = self.avgpool(x)
        x = self.bn_before_gru(x)
        x = self.selu(x)
        x = x.permute(0, 2, 1)  # (batch, filt, time) >> (batch, time, filt)
        self.gru.flatten_parameters()
        x, _ = self.gru(x)
        x = x[:, -1, :]
        last_hidden = self.fc1_gru(x)
        x = self.fc2_gru(last_hidden)
        output = self.logsoftmax(x)
        return output, last_hidden

class Model(nn.Module):
    #def __init__(self, d_args, device):
    def __init__(self, d_args):
        super().__init__()

        #self.device = device
        self.Sinc_conv = SincConv(
            #device=self.device,
            out_channels=d_args["filts"][0],
            kernel_size=d_args["first_conv"],
            in_channels=d_args["in_channels"],
        )

        self.first_bn = nn.BatchNorm1d(num_features=d_args["filts"][0])
        self.selu = nn.SELU(inplace=True)
        self.block0 = nn.Sequential(
            Residual_block(nb_filts=d_args["filts"][1], first=True))
        self.block1 = nn.Sequential(
            Residual_block(nb_filts=d_args["filts"][1]))
        self.block2 = nn.Sequential(
            Residual_block(nb_filts=d_args["filts"][2]))
        d_args["filts"][2][0] = d_args["filts"][2][1]
        self.block3 = nn.Sequential(
            Residual_block(nb_filts=d_args["filts"][2]))
        self.block4 = nn.Sequential(
            Residual_block(nb_filts=d_args["filts"][2]))
        self.block5 = nn.Sequential(
            Residual_block(nb_filts=d_args["filts"][2]))
        self.avgpool = nn.AdaptiveAvgPool1d(1)

        self.bn_before_gru = nn.BatchNorm1d(
            num_features=d_args["filts"][2][-1])
        self.gru = nn.GRU(
            input_size=d_args["filts"][2][-1],
            hidden_size=d_args["gru_node"],
            num_layers=d_args["nb_gru_layer"],
            batch_first=True,
        )

        self.fc1_gru = nn.Linear(in_features=d_args["gru_node"],
                                 out_features=d_args["nb_fc_node"])

        self.fc2_gru = nn.Linear(
            in_features=d_args["nb_fc_node"],
            out_features=d_args["nb_classes"],
            bias=True,
        )

        self.sig = nn.Sigmoid()
        self.logsoftmax = nn.LogSoftmax(dim=1)

    def forward(self, x, Freq_aug=None):

        nb_samp = x.shape[0]
        len_seq = x.shape[1]
        x = x.view(nb_samp, 1, len_seq)

        x = self.Sinc_conv(x)
        x = F.max_pool1d(torch.abs(x), 3)
        x = self.first_bn(x)
        x = self.selu(x)

        x = self.block0(x)
        x = self.block1(x)
        x = self.block2(x)
        x = self.block3(x)
        x = self.block4(x)
        x = self.block5(x)
        
        x = self.bn_before_gru(x)
        x = self.selu(x)
        x = x.permute(0, 2, 1)  # (batch, filt, time) >> (batch, time, filt)
        self.gru.flatten_parameters()
        x, _ = self.gru(x)
        x = x[:, -1, :]
        last_hidden = self.fc1_gru(x)
        x = self.fc2_gru(last_hidden)
        output = self.logsoftmax(x)

        return last_hidden, output

    def _make_attention_fc(self, in_features, l_out_features):

        l_fc = []

        l_fc.append(
            nn.Linear(in_features=in_features, out_features=l_out_features))

        return nn.Sequential(*l_fc)

    def _make_layer(self, nb_blocks, nb_filts, first=False):
        layers = []
        # def __init__(self, nb_filts, first = False):
        for i in range(nb_blocks):
            first = first if i == 0 else False
            layers.append(Residual_block(nb_filts=nb_filts, first=first))
            if i == 0:
                nb_filts[0] = nb_filts[1]

        return nn.Sequential(*layers)
