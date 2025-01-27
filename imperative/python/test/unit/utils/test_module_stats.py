import math
from copy import deepcopy

import numpy as np
import pytest

import megengine as mge
import megengine.functional as F
import megengine.hub as hub
import megengine.module as M
from megengine.core._trace_option import use_symbolic_shape
from megengine.utils.module_stats import module_stats


@pytest.mark.skipif(
    use_symbolic_shape(), reason="This test do not support symbolic shape.",
)
def test_module_stats():
    net = ResNet(BasicBlock, [2, 2, 2, 2])
    input_shape = (1, 3, 224, 224)
    total_stats, stats_details = module_stats(net, input_shapes=input_shape)
    x1 = np.random.random((1, 3, 224, 224)).astype("float32")
    gt_flops, gt_acts = net.get_stats(mge.tensor(x1))
    assert (total_stats.flops, total_stats.act_dims) == (gt_flops, gt_acts,)

    total_stats, stats_details = module_stats(net, inputs=x1)
    assert (total_stats.flops, total_stats.act_dims) == (gt_flops, gt_acts,)


class BasicBlock(M.Module):
    expansion = 1

    def __init__(
        self,
        in_channels,
        channels,
        stride=1,
        groups=1,
        base_width=64,
        dilation=1,
        norm=M.BatchNorm2d,
    ):
        super().__init__()

        self.tmp_in_channels = in_channels
        self.tmp_channels = channels
        self.stride = stride

        if groups != 1 or base_width != 64:
            raise ValueError("BasicBlock only supports groups=1 and base_width=64")
        if dilation > 1:
            raise NotImplementedError("Dilation > 1 not supported in BasicBlock")
        self.conv1 = M.Conv2d(
            in_channels, channels, 3, stride, padding=dilation, bias=False
        )
        self.bn1 = norm(channels)
        self.conv2 = M.Conv2d(channels, channels, 3, 1, padding=1, bias=False)
        self.bn2 = norm(channels)

        self.downsample_id = M.Identity()
        self.downsample_conv = M.Conv2d(in_channels, channels, 1, stride, bias=False)
        self.downsample_norm = norm(channels)

    def forward(self, x):
        identity = x
        x = self.conv1(x)
        x = self.bn1(x)
        x = F.relu(x)
        x = self.conv2(x)
        x = self.bn2(x)
        if self.tmp_in_channels == self.tmp_channels and self.stride == 1:
            identity = self.downsample_id(identity)
        else:
            identity = self.downsample_conv(identity)
            identity = self.downsample_norm(identity)
        x += identity
        x = F.relu(x)
        return x

    def get_stats(self, x):
        activations, flops = 0, 0

        identity = x

        in_x = deepcopy(x)
        x = self.conv1(x)
        tmp_flops, tmp_acts = cal_conv_stats(self.conv1, in_x, x)
        activations += tmp_acts
        flops += tmp_flops

        in_x = deepcopy(x)
        x = self.bn1(x)
        tmp_flops, tmp_acts = cal_norm_stats(self.bn1, in_x, x)
        activations += tmp_acts
        flops += tmp_flops

        x = F.relu(x)

        in_x = deepcopy(x)
        x = self.conv2(x)
        tmp_flops, tmp_acts = cal_conv_stats(self.conv2, in_x, x)
        activations += tmp_acts
        flops += tmp_flops

        in_x = deepcopy(x)
        x = self.bn2(x)
        tmp_flops, tmp_acts = cal_norm_stats(self.bn2, in_x, x)
        activations += tmp_acts
        flops += tmp_flops

        if self.tmp_in_channels == self.tmp_channels and self.stride == 1:
            identity = self.downsample_id(identity)
        else:
            in_x = deepcopy(identity)
            identity = self.downsample_conv(identity)
            tmp_flops, tmp_acts = cal_conv_stats(self.downsample_conv, in_x, identity)
            activations += tmp_acts
            flops += tmp_flops

            in_x = deepcopy(identity)
            identity = self.downsample_norm(identity)
            tmp_flops, tmp_acts = cal_norm_stats(self.downsample_norm, in_x, identity)
            activations += tmp_acts
            flops += tmp_flops

        x += identity
        x = F.relu(x)

        return x, flops, activations


