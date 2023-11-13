import logging
import packaging.version
from dataclasses import dataclass
from typing import Union
import functools
import torch
import sfast
from sfast.jit import passes
from sfast.jit.trace_helper import lazy_trace
from sfast.jit import utils as jit_utils
from sfast.cuda.graphs import make_dynamic_graphed_callable
from sfast.utils import gpu_device

logger = logging.getLogger()


class CompilationConfig:
    @dataclass
    class Default:
        memory_format: torch.memory_format = (
            torch.channels_last
            if gpu_device.device_has_tensor_core()
            else torch.contiguous_format
        )
        enable_jit: bool = True
        enable_jit_freeze: bool = True
        preserve_parameters: bool = True
        enable_cnn_optimization: bool = True
        prefer_lowp_gemm: bool = True
        enable_xformers: bool = False
        enable_cuda_graph: bool = False
        enable_triton: bool = False
        trace_scheduler: bool = False


def compile(m, config):
    m.unet = compile_unet(m.unet, config)
    if hasattr(m, 'controlnet'):
        m.controlnet = compile_unet(m.controlnet, config)

    enable_cuda_graph = config.enable_cuda_graph and m.device.type == 'cuda'

    if config.enable_xformers:
        _enable_xformers(m)

    if config.memory_format is not None:
        m.vae.to(memory_format=config.memory_format)

    if config.enable_jit:
        lazy_trace_ = _build_lazy_trace(config)

        m.text_encoder.forward = lazy_trace_(m.text_encoder.forward)
        if (
            not packaging.version.parse('2.0.0')
            <= packaging.version.parse(torch.__version__)
            < packaging.version.parse('2.1.0')
        ):
            """
            Weird bug in PyTorch 2.0.x

            RuntimeError: shape '[512, 512, 64, 64]' is invalid for input of size 2097152

            When executing AttnProcessor in TorchScript
            """
            m.vae.decode = lazy_trace_(m.vae.decode)
            # For img2img
            m.vae.encoder.forward = lazy_trace_(m.vae.encoder.forward)
            m.vae.quant_conv.forward = lazy_trace_(m.vae.quant_conv.forward)
        if config.trace_scheduler:
            m.scheduler.scale_model_input = lazy_trace_(m.scheduler.scale_model_input)
            m.scheduler.step = lazy_trace_(m.scheduler.step)

    return m


def compile_unet(m, config):
    enable_cuda_graph = config.enable_cuda_graph and m.device.type == 'cuda'

    if config.enable_xformers:
        _enable_xformers(m)

    if config.memory_format is not None:
        m.to(memory_format=config.memory_format)

    if config.enable_jit:
        lazy_trace_ = _build_lazy_trace(config)
        m.forward = lazy_trace_(m.forward)

    if enable_cuda_graph:
        m.forward = make_dynamic_graphed_callable(m.forward)

    return m


def _modify_model(
    m,
    enable_cnn_optimization=True,
    prefer_lowp_gemm=True,
    enable_triton=False,
    memory_format=None,
):
    if enable_triton:
        from sfast.jit.passes import triton_passes

    torch._C._jit_pass_inline(m.graph)

    passes.jit_pass_remove_dropout(m.graph)

    passes.jit_pass_remove_contiguous(m.graph)
    passes.jit_pass_replace_view_with_reshape(m.graph)
    if enable_triton:
        triton_passes.jit_pass_optimize_reshape(m.graph)

        # triton_passes.jit_pass_optimize_cnn(m.graph)

        triton_passes.jit_pass_fuse_group_norm_silu(m.graph)
        triton_passes.jit_pass_optimize_group_norm(m.graph)

    passes.jit_pass_optimize_linear(m.graph)

    if memory_format is not None:
        sfast._C._jit_pass_convert_op_input_tensors(
            m.graph, 'aten::_convolution', indices=[0], memory_format=memory_format
        )

    if enable_cnn_optimization:
        passes.jit_pass_optimize_cnn(m.graph)

    if prefer_lowp_gemm:
        passes.jit_pass_prefer_lowp_gemm(m.graph)
        passes.jit_pass_fuse_lowp_linear_add(m.graph)


def _ts_compiler(
    m,
    call_helper,
    inputs,
    kwarg_inputs,
    modify_model_fn=None,
    freeze=False,
    preserve_parameters=False,
):
    with torch.jit.optimized_execution(True):
        if freeze and not getattr(m, 'training', False):
            # raw freeze causes Tensor reference leak
            # because the constant Tensors in the GraphFunction of
            # the compilation unit are never freed.
            m = jit_utils.better_freeze(
                    m,
                    preserve_parameters=preserve_parameters,
                )
        if modify_model_fn is not None:
            modify_model_fn(m)

    return m


def _build_lazy_trace(config):
    modify_model = functools.partial(
        _modify_model,
        enable_cnn_optimization=config.enable_cnn_optimization,
        prefer_lowp_gemm=config.prefer_lowp_gemm,
        enable_triton=config.enable_triton,
        memory_format=config.memory_format,
    )

    ts_compiler = functools.partial(
        _ts_compiler,
        freeze=config.enable_jit_freeze,
        preserve_parameters=config.preserve_parameters,
        modify_model_fn=modify_model,
    )

    lazy_trace_ = functools.partial(
        lazy_trace,
        ts_compiler=ts_compiler,
        check_trace=False,
        strict=False,
    )

    return lazy_trace_


def _enable_xformers(m):
    from xformers import ops
    from sfast.utils.xformers_attention import (
        xformers_memory_efficient_attention,
    )

    ops.memory_efficient_attention = xformers_memory_efficient_attention
    m.enable_xformers_memory_efficient_attention()