import random
import torch
from typing import Tuple

import deep_gemm
from deep_gemm import bench_kineto, calc_diff, ceil_div, get_col_major_tma_aligned_tensor
from deep_gemm.jit_kernels.utils import get_m_alignment_for_contiguous_layout


def per_token_cast_to_fp8(x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
    assert x.dim() == 2 and x.size(1) % 128 == 0
    m, n = x.shape
    x_view = x.view(m, -1, 128)
    x_amax = x_view.abs().float().amax(dim=2).view(m, -1).clamp(1e-4)
    return (x_view * (448.0 / x_amax.unsqueeze(2))).to(torch.float8_e4m3fn).view(m, n), (x_amax / 448.0).view(m, -1)


def per_block_cast_to_fp8(x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
    assert x.dim() == 2
    m, n = x.shape
    x_padded = torch.zeros((ceil_div(m, 128) * 128, ceil_div(n, 128) * 128), dtype=x.dtype, device=x.device)
    x_padded[:m, :n] = x
    x_view = x_padded.view(-1, 128, x_padded.size(1) // 128, 128)
    x_amax = x_view.abs().float().amax(dim=(1, 3), keepdim=True).clamp(1e-4)
    x_scaled = (x_view * (448.0 / x_amax)).to(torch.float8_e4m3fn)
    return x_scaled.view_as(x_padded)[:m, :n].contiguous(), (x_amax / 448.0).view(x_view.size(0), x_view.size(2))


def construct(m: int, k: int, n: int) -> \
        Tuple[Tuple[torch.Tensor, torch.Tensor], Tuple[torch.Tensor, torch.Tensor], torch.Tensor, torch.Tensor]:
    x = torch.randn((m, k), device='cuda', dtype=torch.bfloat16)
    y = torch.randn((n, k), device='cuda', dtype=torch.bfloat16)
    out = torch.empty((m, n), device='cuda', dtype=torch.bfloat16)
    ref_out = x @ y.t()

    x_fp8, y_fp8 = per_token_cast_to_fp8(x), per_block_cast_to_fp8(y)
    # Transpose earlier so that the testing will not trigger transposing kernels
    x_fp8 = (x_fp8[0], get_col_major_tma_aligned_tensor(x_fp8[1]))
    return x_fp8, y_fp8, out, ref_out


def construct_contiguous_grouped(num_groups: int, expected_m_per_group: int, k: int, n: int) -> \
        Tuple[int, Tuple[torch.Tensor, torch.Tensor], Tuple[torch.Tensor, torch.Tensor], torch.Tensor, torch.Tensor, torch.Tensor]:
    m = 0
    m_aligned = get_m_alignment_for_contiguous_layout()
    group_m_list = []
    for i in range(num_groups):
        group_m = m_aligned * random.randint(int(expected_m_per_group * 0.7) // m_aligned, int(expected_m_per_group * 1.3) // m_aligned)
        m += group_m
        group_m_list.append(group_m)
    x = torch.randn((m, k), device='cuda', dtype=torch.bfloat16)
    y = torch.randn((num_groups, n, k), device='cuda', dtype=torch.bfloat16)    
    m_indices = torch.empty(m, device='cuda', dtype=torch.int32)
    out = torch.empty((m, n), device='cuda', dtype=torch.bfloat16)
    ref_out = torch.randn((m, n), device='cuda', dtype=torch.bfloat16)

    start = 0
    for i, group_m in enumerate(group_m_list):
        end = start + group_m
        m_indices[start:end] = i
        ref_out[start:end] = x[start:end] @ y[i].t()
        start = end

    assert m % 4 == 0, f'TMA alignment error: {m}'
    x_fp8 = per_token_cast_to_fp8(x)
    y_fp8 = (torch.empty_like(y, dtype=torch.float8_e4m3fn), torch.empty((num_groups, (n + 127) // 128, k // 128), device='cuda', dtype=torch.float))
    for i in range(num_groups):
        y_fp8[0][i], y_fp8[1][i] = per_block_cast_to_fp8(y[i])

    return m, x_fp8, y_fp8, m_indices, out, ref_out


def construct_masked_grouped(num_groups: int, m: int, k: int, n: int) -> \
        Tuple[Tuple[torch.Tensor, torch.Tensor], Tuple[torch.Tensor, torch.Tensor], torch.Tensor, torch.Tensor]:
    x = torch.randn((num_groups, m, k), device='cuda', dtype=torch.bfloat16)
    y = torch.randn((num_groups, n, k), device='cuda', dtype=torch.bfloat16)
    out = torch.empty((num_groups, m, n), device='cuda', dtype=torch.bfloat16)
    ref_out = torch.einsum('gmk,gnk->gmn', x, y)

    assert m % 4 == 0, f'TMA alignment error: {m}'
    x_fp8 = (torch.empty_like(x, dtype=torch.float8_e4m3fn), torch.empty((num_groups, m, k // 128), device='cuda', dtype=torch.float))
    y_fp8 = (torch.empty_like(y, dtype=torch.float8_e4m3fn), torch.empty((num_groups, (n + 127) // 128, k // 128), device='cuda', dtype=torch.float))
    for i in range(num_groups):
        x_fp8[0][i], x_fp8[1][i] = per_token_cast_to_fp8(x[i])
        y_fp8[0][i], y_fp8[1][i] = per_block_cast_to_fp8(y[i])

    # Transpose earlier so that the testing will not trigger transposing kernels
    x_fp8 = (x_fp8[0], get_col_major_tma_aligned_tensor(x_fp8[1]))
    return x_fp8, y_fp8, out, ref_out


def test_gemm() -> None:
    print('Testing GEMM:')
    for m in (64, 128, 4096):
        for k, n in [(7168, 2112), (1536, 24576), (512, 32768), (16384, 7168), (7168, 4096), (2048, 7168)]:
            x_fp8, y_fp8, out, ref_out = construct(m, k, n)
            deep_gemm.gemm_fp8_fp8_bf16_nt(x_fp8, y_fp8, out)
            diff = calc_diff(out, ref_out)
            assert diff < 0.001, f'{m=}, {k=}, {n=}, {diff:.5f}'

            # Construct new tensors only once to avoid L2 cache acceleration (creating them puts them in L2)
            x_fp8, y_fp8, out, ref_out = construct(m, k, n)

            # noinspection PyShadowingNames
            def test_func():
                deep_gemm.gemm_fp8_fp8_bf16_nt(x_fp8, y_fp8, out)

            t = bench_kineto(test_func, 'fp8_gemm', suppress_kineto_output=True)
            print(f' > Performance (m={m:5}, n={n:5}, k={k:5}): {t * 1e6:4.0f} us | '
                  f'throughput: {2 * m * n * k / t / 1e12:4.0f} TFLOPS, '
                  f'{(m * k + k * n + m * n * 2) / 1e9 / t:4.0f} GB/s')
    print()


def test_m_grouped_gemm_contiguous() -> None:
    print('Testing grouped contiguous GEMM:')

    for num_groups, expected_m_per_group, k, n in ((4, 8192, 7168, 4096), (4, 8192, 2048, 7168), (8, 4096, 7168, 4096), (8, 4096, 2048, 7168)):
        # TODO: make a stronger test
        m, x_fp8, y_fp8, m_indices, out, ref_out = construct_contiguous_grouped(num_groups, expected_m_per_group, k, n)
        deep_gemm.m_grouped_gemm_fp8_fp8_bf16_nt_contiguous(x_fp8, y_fp8, out, m_indices)
        diff = calc_diff(out, ref_out)
        assert diff < 0.001, f'{m=}, {k=}, {n=}, {diff:.5f}'

        # Construct new tensors only once to avoid L2 cache acceleration (creating them puts them in L2)
        m, x_fp8, y_fp8, m_indices, out, ref_out = construct_contiguous_grouped(num_groups, expected_m_per_group, k, n)

        # noinspection PyShadowingNames
        def test_func():
            deep_gemm.m_grouped_gemm_fp8_fp8_bf16_nt_contiguous(x_fp8, y_fp8, out, m_indices)

        t = bench_kineto(test_func, 'fp8_gemm', suppress_kineto_output=True)
        print(f' > Performance ({num_groups=}, m={m:4}, n={n:4}, k={k:4}): {t * 1e6:4.0f} us | '
              f'throughput: {2 * m * n * k / t / 1e12:4.0f} TFLOPS, '
              f'{(m * k + num_groups * k * n + m * n * 2) / 1e9 / t:4.0f} GB/s')
    print()


def test_m_grouped_gemm_masked() -> None:
    print('Testing grouped masked GEMM:')

    for num_groups, m in ((1, 1024), (2, 512), (4, 256)):
        for k, n in ((7168, 4096), (2048, 7168), ):
            # Test correctness
            masked_m_candidates = list(filter(lambda candidate: candidate <= m, (64, 128, 192, 256, 320, 384)))
            for i in range(10):
                x_fp8, y_fp8, out, ref_out = construct_masked_grouped(num_groups, m, k, n)
                masked_m = torch.empty((num_groups, ), device='cuda', dtype=torch.int)
                for j in range(num_groups):
                    masked_m[j] = random.choice(masked_m_candidates)
                expected_m = min(int(masked_m.float().mean()) + 1, m)
                deep_gemm.m_grouped_gemm_fp8_fp8_bf16_nt_masked(x_fp8, y_fp8, out, masked_m, expected_m)
                for j in range(num_groups):
                    diff = calc_diff(out[j, :masked_m[j].item()], ref_out[j, :masked_m[j].item()])
                    assert diff < 0.001, f'{m=}, {k=}, {n=}, {j=}, masked_m={masked_m[j]}, {num_groups=}, {diff:.5f}'

            # Construct new tensors only once to avoid L2 cache acceleration (creating them puts them in L2)
            x_fp8, y_fp8, out, ref_out = construct_masked_grouped(num_groups, m, k, n)
            masked_m = torch.ones((num_groups, ), device='cuda', dtype=torch.int) * m

            # noinspection PyShadowingNames
            def test_func():
                deep_gemm.m_grouped_gemm_fp8_fp8_bf16_nt_masked(x_fp8, y_fp8, out, masked_m, m)

            # Test performance with fixed shapes
            t = bench_kineto(test_func, 'fp8_gemm', suppress_kineto_output=True)
            print(f' > Performance ({num_groups=}, m_per_group={m:4}, n={n:4}, k={k:4}): {t * 1e6:4.0f} us | '
                  f'throughput: {2 * num_groups * m * n * k / t / 1e12:4.0f} TFLOPS, '
                  f'{(num_groups * (m * k + k * n + m * n * 2)) / 1e9 / t:4.0f} GB/s')
    print()


if __name__ == '__main__':
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    torch.manual_seed(0)
    random.seed(0)

    print('Library path:')
    print(f' > {deep_gemm.__path__}\n')

    test_gemm()
    test_m_grouped_gemm_contiguous()
    test_m_grouped_gemm_masked()