class ResNet(M.Module):
    def __init__(
        self,
        block,
        layers=[2, 2, 2, 2],
        num_classes=1000,
        zero_init_residual=False,
        groups=1,
        width_per_group=64,
        replace_stride_with_dilation=None,
        norm=M.BatchNorm2d,
    ):
        super().__init__()
        self.in_channels = 64
        self.dilation = 1
        if replace_stride_with_dilation is None:
            # each element in the tuple indicates if we should replace
            # the 2x2 stride with a dilated convolution instead
            replace_stride_with_dilation = [False, False, False]
        if len(replace_stride_with_dilation) != 3:
            raise ValueError(
                "replace_stride_with_dilation should be None "
                "or a 3-element tuple, got {}".format(replace_stride_with_dilation)
            )
        self.groups = groups
        self.base_width = width_per_group
        self.conv1 = M.Conv2d(
            3, self.in_channels, kernel_size=7, stride=2, padding=3, bias=False
        )
        self.bn1 = norm(self.in_channels)
        self.maxpool = M.MaxPool2d(kernel_size=3, stride=2, padding=1)

        self.layer1_0 = BasicBlock(
            self.in_channels,
            64,
            stride=1,
            groups=self.groups,
            base_width=self.base_width,
            dilation=self.dilation,
            norm=M.BatchNorm2d,
        )
        self.layer1_1 = BasicBlock(
            self.in_channels,
            64,
            stride=1,
            groups=self.groups,
            base_width=self.base_width,
            dilation=self.dilation,
            norm=M.BatchNorm2d,
        )
        self.layer2_0 = BasicBlock(64, 128, stride=2)
        self.layer2_1 = BasicBlock(128, 128)
        self.layer3_0 = BasicBlock(128, 256, stride=2)
        self.layer3_1 = BasicBlock(256, 256)
        self.layer4_0 = BasicBlock(256, 512, stride=2)
        self.layer4_1 = BasicBlock(512, 512)

        self.layer1 = self._make_layer(block, 64, layers[0], norm=norm)
        self.layer2 = self._make_layer(
            block, 128, 2, stride=2, dilate=replace_stride_with_dilation[0], norm=norm
        )
        self.layer3 = self._make_layer(
            block, 256, 2, stride=2, dilate=replace_stride_with_dilation[1], norm=norm
        )
        self.layer4 = self._make_layer(
            block, 512, 2, stride=2, dilate=replace_stride_with_dilation[2], norm=norm
        )
        self.fc = M.Linear(512, num_classes)

        for m in self.modules():
            if isinstance(m, M.Conv2d):
                M.init.msra_normal_(m.weight, mode="fan_out", nonlinearity="relu")
                if m.bias is not None:
                    fan_in, _ = M.init.calculate_fan_in_and_fan_out(m.weight)
                    bound = 1 / math.sqrt(fan_in)
                    M.init.uniform_(m.bias, -bound, bound)
            elif isinstance(m, M.BatchNorm2d):
                M.init.ones_(m.weight)
                M.init.zeros_(m.bias)
            elif isinstance(m, M.Linear):
                M.init.msra_uniform_(m.weight, a=math.sqrt(5))
                if m.bias is not None:
                    fan_in, _ = M.init.calculate_fan_in_and_fan_out(m.weight)
                    bound = 1 / math.sqrt(fan_in)
                    M.init.uniform_(m.bias, -bound, bound)
        if zero_init_residual:
            for m in self.modules():
                M.init.zeros_(m.bn2.weight)

    def _make_layer(
        self, block, channels, blocks, stride=1, dilate=False, norm=M.BatchNorm2d
    ):
        previous_dilation = self.dilation
        if dilate:
            self.dilation *= stride
            stride = 1

        layers = []
        layers.append(
            block(
                self.in_channels,
                channels,
                stride,
                groups=self.groups,
                base_width=self.base_width,
                dilation=previous_dilation,
                norm=norm,
            )
        )
        self.in_channels = channels * block.expansion
        for _ in range(1, blocks):
            layers.append(
                block(
                    self.in_channels,
                    channels,
                    groups=self.groups,
                    base_width=self.base_width,
                    dilation=self.dilation,
                    norm=norm,
                )
            )

        return M.Sequential(*layers)

    def extract_features(self, x):
        outputs = {}
        x = self.conv1(x)
        x = self.bn1(x)
        x = F.relu(x)
        x = self.maxpool(x)
        outputs["stem"] = x

        x = self.layer1(x)
        outputs["res2"] = x
        x = self.layer2(x)
        outputs["res3"] = x
        x = self.layer3(x)
        outputs["res4"] = x
        x = self.layer4(x)
        outputs["res5"] = x
        return outputs

    def forward(self, x):
        x = self.extract_features(x)["res5"]

        x = F.avg_pool2d(x, 7)
        x = F.flatten(x, 1)
        x = self.fc(x)

        return x

    def get_stats(self, x):
        flops, activations = 0, 0
        in_x = deepcopy(x)
        x = self.conv1(x)
        tmp_flops, tmp_acts = cal_conv_stats(self.conv1, in_x, x)
        activations += tmp_acts
        flops += tmp_flops

        in_x = deepcopy(x)
        x = self.bn1(x)
        tmp_flops, tmp_acts = cal_norm_stats(self.bn1, in_x, x)
        activations += tmp_acts
        flops += tmp_flops

        x = F.relu(x)

        in_x = deepcopy(x)
        x = self.maxpool(x)
        tmp_flops, tmp_acts = cal_pool_stats(self.maxpool, in_x, x)
        activations += tmp_acts
        flops += tmp_flops

        x, tmp_flops, tmp_acts = self.layer1_0.get_stats(x)
        activations += tmp_acts
        flops += tmp_flops

        x, tmp_flops, tmp_acts = self.layer1_1.get_stats(x)
        activations += tmp_acts
        flops += tmp_flops

        x, tmp_flops, tmp_acts = self.layer2_0.get_stats(x)
        activations += tmp_acts
        flops += tmp_flops

        x, tmp_flops, tmp_acts = self.layer2_1.get_stats(x)
        activations += tmp_acts
        flops += tmp_flops

        x, tmp_flops, tmp_acts = self.layer3_0.get_stats(x)
        activations += tmp_acts
        flops += tmp_flops

        x, tmp_flops, tmp_acts = self.layer3_1.get_stats(x)
        activations += tmp_acts
        flops += tmp_flops

        x, tmp_flops, tmp_acts = self.layer4_0.get_stats(x)
        activations += tmp_acts
        flops += tmp_flops

        x, tmp_flops, tmp_acts = self.layer4_1.get_stats(x)
        activations += tmp_acts
        flops += tmp_flops

        x = F.avg_pool2d(x, 7)

        x = F.flatten(x, 1)

        in_x = deepcopy(x)
        x = self.fc(x)
        tmp_flops, tmp_acts = cal_linear_stats(self.fc, in_x, x)
        activations += tmp_acts
        flops += tmp_flops

        return flops, activations


def cal_conv_stats(module, input, output):
    bias = 1 if module.bias is not None else 0
    flops = np.prod(output[0].shape) * (
        module.in_channels // module.groups * np.prod(module.kernel_size) + bias
    )
    acts = np.prod(output[0].shape)
    return flops, acts


def cal_norm_stats(module, input, output):
    return np.prod(input[0].shape) * 7, np.prod(output[0].shape)


def cal_linear_stats(module, inputs, outputs):
    bias = module.out_features if module.bias is not None else 0
    return (
        np.prod(outputs[0].shape) * module.in_features + bias,
        np.prod(outputs[0].shape),
    )


def cal_pool_stats(module, inputs, outputs):
    return (
        np.prod(outputs[0].shape) * (module.kernel_size ** 2),
        np.prod(outputs[0].shape),
    )
