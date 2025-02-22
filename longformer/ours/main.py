import sys
import time
import math
import argparse
import numpy as np
import freetensor as ft
from freetensor import debug

sys.path.append('../..')
from common.numpy.io import load_txt, store_txt


def compile_all(w, dilation, dilation_heads, n_heads, seq_len, feat_len,
                device, ad_save_all):

    @ft.transform
    def inference(Q, K, V, Y):
        Q: ft.Var[(n_heads, seq_len, feat_len), "float32", "input"]
        K: ft.Var[(n_heads, seq_len, feat_len), "float32", "input"]
        V: ft.Var[(n_heads, seq_len, feat_len), "float32", "input"]
        Y: ft.Var[(n_heads, seq_len, feat_len), "float32", "output"]
        for i in range(n_heads):
            for j in range(seq_len):
                dot = ft.empty((2 * w + 1, ), "float32")
                for k in range(-w, w + 1):
                    dot[k + w] = 0
                    if j + ft.if_then_else(i >= dilation_heads, k, k * dilation
                                           ) >= 0 and j + ft.if_then_else(
                                               i >= dilation_heads, k,
                                               k * dilation) < seq_len:
                        for p in range(feat_len):
                            dot[k +
                                w] += Q[i, j, p] * K[i, j + ft.if_then_else(
                                    i >= dilation_heads, k, k * dilation), p]

                maxval = ft.empty((), "float32")
                maxval[()] = -float("inf")
                for k in range(2 * w + 1):
                    maxval[()] = ft.max(maxval[()], dot[k])
                expval = ft.empty((2 * w + 1, ), "float32")
                for k in range(2 * w + 1):
                    expval[k] = ft.exp(dot[k] - maxval[()])
                expsum = ft.empty((), "float32")
                expsum[()] = 0
                for k in range(2 * w + 1):
                    expsum[()] += expval[k]
                attn = ft.empty((2 * w + 1, ), "float32")
                for k in range(2 * w + 1):
                    attn[k] = expval[k] / expsum[()] / math.sqrt(feat_len)

                for p in range(feat_len):
                    Y[i, j, p] = 0
                for k in range(-w, w + 1):
                    if j + ft.if_then_else(i >= dilation_heads, k, k * dilation
                                           ) >= 0 and j + ft.if_then_else(
                                               i >= dilation_heads, k,
                                               k * dilation) < seq_len:
                        for p in range(feat_len):
                            Y[i, j,
                              p] += attn[k + w] * V[i, j + ft.if_then_else(
                                  i >= dilation_heads, k, k * dilation), p]

    print("# Inference:")
    print(inference)
    t0 = time.time()
    inference_exe = ft.optimize(
        inference,
        schedule_callback=lambda s: s.auto_schedule(device.target()),
        verbose=1)
    t1 = time.time()
    print(f"Inference compiling time: {t1 - t0}s")

    forward, backward, requires, privdes = ft.grad_(
        inference, set(["Q", "K", "V"]), set(["Y"]),
        ft.GradTapeMode.All if ad_save_all else ft.GradTapeMode.NoReuseOnly)

    print("# Forward:")
    print(forward)
    forward_exe = ft.optimize(
        forward,
        schedule_callback=lambda s: s.auto_schedule(device.target()),
        verbose=1)

    print("# Backward:")
    print(backward)
    backward_exe = ft.optimize(
        backward,
        schedule_callback=lambda s: s.auto_schedule(device.target()),
        verbose=1)

    def run_backward(Q, K, V, Y, d_Y, d_Q, d_K, d_V):
        kvs = {}
        kvs[privdes['Y']] = d_Y
        kvs[requires['Q']] = d_Q
        kvs[requires['K']] = d_K
        kvs[requires['V']] = d_V
        backward_exe(**kvs)

    return inference_exe, forward_exe, run_backward


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('target', nargs='?')
    parser.add_argument('--warmup-repeat',
                        type=int,
                        default=10,
                        dest='warmup_num')
    parser.add_argument('--timing-repeat',
                        type=int,
                        default=100,
                        dest='test_num')
    parser.add_argument('--ad-save-all',
                        action='store_true',
                        dest='ad_save_all')
    parser.add_argument('--profile-gpu',
                        action='store_true',
                        dest='profile_gpu')
    cmd_args = parser.parse_args()

    if cmd_args.profile_gpu:
        from common.gpu import profile_start, profile_stop

    device = cmd_args.target

    n_heads = 8
    seq_len = 10000
    feat_len = 512
    w = 32
    dilation = 4  # counts from 1
    dilation_heads = 2
    q = load_txt("../q.in", "float32")
    k = load_txt("../k.in", "float32")
    v = load_txt("../v.in", "float32")
    y = np.zeros((n_heads, seq_len, feat_len), dtype="float32")
    d_q = np.zeros(q.shape, dtype='float32')
    d_k = np.zeros(k.shape, dtype='float32')
    d_v = np.zeros(v.shape, dtype='float32')
    d_y = load_txt("../d_y.in", "float32")

    if device == 'gpu':
        ir_dev = ft.Device(ft.GPU())
    else:
        assert device == 'cpu'
        ir_dev = ft.Device(ft.CPU())

    q = ft.Array(q)
    k = ft.Array(k)
    v = ft.Array(v)
    y = ft.Array(y)
    d_q = ft.Array(d_q)
    d_k = ft.Array(d_k)
    d_v = ft.Array(d_v)
    d_y = ft.Array(d_y)

    with ir_dev:
        inference, forward, backward = compile_all(w, dilation, dilation_heads,
                                                   n_heads, seq_len, feat_len,
                                                   ir_dev,
                                                   cmd_args.ad_save_all)

    print(
        f"{cmd_args.warmup_num} warmup, {cmd_args.test_num} repeats for evalution"
    )
    warmup_num = cmd_args.warmup_num
    test_num = cmd_args.test_num

    for i in range(warmup_num):
        inference(q, k, v, y)
        if i == 0:
            store_txt("y.out", y.numpy().reshape((n_heads, seq_len, feat_len)))
    ir_dev.sync()
    if cmd_args.profile_gpu:
        profile_start()
    t0 = time.time()
    for i in range(test_num):
        inference(q, k, v, y)
    ir_dev.sync()
    t1 = time.time()
    if cmd_args.profile_gpu:
        profile_stop()

    print(f"Inference Time = {(t1 - t0) / test_num * 1000} ms")

    if cmd_args.profile_gpu:
        exit(0)

    for i in range(warmup_num):
        forward(q, k, v, y)
    ir_dev.sync()
    t0 = time.time()
    for i in range(test_num):
        forward(q, k, v, y)
    ir_dev.sync()
    t1 = time.time()

    print(f"Forward Time = {(t1 - t0) / test_num * 1000} ms")

    for i in range(warmup_num):
        backward(q, k, v, y, d_y, d_q, d_k, d_v)
        if i == 0:
            store_txt("d_q.out",
                      d_q.numpy().reshape((n_heads, seq_len, feat_len)))
            store_txt("d_k.out",
                      d_k.numpy().reshape((n_heads, seq_len, feat_len)))
            store_txt("d_v.out",
                      d_v.numpy().reshape((n_heads, seq_len, feat_len)))
    ir_dev.sync()
    t0 = time.time()
    for i in range(test_num):
        backward(q, k, v, y, d_y, d_q, d_k, d_v)
    ir_dev.sync()
    t1 = time.time()

    print(f"Backward Time = {(t1 - t0) / test_num * 1000} ms")
